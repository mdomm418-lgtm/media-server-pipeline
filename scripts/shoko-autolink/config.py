from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            key = m.group(1)
            return os.environ.get(key, m.group(0))

        return re.sub(r"\$\{([^}]+)\}", repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path or os.environ.get("SHOKO_AUTOLINK_CONFIG", "/opt/shoko-autolink/config.yaml"))
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    return _expand_env(data)
