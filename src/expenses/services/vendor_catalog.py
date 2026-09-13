from __future__ import annotations

import logging
from typing import Any

from src.expenses.repositories.vendor_catalog_repository import VendorCatalogRepository


class VendorCatalogService:
    """Expose vendor catalog CRUD and matching for the expense manager."""

    def __init__(self, repository: VendorCatalogRepository) -> None:
        """Create one vendor-catalog service backed by the local repository."""

        self.repository = repository
        self.logger = logging.getLogger(self.__class__.__name__)

    def resolve_vendor(self, raw_value: str) -> dict[str, Any] | None:
        """Resolve one raw counterparty value to a vendor row."""

        try:
            return self.repository.resolve_vendor(raw_value)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Vendor resolution failed raw_value=%s error=%s", raw_value, exc)
            return None

    def resolve_vendors(self, raw_values: list[str]) -> dict[str, dict[str, Any] | None]:
        """Resolve a batch from one catalog snapshot instead of one DB connection per row."""

        requested = [str(value or "").strip() for value in raw_values]
        requested = list(dict.fromkeys(value for value in requested if value))
        if not requested:
            return {}
        try:
            records = self.repository.list_vendors()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Vendor batch resolution failed error=%s", exc)
            return {}

        exact: dict[str, dict[str, Any]] = {}
        lowered: dict[str, dict[str, Any]] = {}
        for record in records:
            values = [
                str(record.get("canonicalVendor", "")),
                str(record.get("canonicalAlias", "")),
                str(record.get("nickname", "")),
                *[str(item) for item in record.get("aliases", [])],
            ]
            for value in values:
                clean = value.strip()
                if not clean:
                    continue
                normalized = self.repository.normalize_alias(clean)
                if normalized:
                    exact.setdefault(normalized, record)
                lowered.setdefault(clean.lower(), record)

        matches: dict[str, dict[str, Any] | None] = {}
        for raw in requested:
            normalized = self.repository.normalize_alias(raw)
            record = exact.get(normalized) if normalized else None
            record = record or lowered.get(raw.lower())
            # Merged descendants are not included in the root-only list snapshot.
            # Resolve those rare cases once per distinct raw value, never once per row.
            if record is None:
                record = self.repository.resolve_vendor(raw)
            matches[raw] = dict(record) if record is not None else None
        return matches

    def search_vendors(self, query: str) -> list[dict[str, Any]]:
        """Return vendors that match one free-text query."""

        try:
            return self.repository.search_vendors(query)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Vendor search failed query=%s error=%s", query, exc)
            return []

    def list_vendors(self, query: str = "") -> list[dict[str, Any]]:
        """Return every vendor, optionally filtered by one text query."""

        try:
            return self.repository.list_vendors(query)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Vendor list failed query=%s error=%s", query, exc)
            return []

    def get_vendor(self, vendor_id: int) -> dict[str, Any] | None:
        """Return one vendor by id when it exists."""

        try:
            return self.repository.get_vendor(vendor_id)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Get vendor failed vendor_id=%s error=%s", vendor_id, exc)
            return None

    def find_vendor(self, value: str) -> dict[str, Any] | None:
        """Return one vendor by canonical value, alias, nickname, or merge row."""

        try:
            return self.repository.find_vendor(value)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Find vendor failed value=%s error=%s", value, exc)
            return None

    def create_vendor(
        self,
        canonical_name: str,
        *,
        canonical_alias: str = "",
        nickname: str = "",
        notes: str = "",
        categories: list[str] | None = None,
    ) -> int:
        """Create one vendor row and return its database id."""

        return self.repository.create_vendor(
            canonical_name,
            canonical_alias=canonical_alias,
            nickname=nickname,
            notes=notes,
            categories=categories or [],
        )

    def update_vendor(
        self,
        vendor_id: int,
        *,
        canonical_name: str,
        canonical_alias: str,
        nickname: str = "",
        notes: str = "",
        categories: list[str] | None = None,
    ) -> None:
        """Update one vendor row and its categories."""

        self.repository.update_vendor(
            vendor_id,
            canonical_name=canonical_name,
            canonical_alias=canonical_alias,
            nickname=nickname,
            notes=notes,
            categories=categories or [],
        )

    def delete_vendor(self, vendor_id: int) -> None:
        """Delete one vendor row and its related merge/category data."""

        self.repository.delete_vendor(vendor_id)

    def add_vendor_category(self, vendor_id: int, category: str) -> None:
        """Attach one category to a vendor."""

        self.repository.add_vendor_category(vendor_id, category)

    def remove_vendor_category(self, vendor_id: int, category: str) -> None:
        """Remove one category from a vendor."""

        self.repository.remove_vendor_category(vendor_id, category)

    def list_merge_members(self, vendor_id: int) -> list[dict[str, Any]]:
        """Return merged raw-name members for one vendor."""

        return self.repository.list_merge_members(vendor_id)

    def add_merge_member(self, vendor_id: int, raw_name: str, *, source: str = "manual") -> None:
        """Attach one raw-name merge row to a vendor."""

        self.repository.add_merge_member(vendor_id, raw_name, source=source)

    def remove_merge_member(self, vendor_id: int, raw_name: str) -> None:
        """Remove one raw-name merge row from a vendor."""

        self.repository.remove_merge_member(vendor_id, raw_name)

    def merge_vendors(self, source_vendor_id: int, target_vendor_id: int, kept_alias: str) -> dict[str, Any]:
        """Soft-merge one source vendor into one kept target vendor."""

        return self.repository.merge_vendors(source_vendor_id, target_vendor_id, kept_alias)

    def unmerge_vendor(self, source_vendor_id: int) -> dict[str, Any]:
        """Restore one previously merged vendor as its own root vendor."""

        return self.repository.unmerge_vendor(source_vendor_id)

    def reindex_vendor_search(self) -> None:
        """Rebuild vendor lookup indexes on the local catalog database."""

        self.repository.reindex_vendor_search()

    def upsert_vendor(
        self,
        canonical_name: str,
        *,
        nickname: str = "",
        category: str = "Uncategorized",
        subcategory: str = "",
        notes: str = "",
        active: bool = True,
    ) -> int:
        """Preserve the older vendor upsert shape used by the expenses UI."""

        _ = subcategory, active
        return self.repository.upsert_vendor(
            canonical_name,
            nickname=nickname,
            category=category,
            subcategory="",
            notes=notes,
            active=True,
        )

    def upsert_alias(self, canonical_name: str, alias: str, *, match_kind: str = "exact", active: bool = True) -> int:
        """Attach one alias to a vendor using the older alias API."""

        _ = match_kind, active
        return self.repository.upsert_alias(canonical_name, alias)

