"""Local, review-first template-mining service shared by GUI and CLI."""

from __future__ import annotations

from typing import Any

from src.expenses.email.template_miner import BankAlertTemplateMiner


class TemplateMiningService:
    """Mine stored candidate emails and create unsaved declarative-rule drafts."""

    def __init__(self, *, expenses_repository, bank_rule_catalog, miner: BankAlertTemplateMiner | None = None) -> None:
        self.expenses_repository = expenses_repository
        self.bank_rule_catalog = bank_rule_catalog
        self.miner = miner or BankAlertTemplateMiner()

    def mine(self, *, provider_id: str = "", limit: int = 2_000, min_support: int = 2) -> dict[str, Any]:
        """Return masked, bounded template suggestions; no rule/configuration is changed."""

        records = self.expenses_repository.list_template_mining_records(
            provider_id=provider_id,
            limit=min(max(1, int(limit or 2_000)), self.miner.MAX_RECORDS),
        )
        return self.miner.mine(records, min_support=min_support)

    def build_draft(
        self,
        template_key: str,
        *,
        provider_id: str = "",
        limit: int = 2_000,
        min_support: int = 2,
    ) -> dict[str, Any]:
        """Return one editable draft from a currently mined template without saving it."""

        result = self.mine(provider_id=provider_id, limit=limit, min_support=min_support)
        selected = next(
            (item for item in result["templates"] if str(item.get("templateKey", "")) == str(template_key)),
            None,
        )
        if selected is None:
            raise ValueError("The requested template is not available for the current mining filters.")
        effective = self.bank_rule_catalog.load_effective()
        existing_ids = {
            str(item.get("id", "")).strip()
            for item in effective.get("banks", [])
            if isinstance(item, dict) and str(item.get("id", "")).strip()
        }
        return {
            "template": selected,
            "draft": self.miner.build_rule_draft(selected, existing_ids=existing_ids),
            "saved": False,
            "activation": "review_and_save_required",
        }
