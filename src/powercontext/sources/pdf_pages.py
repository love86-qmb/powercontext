# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Page-level Sources for complete financial-report PDF documents.

Every physical page of the original document becomes its own ``CAPTURED`` Source
identified by the document, the digest of its exact bytes and the physical page
number, so a retrieval citation can be resolved back to one page of one revision
of one file. The whole document is parsed before any page is returned: a
truncated or unreadable file fails atomically instead of yielding a partial
report.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import weakref
from collections import Counter
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PDF_PAGE_SOURCE_NAME = "pdf-page"
PDF_PAGE_DEFINITION_VERSION = "1"
_PDF_PAGE_IDENTITY_SCHEME = "pdf"
_MAX_IDENTITY_HASH_LENGTH = 64
_DEFAULT_SEARCH_PAGE_LIMIT = 100
_MAX_QUERY_TERMS = 8
_MAX_INFLECTION_TERMS = 6
_DEFAULT_SEARCH_PAGE_HITS = 40
_MAX_ENTRY_LOOKUPS = 500
_PASS_QUESTION_WEIGHT = 2.5
_MAX_CORPUS_ENTRIES = 20000
_BM25_K1 = 1.2
_BM25_B = 0.75

_CORPUS_CACHE: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class _PageCandidate:
    """One evidence page and the retrieval support accumulated for it."""

    __slots__ = ("best_rank", "citation", "citation_rank", "score")

    def __init__(self, *, citation: object, citation_rank: int) -> None:
        self.citation = citation
        self.citation_rank = citation_rank
        self.best_rank = citation_rank
        self.score = 0.0


def _pass_weight(hits: int) -> float:
    """Weight one pass by how selective it was: fewer pages means rarer terms."""

    return 1.0 / (1.0 + math.log(1.0 + hits))
_MINIMUM_TERM_LENGTH = 3
_QUERY_TOKEN = re.compile(r"[a-z]+|\d+")
_QUERY_STOPWORDS = frozenset({
    "and",
    "any",
    "are",
    "been",
    "both",
    "does",
    "for",
    "from",
    "has",
    "have",
    "into",
    "its",
    "much",
    "only",
    "over",
    "such",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "why",
    "will",
    "with",
    "would",
})


class PDFDocumentInput(BaseModel):
    """One complete original PDF document."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page of one exact document revision."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """A captured PDF page whose identity is its document revision and page number."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Return the stable identity of one PDF page Source."""

    _require_document_id(document_id)
    _require_digest(document_sha256)
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    identity = (
        f"{_PDF_PAGE_IDENTITY_SCHEME}:{quote(document_id, safe='')}:"
        f"{document_sha256}:{page_number}"
    )
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        digest = hashlib.sha256(f"{document_id}\x00{document_sha256}".encode()).hexdigest()
        identity = f"{_PDF_PAGE_IDENTITY_SCHEME}:hashed:{digest[:_MAX_IDENTITY_HASH_LENGTH]}:{page_number}"
    return identity


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of one original PDF in order.

    The document is read end to end before anything is returned, so a damaged or
    unsupported file raises ``ValueError`` instead of exposing a partial report.
    A PDF that only carries an empty-password permission marker is a legal input.
    """

    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    _require_document_id(value.document_id)
    raw = _document_bytes(value.content)
    digest = hashlib.sha256(raw).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires a user password that was not supplied")
        pages: list[PDFPageInput] = []
        for number, page in enumerate(reader.pages, start=1):
            text = page.extract_text()
            pages.append(
                PDFPageInput(
                    document_id=value.document_id,
                    document_sha256=digest,
                    page_number=number,
                    text="" if text is None else text,
                )
            )
    except ValueError:
        raise
    except (PdfReadError, OSError, KeyError, TypeError, AttributeError, IndexError) as error:
        raise ValueError(f"PDF document could not be fully parsed: {error}") from error
    except Exception as error:  # noqa: BLE001 - any pypdf failure means the input is unusable
        raise ValueError(f"PDF document could not be fully parsed: {error}") from error
    if not pages:
        raise ValueError("PDF document must contain at least one physical page")
    return tuple(pages)


async def search_pdf_pages(memory: object, question: str, limit: int = 10) -> tuple[object, ...]:
    """Return at most ``limit`` exact citations to distinct evidence pages.

    Every returned citation is a real stored entry that ``memory.get`` can
    resolve, so a caller can回查 the page. Ranking runs over the whole document
    set read through the public Memory API: a financial report is long enough
    that only an idf-weighted score over every page, with the query and the page
    reduced by the same stemmer, reliably surfaces the page that carries the
    answer. Full-text search is the fallback when the corpus is unavailable.
    """

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be non-empty text")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")

    ranked = await _rank_by_corpus(memory, question, limit)
    if ranked is not None and len(ranked) >= limit:
        return ranked
    # Full-text search still contributes whenever the corpus ranking could not
    # fill the answer, so the result never shrinks below what search alone found.
    searched = await _rank_by_full_text_search(memory, question, limit)
    if ranked is None:
        return searched
    merged = list(ranked)
    seen = {
        (
            citation.memory_ref.artifact_id,
            citation.memory_ref.revision,
            citation.entry_id,
            citation.entry_version_id,
        )
        for citation in merged
    }
    for citation in searched:
        if len(merged) >= limit:
            break
        identity = (
            citation.memory_ref.artifact_id,
            citation.memory_ref.revision,
            citation.entry_id,
            citation.entry_version_id,
        )
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(citation)
    return tuple(merged)


async def _rank_by_corpus(memory: object, question: str, limit: int) -> tuple[object, ...] | None:
    """Rank every stored page against the question with an idf-weighted score."""

    corpus = await _pdf_corpus(memory)
    if corpus is None or not corpus.documents:
        return None
    terms = _query_stems(question)
    if not terms:
        return None
    scored = _bm25_page_scores(corpus, terms)
    if not scored:
        return None
    citations: list[object] = []
    seen: set[tuple[str, int, str, str]] = set()
    for score, document in scored:
        if score <= 0.0:
            break
        citation = document.citation
        identity = (
            citation.memory_ref.artifact_id,
            citation.memory_ref.revision,
            citation.entry_id,
            citation.entry_version_id,
        )
        if identity in seen:
            continue
        seen.add(identity)
        citations.append(citation)
        if len(citations) >= limit:
            break
    return tuple(citations) if citations else None


async def _rank_by_full_text_search(memory: object, question: str, limit: int) -> tuple[object, ...]:
    """Rank pages through the backend search channels when no corpus is readable."""

    from powercontext.builtin.artifacts.memory.models import MemoryCitation
    from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest

    pages: dict[tuple[str, str], _PageCandidate] = {}
    entry_pages: dict[tuple[str, int, str, str], tuple[tuple[str, str], ...]] = {}
    lookups = 0
    for pass_index, query in enumerate(_query_plan(question)):
        try:
            found = await memory.search(
                SearchMemoryRequest(query=query, mode="fts", limit=_DEFAULT_SEARCH_PAGE_LIMIT)
            )
        except Exception:  # noqa: BLE001 - one unusable query must not lose the others
            continue
        hits = tuple(found.hits)[:_DEFAULT_SEARCH_PAGE_HITS]
        if not hits:
            continue
        # A pass that returns few pages carries a rare term, so a page it reaches
        # is stronger evidence than one reached by a term the whole report
        # mentions. The original question is the strongest pass of all.
        weight = _PASS_QUESTION_WEIGHT if pass_index == 0 else _pass_weight(len(hits))
        for rank, hit in enumerate(hits):
            identity = (
                hit.memory_ref.artifact_id,
                hit.memory_ref.revision,
                hit.entry_id,
                hit.entry_version_id,
            )
            candidate_keys = entry_pages.get(identity)
            if candidate_keys is None:
                if lookups >= _MAX_ENTRY_LOOKUPS:
                    continue
                lookups += 1
                citation = MemoryCitation(
                    memory_ref=hit.memory_ref,
                    entry_id=hit.entry_id,
                    entry_version_id=hit.entry_version_id,
                )
                try:
                    entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
                except Exception:  # noqa: BLE001 - a stale hit is simply skipped
                    continue
                candidate_keys = tuple(
                    (reference.source_type, reference.source_id) for reference in entry.sources
                )
                if not candidate_keys:
                    continue
                entry_pages[identity] = candidate_keys
                for key in candidate_keys:
                    if key not in pages:
                        pages[key] = _PageCandidate(citation=citation, citation_rank=rank)
            for key in candidate_keys:
                page = pages.get(key)
                if page is None:
                    continue
                page.score += weight
                if rank < page.best_rank:
                    page.best_rank = rank
    # Rank by how many question passes support a page before how early any single
    # pass placed it: the evidence page of a long report usually shares several
    # terms with the question, and a single rare-term hit is often a coincidence.
    ordered = sorted(
        pages.values(),
        key=lambda page: (-page.score, page.best_rank, page.citation_rank),
    )
    citations: list[MemoryCitation] = []
    seen: set[tuple[str, int, str, str]] = set()
    for page in ordered:
        citation = page.citation
        identity = (
            citation.memory_ref.artifact_id,
            citation.memory_ref.revision,
            citation.entry_id,
            citation.entry_version_id,
        )
        if identity in seen:
            continue
        seen.add(identity)
        citations.append(citation)
        if len(citations) >= limit:
            break
    return tuple(citations)


class _CorpusDocument:
    """One stored Memory entry reduced to stemmed term frequencies."""

    __slots__ = ("citation", "length", "page_keys", "terms")

    def __init__(self, *, citation: object, page_keys: tuple[tuple[str, str], ...], terms: object) -> None:
        self.citation = citation
        self.page_keys = page_keys
        self.terms = terms
        self.length = sum(terms.values())


class _PdfCorpus:
    """Every stored entry of one scoped Memory, tokenised once."""

    __slots__ = ("average_length", "documents", "frequency")

    def __init__(
        self,
        documents: list[_CorpusDocument],
        frequency: dict[str, int],
        average_length: float,
    ) -> None:
        self.documents = documents
        self.frequency = frequency
        self.average_length = average_length


async def _pdf_corpus(memory: object) -> _PdfCorpus | None:
    """Return the tokenised document set of one scoped Memory, building it once."""

    try:
        cached = _CORPUS_CACHE.get(memory)
    except TypeError:
        return await _build_corpus(memory)
    if cached is not None:
        return cached
    corpus = await _build_corpus(memory)
    if corpus is not None:
        try:
            _CORPUS_CACHE[memory] = corpus
        except TypeError:
            pass
    return corpus


async def _build_corpus(memory: object) -> _PdfCorpus | None:
    try:
        page = await memory.list()
    except Exception:  # noqa: BLE001 - retrieval must still work without a full read
        return None
    records = tuple(getattr(page, "entries", ()) or ())
    if not records or len(records) > _MAX_CORPUS_ENTRIES:
        return None
    documents: list[_CorpusDocument] = []
    frequency: dict[str, int] = {}
    total = 0
    for record in records:
        entry = getattr(record, "entry", None)
        text = getattr(entry, "text", None)
        if not isinstance(text, str) or not text:
            continue
        page_keys = tuple((reference.source_type, reference.source_id) for reference in entry.sources)
        if not page_keys:
            continue
        terms = Counter()
        for token in _QUERY_TOKEN.findall(text.lower()):
            terms[_stem(token)] += 1
        if not terms:
            continue
        document = _CorpusDocument(citation=record.citation, page_keys=page_keys, terms=terms)
        documents.append(document)
        total += document.length
        for term in terms:
            frequency[term] = frequency.get(term, 0) + 1
    if not documents:
        return None
    return _PdfCorpus(documents, frequency, total / len(documents))


def _query_stems(question: str) -> tuple[str, ...]:
    """Return the question's content terms reduced by the corpus stemmer."""

    stems: list[str] = []
    for token in _QUERY_TOKEN.findall(question.lower()):
        if len(token) < _MINIMUM_TERM_LENGTH or token in _QUERY_STOPWORDS:
            continue
        stem = _stem(token)
        if stem and stem not in stems:
            stems.append(stem)
    return tuple(stems)


def _bm25_page_scores(corpus: _PdfCorpus, terms: tuple[str, ...]) -> list[tuple[float, _CorpusDocument]]:
    """Score every page with BM25 and keep each page's best entry."""

    count = len(corpus.documents)
    average = corpus.average_length or 1.0
    idf: dict[str, float] = {}
    for term in terms:
        document_frequency = corpus.frequency.get(term, 0)
        idf[term] = math.log(1.0 + (count - document_frequency + 0.5) / (document_frequency + 0.5))
    best: dict[tuple[str, str], tuple[float, _CorpusDocument]] = {}
    for document in corpus.documents:
        score = 0.0
        for term in terms:
            occurrences = document.terms.get(term, 0)
            if not occurrences:
                continue
            denominator = occurrences + _BM25_K1 * (
                1.0 - _BM25_B + _BM25_B * document.length / average
            )
            score += idf[term] * occurrences * (_BM25_K1 + 1.0) / denominator
        if score <= 0.0:
            continue
        for key in document.page_keys:
            current = best.get(key)
            if current is None or score > current[0]:
                best[key] = (score, document)
    return sorted(best.values(), key=lambda item: -item[0])


def _stem(token: str) -> str:
    """Reduce one English token to a stable surface form.

    The stored page text is analysed without stemming, so the question and the
    page are reduced here with the same rules instead: ``geographies`` and
    ``geography`` both become ``geography``, ``operates`` and ``operating`` both
    become ``operat``.
    """

    if len(token) <= _MINIMUM_TERM_LENGTH:
        return token
    if token.endswith("ies") and len(token) > 4:
        token = token[:-3] + "y"
    elif token.endswith("es") and len(token) > 4:
        token = token[:-2]
    elif token.endswith("s") and not token.endswith(("ss", "us", "is")):
        token = token[:-1]
    if token.endswith("ing") and len(token) > 6:
        token = token[:-3]
    elif token.endswith("ed") and len(token) > 5:
        token = token[:-2]
    if token.endswith("ment") and len(token) > 7:
        token = token[:-4]
    elif token.endswith("ly") and len(token) > 5:
        token = token[:-2]
    if token.endswith("e") and len(token) > 4:
        token = token[:-1]
    if len(token) > 4 and token[-1] == token[-2] and token[-1] not in "aeiouls":
        token = token[:-1]
    return token


def _query_plan(question: str) -> tuple[str, ...]:
    """Return the question, its most specific terms and their inflected forms.

    Analyzer v1 casefolds and splits words without stemming, so the stored page
    text only matches the exact surface form. A question that says
    ``geographies`` therefore cannot reach a page that says ``geography``
    unless the plural and verb forms are searched explicitly.
    """

    plan = [question]
    seen = _QUERY_TOKEN.findall(question.lower())
    ordered = sorted(
        (term for term in set(seen) if len(term) >= _MINIMUM_TERM_LENGTH and term not in _QUERY_STOPWORDS),
        key=lambda term: (-len(term), term),
    )
    exact = ordered[:_MAX_QUERY_TERMS]
    plan.extend(exact)
    known = set(exact) | {question}
    for term in exact:
        for variant in sorted(_inflections(term)):
            if len(plan) >= _MAX_QUERY_TERMS + 1 + _MAX_INFLECTION_TERMS:
                break
            if variant in known:
                continue
            known.add(variant)
            plan.append(variant)
    return tuple(plan)


def _inflections(term: str) -> set[str]:
    """Return the plausible surface-form variants of one English query term."""

    variants: set[str] = set()
    if len(term) < 5:
        return variants
    if term.endswith("ies") or term.endswith("ied"):
        # Singular forms of ``-ies``/``-ied`` are the useful surface forms; the
        # generic suffix rules below would only add unusable fragments.
        variants.add(term[:-3] + "y")
        return {variant for variant in variants if len(variant) >= _MINIMUM_TERM_LENGTH}
    if term.endswith("ing"):
        variants.update({term[:-3], term[:-3] + "e"})
    if term.endswith("ed"):
        variants.add(term[:-2])
    if term.endswith("es"):
        variants.add(term[:-1])
    if term.endswith("s") and not term.endswith("ss"):
        variants.add(term[:-1])
    if term.endswith("ly"):
        variants.add(term[:-2])
    if term.endswith("ment"):
        variants.add(term[:-4])
    if term.endswith("ion"):
        variants.update({term[:-3], term[:-3] + "e"})
    return {variant for variant in variants if len(variant) >= _MINIMUM_TERM_LENGTH}


class PDFPageSourceAdapter:
    """Resolve and read PDF page Sources from per-page inputs."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            definition_version=PDF_PAGE_DEFINITION_VERSION,
            materialization=SourceMaterialization.CAPTURED,
            source_id=source_id,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one PDF page as textual Artifact evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    version=PDF_PAGE_DEFINITION_VERSION,
    projections=(PDFPageTextEvidenceProjection(),),
)


def _document_bytes(content: object) -> bytes:
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise ValueError("PDF content must be bytes")
    raw = bytes(content)
    if not raw:
        raise ValueError("PDF document must not be empty")
    if not raw.lstrip()[:5].startswith(b"%PDF-"):
        raise ValueError("PDF document must start with a PDF header")
    return raw


def _require_document_id(document_id: object) -> str:
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")
    if document_id != document_id.strip():
        raise ValueError("document_id must not contain leading or trailing whitespace")
    return document_id


def _require_digest(document_sha256: object) -> str:
    if (
        not isinstance(document_sha256, str)
        or len(document_sha256) != 64
        or any(character not in "0123456789abcdef" for character in document_sha256)
    ):
        raise ValueError("document_sha256 must be a lowercase hexadecimal SHA-256 string")
    return document_sha256


__all__ = [
    "PDF_PAGE_DEFINITION_VERSION",
    "PDF_PAGE_SOURCE_ADAPTER",
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "PDFPageSourceAdapter",
    "PDFPageTextEvidenceProjection",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
