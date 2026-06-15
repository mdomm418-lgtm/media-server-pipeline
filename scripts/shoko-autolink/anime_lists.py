from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests


@dataclass(frozen=True)
class SeasonMapping:
    tvdb_season: int
    anidb_season: int
    start: int | None = None
    end: int | None = None
    offset: int = 0


@dataclass(frozen=True)
class AnimeListEntry:
    anidb_id: int
    tvdb_id: int
    default_tvdb_season: int | str
    name: str
    mappings: tuple[SeasonMapping, ...] = ()


@dataclass(frozen=True)
class ResolveResult:
    anidb_id: int
    confidence: float
    source: str
    entry: AnimeListEntry | None = None
    episode_offset: int = 0


def _parse_season(raw: str | None) -> int | str:
    if raw is None:
        return 1
    if raw.lower() == "a":
        return "a"
    try:
        return int(raw)
    except ValueError:
        return raw


def _parse_mappings(node: ET.Element) -> tuple[SeasonMapping, ...]:
    ml = node.find("mapping-list")
    if ml is None:
        return ()
    out: list[SeasonMapping] = []
    for m in ml.findall("mapping"):
        try:
            tvdb_s = int(m.get("tvdbseason", "0"))
            anidb_s = int(m.get("anidbseason", "0"))
        except ValueError:
            continue
        start = m.get("start")
        end = m.get("end")
        offset = m.get("offset")
        out.append(
            SeasonMapping(
                tvdb_season=tvdb_s,
                anidb_season=anidb_s,
                start=int(start) if start else None,
                end=int(end) if end else None,
                offset=int(offset) if offset else 0,
            )
        )
    return tuple(out)


def _match_via_mappings(entries: list[AnimeListEntry], season: int) -> tuple[AnimeListEntry | None, SeasonMapping | None]:
    for entry in entries:
        for m in entry.mappings:
            if m.tvdb_season != season:
                continue
            return entry, m
    return None, None


class AnimeListsDB:
    def __init__(
        self,
        cache_path: str,
        primary_url: str,
        refresh_days: int = 7,
        fallback_url: str | None = None,
        fallback_cache_path: str | None = None,
    ):
        self.cache_path = Path(cache_path)
        self.primary_url = primary_url
        self.refresh_days = refresh_days
        self.fallback_url = fallback_url
        self.fallback_cache_path = (
            Path(fallback_cache_path)
            if fallback_cache_path
            else self.cache_path.parent / "anime-list-full.json"
        )
        self._by_tvdb: dict[int, list[AnimeListEntry]] | None = None
        self._fribb_by_tvdb: dict[int, list[AnimeListEntry]] | None = None

    def ensure_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        stale = True
        if self.cache_path.exists():
            age_days = (time.time() - self.cache_path.stat().st_mtime) / 86400
            stale = age_days > self.refresh_days
        if stale or not self.cache_path.exists() or self.cache_path.stat().st_size == 0:
            r = requests.get(self.primary_url, timeout=120)
            r.raise_for_status()
            self.cache_path.write_bytes(r.content)

    def _load_xml_entries(self, path: Path) -> dict[int, list[AnimeListEntry]]:
        tree = ET.parse(path)
        root = tree.getroot()
        by_tvdb: dict[int, list[AnimeListEntry]] = {}
        for node in root.findall("anime"):
            tvdb_raw = node.get("tvdbid")
            anidb_raw = node.get("anidbid")
            if not tvdb_raw or not anidb_raw or tvdb_raw == "movie":
                continue
            try:
                tvdb_id = int(tvdb_raw)
                anidb_id = int(anidb_raw)
            except ValueError:
                continue
            season = _parse_season(node.get("defaulttvdbseason"))
            if isinstance(season, str) and season != "a":
                continue
            name_el = node.find("name")
            name = name_el.text if name_el is not None and name_el.text else ""
            entry = AnimeListEntry(
                anidb_id, tvdb_id, season, name, _parse_mappings(node)
            )
            by_tvdb.setdefault(tvdb_id, []).append(entry)
        return by_tvdb

    def load(self) -> None:
        self.ensure_cache()
        self._by_tvdb = self._load_xml_entries(self.cache_path)

    def _ensure_fribb(self) -> dict[int, list[AnimeListEntry]]:
        if self._fribb_by_tvdb is not None:
            return self._fribb_by_tvdb
        if not self.fallback_url:
            self._fribb_by_tvdb = {}
            return self._fribb_by_tvdb
        self.fallback_cache_path.parent.mkdir(parents=True, exist_ok=True)
        stale = True
        if self.fallback_cache_path.exists():
            age_days = (time.time() - self.fallback_cache_path.stat().st_mtime) / 86400
            stale = age_days > self.refresh_days
        if stale or self.fallback_cache_path.stat().st_size == 0:
            r = requests.get(self.fallback_url, timeout=120)
            r.raise_for_status()
            self.fallback_cache_path.write_bytes(r.content)
        data = json.loads(self.fallback_cache_path.read_text())
        by_tvdb: dict[int, list[AnimeListEntry]] = {}
        for row in data if isinstance(data, list) else data.get("data", []):
            tvdb_id = row.get("tvdb_id") or row.get("tvdbId")
            anidb_id = row.get("anidb_id") or row.get("anidbId")
            if tvdb_id is None or anidb_id is None:
                continue
            season = _parse_season(str(row.get("default_tvdb_season", row.get("defaulttvdbseason", "1"))))
            name = row.get("name", "")
            entry = AnimeListEntry(int(anidb_id), int(tvdb_id), season, name, ())
            by_tvdb.setdefault(int(tvdb_id), []).append(entry)
        self._fribb_by_tvdb = by_tvdb
        return self._fribb_by_tvdb

    def entries_for_tvdb(self, tvdb_id: int) -> list[AnimeListEntry]:
        if self._by_tvdb is None:
            self.load()
        entries = list(self._by_tvdb.get(tvdb_id, []))
        if not entries:
            entries = list(self._ensure_fribb().get(tvdb_id, []))
        return entries

    def resolve(self, tvdb_id: int, season: int) -> ResolveResult | None:
        entries = self.entries_for_tvdb(tvdb_id)
        if not entries:
            return None

        int_matches = [e for e in entries if e.default_tvdb_season == season]
        if len(int_matches) == 1:
            e = int_matches[0]
            # Check if there's a mapping with offset for this season
            offset = 0
            for m in e.mappings:
                if m.tvdb_season == season:
                    offset = m.offset
                    break
            return ResolveResult(e.anidb_id, 0.9, "anime-lists", e, episode_offset=offset)
        if len(int_matches) > 1:
            mapped, matched_mapping = _match_via_mappings(int_matches, season)
            if mapped:
                offset = matched_mapping.offset if matched_mapping else 0
                return ResolveResult(mapped.anidb_id, 0.9, "anime-lists-mapping", mapped, episode_offset=offset)
            return None

        mapped, matched_mapping = _match_via_mappings(entries, season)
        if mapped:
            offset = matched_mapping.offset if matched_mapping else 0
            return ResolveResult(mapped.anidb_id, 0.9, "anime-lists-mapping", mapped, episode_offset=offset)

        absolute = [e for e in entries if e.default_tvdb_season == "a"]
        if len(absolute) == 1 and season >= 1:
            e = absolute[0]
            return ResolveResult(e.anidb_id, 0.9, "anime-lists-absolute", e)

        if len(entries) == 1:
            e = entries[0]
            return ResolveResult(e.anidb_id, 0.75, "anime-lists-default", e)
        return None

    def resolve_candidates(self, tvdb_id: int, season: int, episode: int | None = None) -> list[ResolveResult]:
        """Ordered AniDB candidates for a TVDB season (for episode retry).

        When anime-lists has multiple same-season entries that resolve() can't
        distinguish, they are included here with episode-range-aware confidence.
        """
        primary = self.resolve(tvdb_id, season)
        seen: set[int] = set()
        out: list[ResolveResult] = []
        if primary:
            out.append(primary)
            seen.add(primary.anidb_id)

        entries = self.entries_for_tvdb(tvdb_id)

        # Same-season duplicates that resolve() couldn't pick between.
        # Include them as candidates, preferring entries whose mapping
        # start/end range covers the requested episode.
        same_season = [
            e for e in entries
            if isinstance(e.default_tvdb_season, int)
            and e.default_tvdb_season == season
            and e.anidb_id not in seen
        ]
        for e in same_season:
            offset = 0
            confidence = 0.8
            source = "anime-lists-same-season"
            if episode is not None:
                for m in e.mappings:
                    if m.tvdb_season == season:
                        offset = m.offset
                        if m.start is not None and m.start <= episode:
                            if m.end is None or m.end >= episode:
                                confidence = 0.85
                                source = "anime-lists-same-season-range"
                                break
            out.append(ResolveResult(e.anidb_id, confidence, source, e, episode_offset=offset))
            seen.add(e.anidb_id)

        # Higher-season alt entries (for series where AniDB splits cours into
        # separate seasons while TVDB keeps one continuous season).
        for e in entries:
            if e.anidb_id in seen:
                continue
            if isinstance(e.default_tvdb_season, int) and e.default_tvdb_season != season:
                if episode is not None and e.default_tvdb_season > season:
                    out.append(ResolveResult(e.anidb_id, 0.8, "anime-lists-alt-season", e))
                    seen.add(e.anidb_id)
        return out

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> AnimeListsDB:
        al = cfg["anime_lists"]
        cache = Path(al["cache_path"])
        return cls(
            al["cache_path"],
            al["primary_url"],
            al.get("refresh_interval_days", 7),
            fallback_url=al.get("fallback_url"),
            fallback_cache_path=str(cache.parent / "anime-list-full.json"),
        )
