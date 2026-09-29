"""Vendor adapters. One per runtime, and one INSTANCE per turn."""

from __future__ import annotations

from office.adapters.agy import AgyAdapter
from office.adapters.claude import ClaudeAdapter
from office.adapters.codex import CodexAdapter
from office.config import Config

__all__ = ["adapter_for", "installed", "RUNTIMES"]

RUNTIMES = ("claude", "codex", "agy")

_CLASSES = {"claude": ClaudeAdapter, "codex": CodexAdapter, "agy": AgyAdapter}


def _bin(runtime: str, config: Config) -> str | None:
    return {"claude": config.claude_bin, "codex": config.codex_bin, "agy": config.agy_bin}[runtime]


def installed(runtime: str, config: Config) -> bool:
    """Whether this machine has the runtime's CLI."""
    return bool(_bin(runtime, config))


def adapter_for(runtime: str, config: Config):
    if runtime not in _CLASSES:
        raise ValueError(f"unknown runtime {runtime!r}")
    bin_path = _bin(runtime, config)
    if not bin_path:
        raise RuntimeError(f"{runtime} CLI not found; set OFFICE_{runtime.upper()}_BIN")
    return _CLASSES[runtime](bin_path)
