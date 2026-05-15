"""TOML-backed configuration for tsh.

Config lives at ``$TSH_CONFIG_DIR/config.toml`` (defaults to ``~/.tsh``).
Defaults are merged in for any missing keys, so a brand-new install is usable.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

DEFAULTS: dict[str, dict[str, Any]] = {
    "jira": {
        "base_url": "",
        "email": "",
        "default_project_key": "",
    },
    "time": {
        "display_timezone": "Australia/Sydney",
        "rounding": "nearest",
        "rounding_minutes": 15,
        "idle_threshold_minutes": 10,
        "max_idle_minutes_before_autostop": 240,
        "stale_active_threshold_minutes": 120,
    },
    "app": {
        "autostart": False,
        "http_port": 42024,
    },
    "git": {
        "branch_pattern": r"[A-Z][A-Z0-9_]+-\d+",
    },
}


class ConfigError(Exception):
    """Raised when the on-disk config is unreadable or malformed."""


def config_dir() -> Path:
    override = os.environ.get("TSH_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".tsh"


def config_path() -> Path:
    return config_dir() / "config.toml"


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Return base merged with overlay; overlay wins for scalars, recurses for dicts."""
    out: dict[str, Any] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict[str, Any]:
    """Read the config from disk, returning defaults for missing keys/files."""
    p = config_path()
    if not p.exists():
        return _deep_merge(DEFAULTS, {})
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Invalid TOML in {p}: {e}") from e
    return _deep_merge(DEFAULTS, data)


def save(data: dict[str, Any]) -> None:
    """Write the full config dict atomically (write-temp then replace)."""
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(tomli_w.dumps(data).encode("utf-8"))
    tmp.replace(p)


def get(key: str) -> Any:
    """Look up a dotted-path key, e.g. ``jira.email``."""
    parts = key.split(".")
    cur: Any = load()
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(key)
        cur = cur[part]
    return cur


def set(key: str, value: Any) -> None:  # noqa: A001 — mirrors `tsh config set`
    """Set a dotted-path key and persist."""
    parts = key.split(".")
    data = load()
    cur: Any = data
    for part in parts[:-1]:
        if not isinstance(cur, dict):
            raise KeyError(key)
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value
    save(data)
