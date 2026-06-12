from __future__ import annotations

import re

from .base import ParseResult

ANIHLS_RE = re.compile(r"(?i)anihls-onepie\.e(?P<episode>\d+)")


class AnihlsParser:
    """Legacy One Piece release names: anihls-onepie.e50.1080p.webrip.mkv"""

    name = "anihls"
    priority = 30

    def match(
        self, filename: str, *, folder_name: str, relative_path: str
    ) -> ParseResult | None:
        m = ANIHLS_RE.search(filename)
        if not m:
            return None
        _, folder_season = _season_from_path(relative_path)
        season = folder_season if folder_season is not None else 1
        episode = int(m.group("episode"))
        return ParseResult(
            season=season,
            episode=episode,
            episode_type="Episode",
            confidence=0.78,
            parser_name=self.name,
        )


def _season_from_path(relative_path: str) -> tuple[str, int | None]:
    parts = relative_path.replace("\\", "/").split("/")
    for i, part in enumerate(parts):
        if part.lower().startswith("season "):
            try:
                return parts[0] if parts else "", int(part.split()[-1])
            except ValueError:
                return parts[0] if parts else "", None
    return parts[0] if parts else "", None
