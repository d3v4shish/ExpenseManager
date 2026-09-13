from __future__ import annotations

import copy
from typing import Any


DEFAULT_APP_SETTINGS: dict[str, Any] = {
    "schemaVersion": 1,
    "logging": {
        "level": "INFO",
        "maxBytes": 2_000_000,
        "backupCount": 5,
    },
    "analytics": {
        "enabled": True,
        "sensitivity": "normal",
        "minimumHistory": 5,
        "unusualAmountMultiplier": 2.5,
        "robustDeviation": 3.5,
        "comparisonMonths": 6,
        "minimumComparisonMonths": 3,
        "periodSpikePercent": 30.0,
        "recurringChangePercent": 15.0,
        "recurringMinimumConfidence": 0.7,
        "duplicateWindowMinutes": 10,
        "inrTransactionFloor": 500.0,
        "inrPeriodFloor": 1000.0,
    },
    "notifications": {"desktop": False},
}

DEFAULT_SYNC_SETTINGS = {
    "lookbackDays": 14,
    "refreshMinutes": 10,
    "overlapHours": 2,
}


class SettingsService:
    """Validate and atomically persist every user-changeable app setting."""

    SETTINGS_FILE = "app_settings.json"
    EMAIL_FILE = "email_accounts.json"

    def __init__(self, files) -> None:
        self.files = files

    def load(self) -> dict[str, Any]:
        raw = self.files.read_user(self.SETTINGS_FILE) if self.files.user_path(self.SETTINGS_FILE).exists() else {}
        payload = self._merge(DEFAULT_APP_SETTINGS, raw)
        email_config = self.files.read_user(self.EMAIL_FILE) if self.files.user_path(self.EMAIL_FILE).exists() else {}
        sync = self._merge(DEFAULT_SYNC_SETTINGS, email_config.get("sync", {}) if isinstance(email_config, dict) else {})
        payload["sync"] = sync
        return self.validate(payload)

    def save(self, updates: dict[str, Any]) -> dict[str, Any]:
        current = self.load()
        merged = self._merge(current, dict(updates or {}))
        validated = self.validate(merged)

        app_payload = {key: value for key, value in validated.items() if key != "sync"}
        self.files.write_user(self.SETTINGS_FILE, app_payload)

        email_config = self.files.read_user(self.EMAIL_FILE) if self.files.user_path(self.EMAIL_FILE).exists() else {}
        if not isinstance(email_config, dict):
            email_config = {}
        email_config = dict(email_config)
        email_config["sync"] = dict(validated["sync"])
        self.files.write_user(self.EMAIL_FILE, email_config)
        return validated

    def validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        merged = self._merge({**DEFAULT_APP_SETTINGS, "sync": DEFAULT_SYNC_SETTINGS}, payload)
        logging_config = dict(merged.get("logging", {}))
        level = str(logging_config.get("level", "INFO")).upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Log level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL.")
        logging_config["level"] = level
        logging_config["maxBytes"] = self._bounded_int(logging_config.get("maxBytes"), 100_000, 100_000_000, "Log size")
        logging_config["backupCount"] = self._bounded_int(logging_config.get("backupCount"), 1, 50, "Log backup count")

        sync = dict(merged.get("sync", {}))
        sync["lookbackDays"] = self._bounded_int(sync.get("lookbackDays"), 1, 3650, "Lookback days")
        sync["refreshMinutes"] = self._bounded_int(sync.get("refreshMinutes"), 1, 1440, "Refresh interval")
        sync["overlapHours"] = self._bounded_int(sync.get("overlapHours"), 0, 168, "Overlap hours")

        analytics = dict(merged.get("analytics", {}))
        sensitivity = str(analytics.get("sensitivity", "normal")).lower()
        if sensitivity not in {"low", "normal", "high"}:
            raise ValueError("Analytics sensitivity must be low, normal, or high.")
        analytics["enabled"] = bool(analytics.get("enabled", True))
        analytics["sensitivity"] = sensitivity
        analytics["minimumHistory"] = self._bounded_int(analytics.get("minimumHistory"), 3, 100, "Minimum history")
        analytics["comparisonMonths"] = self._bounded_int(analytics.get("comparisonMonths"), 3, 36, "Comparison months")
        analytics["minimumComparisonMonths"] = self._bounded_int(
            analytics.get("minimumComparisonMonths"), 2, analytics["comparisonMonths"], "Minimum comparison months"
        )
        analytics["duplicateWindowMinutes"] = self._bounded_int(
            analytics.get("duplicateWindowMinutes"), 1, 1440, "Duplicate window"
        )
        for key, minimum, maximum, label in (
            ("unusualAmountMultiplier", 1.1, 20.0, "Unusual amount multiplier"),
            ("robustDeviation", 1.0, 20.0, "Robust deviation"),
            ("periodSpikePercent", 1.0, 1000.0, "Period spike percentage"),
            ("recurringChangePercent", 1.0, 1000.0, "Recurring change percentage"),
            ("recurringMinimumConfidence", 0.0, 1.0, "Recurring confidence"),
            ("inrTransactionFloor", 0.0, 100_000_000.0, "INR transaction floor"),
            ("inrPeriodFloor", 0.0, 100_000_000.0, "INR period floor"),
        ):
            analytics[key] = self._bounded_float(analytics.get(key), minimum, maximum, label)

        notifications = {"desktop": bool(dict(merged.get("notifications", {})).get("desktop", False))}
        return {
            "schemaVersion": 1,
            "logging": logging_config,
            "analytics": analytics,
            "notifications": notifications,
            "sync": sync,
        }

    @staticmethod
    def _bounded_int(value: Any, minimum: int, maximum: int, label: str) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a whole number.") from exc
        if parsed < minimum or parsed > maximum:
            raise ValueError(f"{label} must be between {minimum} and {maximum}.")
        return parsed

    @staticmethod
    def _bounded_float(value: Any, minimum: float, maximum: float, label: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be numeric.") from exc
        if parsed < minimum or parsed > maximum:
            raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}.")
        return parsed

    @classmethod
    def _merge(cls, base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(base)
        for key, value in dict(incoming or {}).items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = cls._merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result


__all__ = ["DEFAULT_APP_SETTINGS", "DEFAULT_SYNC_SETTINGS", "SettingsService"]
