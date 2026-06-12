from __future__ import annotations

import re

from .base import ParseResult

MULTI_EP_RE = re.compile(
    r" - S(?P<season>\d+)E(?P<episode>\d+)-(?P<episode2>\d+) - ",
    re.IGNORECASE,
)


class MultiEpisodeParser:
    """Links first episode of a multi-ep filename (e.g. S01E05-06)."""

    name = "multi_episode"
    priority = 5

    def match(
        self, filename: str, *, folder_name: str, relative_path: str
    ) -> ParseResult | None:
        m = MULTI_EP_RE.search(filename)
        if not m:
            return None
        season = int(m.group("season"))
        episode = int(m.group("episode"))
        ep_type = "Special" if season == 0 else "Episode"
        return ParseResult(
            season=season,
            episode=episode,
            episode_type=ep_type,
            confidence=0.85,
            parser_name=self.name,
        )
