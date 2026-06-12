from __future__ import annotations

import re

from .base import ParseResult

S00_RE = re.compile(r" - S00E(?P<episode>\d+) - ", re.IGNORECASE)


class SpecialsParser:
    name = "specials"
    priority = 20

    def match(
        self, filename: str, *, folder_name: str, relative_path: str
    ) -> ParseResult | None:
        m = S00_RE.search(filename)
        if not m:
            return None
        return ParseResult(
            season=0,
            episode=int(m.group("episode")),
            episode_type="Special",
            confidence=0.9,
            parser_name=self.name,
        )
