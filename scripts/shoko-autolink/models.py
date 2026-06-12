from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

SEASON_DIR_RE = re.compile(r"^Season\s+(\d+)$", re.I)
SPECIALS_DIR = "specials"


@dataclass(frozen=True)
class VideoFile:
    file_id: int
    relative_path: str
    full_path: str
    is_accessible: bool

    @classmethod
    def from_shoko(cls, row: dict) -> VideoFile | None:
        locs = row.get("Locations") or []
        if not locs:
            return None
        loc = locs[0]
        return cls(
            file_id=int(row["ID"]),
            relative_path=loc.get("RelativePath", ""),
            full_path=loc.get("RelativePath", ""),
            is_accessible=bool(loc.get("IsAccessible", True)),
        )


@dataclass(frozen=True)
class ParsedFile:
    season: int
    episode: int
    episode_type: str
    confidence: float
    parser_name: str


@dataclass
class SeriesBatch:
    folder_name: str
    files: list[VideoFile] = field(default_factory=list)
    tvdb_id: int | None = None
    sonarr_series_id: int | None = None
    anidb_id: int | None = None
    shoko_series_id: int | None = None
    resolve_confidence: float = 0.0
    resolve_source: str = ""


@dataclass
class EpisodeMap:
    by_key: dict[tuple[str, int], int]
    by_tvdb: dict[int, int]


@dataclass
class LinkResult:
    file_id: int
    relative_path: str
    success: bool
    shoko_episode_id: int | None = None
    reason: str = ""


def parse_series_folder(shoko_path: str, import_prefix: str = "/mnt/anime") -> tuple[str, int | None]:
    """From /mnt/anime/Show Name/Season 1/file.mkv -> (Show Name, season)."""
    p = shoko_path
    if p.startswith(import_prefix):
        p = p[len(import_prefix) :].lstrip("/")
    parts = Path(p).parts
    if len(parts) < 2:
        return parts[0] if parts else "", None
    series = parts[0]
    season_dir = parts[1]
    if season_dir.lower() == SPECIALS_DIR:
        return series, 0
    m = SEASON_DIR_RE.match(season_dir)
    season = int(m.group(1)) if m else None
    return series, season
