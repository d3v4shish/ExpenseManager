from __future__ import annotations

import inspect
import logging

from src.expenses.email.mail_types import ParsedFact, RecordParser, SourceRecord


class MailParserChain:
    """Run the registered mail parsers over one normalized source record."""

    def __init__(self) -> None:
        """Initialize the parser list."""

        self._parsers: list[RecordParser] = []
        self.logger = logging.getLogger(self.__class__.__name__)

    def add(self, parser: RecordParser) -> None:
        """Register one parser in the chain."""

        self._parsers.append(parser)

    def parse(self, record: SourceRecord) -> list[ParsedFact]:
        """Return the merged facts emitted by every matching parser."""

        return list(self.inspect_record(record).get("facts", []))

    def inspect_record(self, record: SourceRecord, *, rules_payload: dict | None = None) -> dict:
        """Return candidate-match diagnostics and parsed facts for one source record."""

        facts: list[ParsedFact] = []
        attempts: list[dict] = []
        candidate_matched = False
        for parser in self._parsers:
            diagnostic_fn = getattr(parser, "diagnose", None)
            if callable(diagnostic_fn):
                diagnostic_kwargs = {"rules_payload": rules_payload}
                if "existing_facts" in inspect.signature(diagnostic_fn).parameters:
                    diagnostic_kwargs["existing_facts"] = list(facts)
                result = diagnostic_fn(record, **diagnostic_kwargs)
                matched = bool(result.get("candidateMatched"))
                parsed = list(result.get("facts", []))
                attempts.extend(list(result.get("attempts", [])))
            else:
                matched = parser.matches(record)
                parsed = parser.parse(record) if matched else []
            self.logger.debug(
                "Parser decision parser_id=%s matched=%s external_id=%s sender=%s title=%s",
                getattr(parser, "parser_id", parser.__class__.__name__),
                matched,
                record.external_id,
                record.sender,
                record.title,
            )
            if not matched:
                continue
            candidate_matched = True
            facts.extend(parsed)
            self.logger.debug(
                "Parser output parser_id=%s external_id=%s fact_count=%s fact_types=%s",
                getattr(parser, "parser_id", parser.__class__.__name__),
                record.external_id,
                len(parsed),
                [fact.fact_type for fact in parsed],
            )
        return {
            "candidateMatched": candidate_matched,
            "facts": facts,
            "attempts": attempts,
        }
