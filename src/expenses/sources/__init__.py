"""Local ingestion providers for ExpenseManager."""

from src.expenses.sources.registry import SourceProviderRegistry, build_default_source_provider_registry

__all__ = ["SourceProviderRegistry", "build_default_source_provider_registry"]
