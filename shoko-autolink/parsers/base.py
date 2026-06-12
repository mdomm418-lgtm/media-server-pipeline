from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ParseResult:
    season: int | None
    episode: int
    episode_type: str
    confidence: float
    parser_name: str


class FilenameParser(Protocol):
    name: str
    priority: int

    def match(
        self, filename: str, *, folder_name: str, relative_path: str
    ) -> ParseResult | None: ...
