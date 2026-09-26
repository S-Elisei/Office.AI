"""Runtime configuration: env vars with sane defaults, plus the on-disk layout."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    root: Path
    port: int
    claude_bin: str | None
    codex_bin: str | None
    agy_bin: str | None

    @property
    def db_path(self) -> Path:
        return self.root / "office.db"

    @property
    def repo_dir(self) -> Path:
        return self.root / "repo"

    @property
    def seed_dir(self) -> Path:
        return self.repo_dir / "seed"

    @property
    def ws_dir(self) -> Path:
        return self.root / "ws"

    @property
    def lfs_dir(self) -> Path:
        return self.root / "lfs"

    @property
    def scratch_dir(self) -> Path:
        """One sandbox per workspace.

        An agent's tooling, temporary scripts and throwaway output go in its own
        subdirectory here, named by workspace id.
        """
        return self.root / "scratch"

    @property
    def tails_dir(self) -> Path:
        """Output lines per agent."""
        return self.root / "tails"

    @property
    def stages_dir(self) -> Path:
        """One working tree per stage, its preparation log beside it, and `.trash/`."""
        return self.root / "stages"

    def ensure_layout(self) -> None:
        for path in (
            self.root,
            self.repo_dir,
            self.seed_dir,
            self.ws_dir,
            self.lfs_dir,
            self.scratch_dir,
            self.tails_dir,
            self.stages_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _detect_cli(env_var: str, which_name: str, fallback: Path | None) -> str | None:
    override = os.environ.get(env_var)
    if override:
        return override
    found = shutil.which(which_name)
    if found:
        return found
    if fallback is not None and fallback.exists():
        return str(fallback)
    return None


def data_root() -> Path:
    """The office's data directory, <root>. Its parent is the owner's repository."""
    return Path(os.environ.get("OFFICE_ROOT", str(PROJECT_ROOT / ".office-data"))).resolve()


def load_config() -> Config:
    root = data_root()
    port = int(os.environ.get("OFFICE_PORT", "7777"))

    local_app_data = os.environ.get("LOCALAPPDATA")
    agy_fallback = Path(local_app_data) / "agy" / "bin" / "agy.EXE" if local_app_data else None
    claude_fallback = Path.home() / ".local" / "bin" / "claude.exe"

    return Config(
        root=root,
        port=port,
        claude_bin=_detect_cli("OFFICE_CLAUDE_BIN", "claude", claude_fallback),
        codex_bin=_detect_cli("OFFICE_CODEX_BIN", "codex", None),
        agy_bin=_detect_cli("OFFICE_AGY_BIN", "agy", agy_fallback),
    )
