from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


ProviderFactory = Callable[[dict[str, Any], Path], Any]


class SourceProviderRegistry:
    """Create configured local source providers by stable provider type."""

    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, provider_type: str, factory: ProviderFactory) -> None:
        normalized = str(provider_type).strip().lower()
        if not normalized:
            raise ValueError("Provider type is required.")
        self._factories[normalized] = factory

    def supported_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))

    def create(self, provider_config: dict[str, Any], runtime_config_path: Path):
        provider_type = str(provider_config.get("type", "")).strip().lower()
        factory = self._factories.get(provider_type)
        if factory is None:
            supported = ", ".join(self.supported_types()) or "none"
            raise ValueError(f"Unsupported source type '{provider_type or '<missing>'}'. Supported types: {supported}.")
        return factory(provider_config, runtime_config_path)


def build_default_source_provider_registry() -> SourceProviderRegistry:
    """Return the built-in local-only provider registry."""

    from src.expenses.sources.providers import (
        AxiosAlternativeArchiveProvider,
        CsvProvider,
        EmlFolderProvider,
        SmsBackupProvider,
        ThunderbirdProvider,
    )

    registry = SourceProviderRegistry()
    registry.register("thunderbird", lambda config, path: ThunderbirdProvider(config, path))
    registry.register("eml_folder", lambda config, path: EmlFolderProvider(config))
    registry.register("csv", lambda config, path: CsvProvider(config))
    registry.register("sms_backup", lambda config, path: SmsBackupProvider(config))
    registry.register("axios_archive", lambda config, path: AxiosAlternativeArchiveProvider(config))
    return registry
