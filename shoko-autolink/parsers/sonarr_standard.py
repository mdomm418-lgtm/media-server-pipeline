from __future__ import annotations

import re

from .base import ParseResult

SONARR_EP_RE = re.compile(
    r" - S(?P<season>\d+)E(?P<episode>\d+) - ",
    re.IGNORECASE,
)


class SonarrStandardParser:
    name = "sonarr_standard"
    priority = 10

    def match(
        self, filename: str, *, folder_name: str, relative_path: str
    ) -> ParseResult | None:
        m = SONARR_EP_RE.search(filename)
        if not m:
            return None
        season = int(m.group("season"))
        episode = int(m.group("episode"))
        ep_type = "Special" if season == 0 else "Episode"
        return ParseResult(
            season=season,
            episode=episode,
            episode_type=ep_type,
            confidence=0.95,
            parser_name=self.name,
        )
