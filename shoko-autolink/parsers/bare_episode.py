from __future__ import annotations

import re

from .base import ParseResult

# Pattern 1: [SubGroup] Series Name - NN [tags].ext
# or         [SubGroup] Series Name NN [tags].ext
# Matches a bracketed group, then series name, optional dash, bare number,
# then optional bracket tags or end-of-string.
_SUBGROUP_BARE_RE = re.compile(
    r"^\[.+?\]\s+.+?"           # [SubGroup] SeriesName
    r"(?:\s+-\s+|\s+)"          # " - " or just whitespace
    r"(?P<episode>\d{1,4})"     # bare episode number (1-4 digits)
    r"(?:\s|\[|$)",             # followed by space, bracket, or end
)

# Pattern 1.5: [Group][Series][NN][tags].ext (no spaces)
_BRACKET_ONLY_RE = re.compile(
    r"^\[[^\]]+\]\[[^\]]+\]\[(?P<episode>\d{1,4})\]",
)

# Pattern 2: Series.Name.ENN.quality.ext  (E followed by number, no S prefix)
_DOT_E_RE = re.compile(
    r"\."                       # preceded by a dot separator
    r"E(?P<episode>\d{1,4})"    # E followed by digits
    r"(?:[\._]|$)",             # followed by dot, underscore, or end
    re.IGNORECASE,
)

# Pattern 3: Series Name - NN text.ext  (dash and number, no brackets)
_DASH_BARE_RE = re.compile(
    r"(?:\s+-\s+|-(?=\d{1,4}\.))" # " - " or "-" right before digits and dot
    r"(?P<episode>\d{1,4})"    # bare episode number
    r"(?:\s|$|\[|\.|_)",       # followed by space, end, bracket, dot, or underscore
)

# Pattern 4: Series Name ENN text.ext  (space + E + number, no brackets, no dots)
_SPACE_E_RE = re.compile(
    r"\s+E(?P<episode>\d{1,4})" # space then E followed by digits
    r"(?:\s|$|\[|\.|_)",        # followed by space, end, bracket, dot, or underscore
    re.IGNORECASE,
)

# Pattern 5: Series Name NN [tags].ext (number immediately preceding a bracket tag)
_BARE_BRACKET_RE = re.compile(
    r"\s+(?P<episode>\d{1,4})\s*\[",
)

# Guard: skip filenames that already contain SxxExx (handled by other parsers)
_SXXEXX_GUARD = re.compile(r"S\d+E\d+", re.IGNORECASE)


class BareEpisodeParser:
    """Parse filenames with bare episode numbers (no SxxExx pattern).

    Matches patterns like:
        [SubGroup] Naruto Shippuuden 148 [1080p BD AV1].mkv
        Classroom.Of.The.Elite.E11.1080p.BluRay.x264-URANiME.mkv
        Kill la Kill - 01 [BD 1080p].mkv
        Fighting Spirit - 25 [BD 1080p].mkv
        To Your Eternity E05 Something.mkv

    Season is derived from the folder path (e.g., "Season 2" -> season 2).
    """

    name = "bare_episode"
    priority = 28

    def match(
        self, filename: str, *, folder_name: str, relative_path: str
    ) -> ParseResult | None:
        # Skip filenames that have SxxExx — those are handled by other parsers
        if _SXXEXX_GUARD.search(filename):
            return None

        episode = self._extract_episode(filename)
        if episode is None:
            return None

        _, folder_season = _season_from_path(relative_path)
        season = folder_season if folder_season is not None else 1
        ep_type = "Special" if season == 0 else "Episode"
        return ParseResult(
            season=season,
            episode=episode,
            episode_type=ep_type,
            confidence=0.7,
            parser_name=self.name,
        )

    @staticmethod
    def _extract_episode(filename: str) -> int | None:
        """Try each pattern in order and return the first match."""
        for pattern in (_BRACKET_ONLY_RE, _SUBGROUP_BARE_RE, _BARE_BRACKET_RE, _DOT_E_RE, _DASH_BARE_RE, _SPACE_E_RE):
            m = pattern.search(filename)
            if m:
                return int(m.group("episode"))
        return None


def _season_from_path(relative_path: str) -> tuple[str, int | None]:
    """Extract series folder and season number from a relative path.

    Mirrors the approach used in anihls.py.
    """
    parts = relative_path.replace("\\", "/").split("/")
    for i, part in enumerate(parts):
        if part.lower().startswith("season "):
            try:
                return parts[0] if parts else "", int(part.split()[-1])
            except ValueError:
                return parts[0] if parts else "", None
    return parts[0] if parts else "", None
