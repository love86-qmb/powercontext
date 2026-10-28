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

"""Line-level captured CSV rows with exact original-record recovery.

Every data record of an offline CSV document becomes its own ``CAPTURED`` Source
identified by the document and the record's primary key value. The original
header order and the exact field strings are preserved so a retrieval citation
can be resolved back to the complete original record.
"""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

CSV_ROW_SOURCE_NAME = "csv-row"
CSV_ROW_DEFINITION_VERSION = "1"
_CSV_ROW_IDENTITY_SCHEME = "csv"
_UTF8_BOM = b"\xef\xbb\xbf"


class CSVDocumentInput(BaseModel):
    """One offline CSV document plus the column that identifies its records."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One captured CSV data record in original header order."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """A captured CSV row whose identity is its document and primary key value."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Return the stable identity of one CSV row Source."""

    _require_document_id(document_id)
    identity = (
        f"{_CSV_ROW_IDENTITY_SCHEME}:{quote(document_id, safe='')}:"
        f"{quote(primary_key_value, safe='')}"
    )
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(
            f"csv row source id must not exceed {MAX_SOURCE_ID_LENGTH} characters"
        )
    return identity


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse and validate one CSV document into per-row inputs.

    The whole document is parsed and validated before any row is returned; every
    encoding, quoting or data validation error surfaces as ``ValueError`` so no
    partially parsed document can be mistaken for a complete one.
    """

    _require_document_id(value.document_id)
    _require_primary_key(value.primary_key)

    reader = csv.reader(io.StringIO(_decode_content(value.content), newline=""), strict=True)
    try:
        records = list(reader)
    except csv.Error as error:
        raise ValueError(f"malformed CSV document: {error}") from error

    if not records:
        raise ValueError("CSV document must contain a header record")

    columns = tuple(records[0])
    _validate_header(columns)

    try:
        key_index = columns.index(value.primary_key)
    except ValueError:
        raise ValueError(
            f"primary_key {value.primary_key!r} is not an exact CSV header column"
        ) from None

    width = len(columns)
    rows: list[CSVRowInput] = []
    keys: set[str] = set()
    for record in records[1:]:
        if not record:
            # A completely empty physical record is not a data record.
            continue
        if len(record) != width:
            raise ValueError(
                f"CSV record must contain {width} fields, found {len(record)}"
            )
        key_value = record[key_index]
        if not key_value or not key_value.strip():
            raise ValueError("CSV primary key values must be non-empty and non-blank")
        if key_value in keys:
            raise ValueError(f"duplicate CSV primary key value {key_value!r}")
        keys.add(key_value)
        rows.append(
            CSVRowInput(
                document_id=value.document_id,
                primary_key=value.primary_key,
                primary_key_value=key_value,
                columns=columns,
                values=tuple(record),
            )
        )
    return tuple(rows)


class CSVRowSourceAdapter:
    """Resolve and read CSV row Sources from per-row inputs."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=source_id,
            definition_version=CSV_ROW_DEFINITION_VERSION,
            materialization=SourceMaterialization.CAPTURED,
            source_id=source_id,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    """Expose one CSV row as textual Artifact evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_ADAPTER = CSVRowSourceAdapter()
CSV_ROW_SOURCE_DEFINITION = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    version=CSV_ROW_DEFINITION_VERSION,
    projections=(CSVRowTextEvidenceProjection(),),
)


def _decode_content(content: bytes) -> str:
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise ValueError("CSV content must be bytes")
    raw = bytes(content)
    if raw.startswith(_UTF8_BOM):
        raw = raw[len(_UTF8_BOM) :]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("CSV content must be valid UTF-8") from error


def _validate_header(columns: tuple[str, ...]) -> None:
    if not columns:
        raise ValueError("CSV header must not be empty")
    seen: set[str] = set()
    for column in columns:
        if not column or not column.strip():
            raise ValueError("CSV header fields must be non-empty and non-blank")
        if column in seen:
            raise ValueError(f"duplicate CSV header field {column!r}")
        seen.add(column)


def _require_document_id(document_id: str) -> None:
    if not isinstance(document_id, str) or not document_id:
        raise ValueError("document_id must be a non-empty string")
    if document_id != document_id.strip():
        raise ValueError("document_id must not contain leading or trailing whitespace")


def _require_primary_key(primary_key: str) -> None:
    if not isinstance(primary_key, str) or not primary_key or not primary_key.strip():
        raise ValueError("primary_key must be a non-empty string")


__all__ = [
    "CSV_ROW_DEFINITION_VERSION",
    "CSV_ROW_SOURCE_ADAPTER",
    "CSV_ROW_SOURCE_DEFINITION",
    "CSV_ROW_SOURCE_NAME",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "CSVRowSourceAdapter",
    "CSVRowTextEvidenceProjection",
    "csv_row_source_id",
    "parse_csv_document",
]
