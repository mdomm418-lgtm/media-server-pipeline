from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ReviewWriter:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        *,
        folder: str,
        file_id: int | None,
        relative_path: str,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "folder": folder,
            "file_id": file_id,
            "file": relative_path,
            "reason": reason,
        }
        if extra:
            row.update(extra)
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")
