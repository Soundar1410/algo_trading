"""Path resolution — every path derives from one configured project root.

The spec (section 10) forbids hard-coded home directories and old-repository
paths: the project lives on a mounted volume and must survive being moved. So
there is exactly one input (the project root) and everything else is derived.

Resolution order for the root:

1. an explicit argument (tests, and any caller that already knows),
2. ``PROJECT_ROOT`` from the environment / ``.env``,
3. the repository the running code sits in (walk up to the ``pyproject.toml``).

Step 3 keeps a fresh checkout runnable with no ``.env`` at all, which matters
because the test suite must not require local configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .settings import Settings, load_settings


class ProjectRootError(RuntimeError):
    """Raised when the project root cannot be resolved or does not exist."""


def _discover_root_from_source() -> Path | None:
    """Walk up from this file to the directory holding ``pyproject.toml``."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


def resolve_project_root(
    explicit: Path | str | None = None,
    settings: Settings | None = None,
) -> Path:
    """Resolve the one project root every other path derives from.

    Raises:
        ProjectRootError: if the resolved root is missing. Failing here is
            deliberate — a runtime that silently invents a data directory on a
            mount that did not come up would write state nobody looks at.
    """
    candidate: Path | None = None

    if explicit is not None:
        candidate = Path(explicit)
    else:
        settings = settings if settings is not None else load_settings()
        if settings.project_root:
            candidate = Path(settings.project_root)
        else:
            candidate = _discover_root_from_source()

    if candidate is None:
        raise ProjectRootError(
            "Cannot resolve the project root. Set PROJECT_ROOT in .env to the "
            "absolute path of this repository."
        )

    root = candidate.expanduser().resolve()
    if not root.is_dir():
        raise ProjectRootError(
            f"Project root {root} does not exist or is not a directory. "
            "If the project lives on a mounted volume, check that it is mounted."
        )
    return root


@dataclass(frozen=True)
class ProjectPaths:
    """Every directory the platform writes to, derived from one root."""

    project_root: Path

    @property
    def data_root(self) -> Path:
        return self.project_root / "data"

    @property
    def log_root(self) -> Path:
        return self.project_root / "logs"

    @property
    def runtime_root(self) -> Path:
        return self.data_root / "runtime"

    @property
    def operational_root(self) -> Path:
        """One SQLite database per runtime group lives here."""
        return self.data_root / "operational"

    @property
    def cache_root(self) -> Path:
        return self.data_root / "cache"

    @property
    def backup_root(self) -> Path:
        """Pre-migration database snapshots (Phase 7 Part 5). Never raw
        caches or PID files — spec section 12 excludes both explicitly."""
        return self.data_root / "backups"

    @property
    def reference_root(self) -> Path:
        return self.data_root / "reference"

    @property
    def config_root(self) -> Path:
        return self.project_root / "config"

    @property
    def pid_root(self) -> Path:
        return self.runtime_root / "pid"

    @property
    def lock_root(self) -> Path:
        return self.runtime_root / "locks"

    def database_path(self, runtime_id: str) -> Path:
        """Operational database for one runtime group, e.g. ``intraday_options``."""
        return self.operational_root / f"{runtime_id}.db"

    def ensure_writable_dirs(self) -> None:
        """Create the directories the platform writes to.

        Config and reference directories are excluded on purpose: those are
        inputs, and silently creating an empty one turns "you have not written
        the config yet" into a confusing empty-directory error later.
        """
        for path in (
            self.data_root,
            self.log_root,
            self.runtime_root,
            self.operational_root,
            self.cache_root,
            self.pid_root,
            self.lock_root,
            self.backup_root,
        ):
            path.mkdir(parents=True, exist_ok=True)


def load_paths(
    explicit_root: Path | str | None = None,
    settings: Settings | None = None,
) -> ProjectPaths:
    """Build :class:`ProjectPaths` from the resolved project root."""
    return ProjectPaths(project_root=resolve_project_root(explicit_root, settings))
