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
import re
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

    The scoped Memory owns retrieval, so every returned citation is a real stored
    entry that ``memory.get`` can resolve. Page coverage grows by running the
    original question first and then its most specific terms, because a single
    full-text pass over a long report rarely reaches enough distinct pages.
    """

    from powercontext.builtin.artifacts.memory.models import MemoryCitation
    from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be non-empty text")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")

    ranked: list[tuple[object, ...]] = []
    for query in _query_plan(question):
        try:
            page = await memory.search(
                SearchMemoryRequest(query=query, mode="fts", limit=_DEFAULT_SEARCH_PAGE_LIMIT)
            )
        except Exception:  # noqa: BLE001 - one unusable query must not lose the others
            continue
        if page.hits:
            ranked.append(tuple(page.hits))

    citations: list[MemoryCitation] = []
    pages: set[tuple[str, str]] = set()
    entries: set[tuple[str, int, str, str]] = set()
    depth = 0
    # Round-robin across passes instead of draining the first one: a long report
    # rarely hides its evidence page behind a single whole-question match, but a
    # specific term usually reaches it, so every pass contributes its best page
    # before any pass contributes its second.
    while len(citations) < limit and any(depth < len(hits) for hits in ranked):
        for hits in ranked:
            if len(citations) >= limit:
                break
            if depth >= len(hits):
                continue
            hit = hits[depth]
            identity = (
                hit.memory_ref.artifact_id,
                hit.memory_ref.revision,
                hit.entry_id,
                hit.entry_version_id,
            )
            if identity in entries:
                continue
            entries.add(identity)
            citation = MemoryCitation(
                memory_ref=hit.memory_ref,
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
            try:
                entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
            except Exception:  # noqa: BLE001 - a stale hit is simply skipped
                continue
            sources = {(reference.source_type, reference.source_id) for reference in entry.sources}
            if not sources or sources & pages:
                continue
            pages |= sources
            citations.append(citation)
        depth += 1
    return tuple(citations)


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
