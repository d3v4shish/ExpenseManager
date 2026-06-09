from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(slots=True)
class SourceRecord:
    """Represent one normalized source record from a provider."""

    provider_id : str
    record_type : str
    external_id : str
    title       : str
    sender      : str
    received_at : str
    payload     : dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""


@dataclass(slots=True)
class ParsedFact:
    """Represent one normalized fact emitted by a parser."""

    fact_type         : str
    parser_id         : str
    source_provider_id: str
    source_record_type: str
    source_external_id: str
    payload           : dict[str, Any] = field(default_factory=dict)
    confidence        : float = 1.0


class RecordProvider(Protocol):
    """Describe the fetch boundary used by source providers."""

    provider_id: str
    record_type: str

    def fetch_records(self, since: datetime | None = None) -> list[SourceRecord]:
        """Return normalized source records."""


class RecordParser(Protocol):
    """Describe the parse boundary used by source parsers."""

    parser_id: str

    def matches(self, record: SourceRecord) -> bool:
        """Return whether the parser should inspect the record."""

    def parse(self, record: SourceRecord) -> list[ParsedFact]:
        """Return normalized facts parsed from the record."""
