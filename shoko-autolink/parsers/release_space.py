from __future__ import annotations

import re

from .base import ParseResult

RELEASE_SPACE_RE = re.compile(
    r"(?i)(?:^|[.\s])S(?P<season>\d+)E(?P<episode>\d+)(?:[^0-9]|$)",
)


class ReleaseSpaceParser:
    name = "release_space"
    priority = 25

    def match(
        self, filename: str, *, folder_name: str, relative_path: str
    ) -> ParseResult | None:
        m = RELEASE_SPACE_RE.search(filename)
        if not m:
            return None
        season = int(m.group("season"))
        episode = int(m.group("episode"))
        ep_type = "Special" if season == 0 else "Episode"
        return ParseResult(
            season=season,
            episode=episode,
            episode_type=ep_type,
            confidence=0.8,
            parser_name=self.name,
        )
