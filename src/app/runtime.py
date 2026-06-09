from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.app.files import RuntimeFiles


@dataclass(frozen=True)
class JobSpec:
    """Describe one background job exposed to the app window."""

    job_id: str
    handler: Callable[[], Any]
    async_job: bool = False


@dataclass
class AppRuntime:
    """Collect the runtime-owned domain pieces for the app shell."""

    services: dict[str, Any] = field(default_factory=dict)
    repositories: dict[str, Any] = field(default_factory=dict)
    jobs: tuple[JobSpec, ...] = field(default_factory=tuple)
    startup_tasks: tuple[Callable[[], Any], ...] = field(default_factory=tuple)
    files: RuntimeFiles | None = None
