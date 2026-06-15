from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from anime_lists import AnimeListsDB, ResolveResult


def season_key(folder_name: str, season: int) -> str:
    return f"{folder_name}::S{season:02d}"


class AnidbResolver:
    def __init__(
        self,
        data_dir: Path,
        anime_lists: AnimeListsDB,
        min_confidence: float = 0.85,
    ):
        self.data_dir = data_dir
        self.anime_lists = anime_lists
        self.min_confidence = min_confidence
        self.manual_map = self._load_json("anidb_map.json")
        self.learned_map = self._migrate_learned(self._load_json("learned_map.json"))
        self.episode_overrides = self._load_json("episode_overrides.json")

    def _load_json(self, name: str) -> dict[str, Any]:
        path = self.data_dir / name
        if not path.exists():
            return {}
        with path.open() as f:
            return json.load(f)

    def _migrate_learned(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Folder-only learned keys -> Folder::S01 for backward compatibility."""
        out: dict[str, Any] = {}
        for key, val in raw.items():
            if "::S" in key:
                out[key] = val
            else:
                out[f"{key}::S01"] = val
        return out

    def _save_learned(self) -> None:
        path = self.data_dir / "learned_map.json"
        with path.open("w") as f:
            json.dump(self.learned_map, f, indent=2)

    def resolve(
        self,
        folder_name: str,
        tvdb_id: int | None,
        season: int,
    ) -> ResolveResult | None:
        key = season_key(folder_name, season)
        entry = self.manual_map.get(key)
        source = "manual-map-season"
        if entry is None:
            entry = self.manual_map.get(folder_name)
            source = "manual-map"
        # Skip range-based entries (resolve_candidates handles those)
        if entry and "anidb_id" in entry:
            return ResolveResult(
                int(entry["anidb_id"]), 1.0, source,
                episode_offset=int(entry.get("episode_offset", 0))
            )

        if key in self.learned_map:
            entry = self.learned_map[key]
            return ResolveResult(int(entry["anidb_id"]), 0.95, "learned-season")

        if tvdb_id is not None:
            al = self.anime_lists.resolve(tvdb_id, season)
            if al and al.confidence >= self.min_confidence:
                return al
            if al:
                return al

        return None

    def _manual_candidates(
        self,
        folder_name: str,
        season: int,
        episode: int | None = None,
    ) -> list[ResolveResult]:
        """Return manual map candidates, filtered by episode range if provided.

        Supports two manual map schemas:

        1. Legacy (single season entry):
           {"anidb_id": 14758, "episode_offset": 0}

        2. Range-based (split-cour / multi-AniDB):
           {"ranges": [
             {"start": 75, "end": 86, "anidb_id": 14796, "episode_offset": -74},
             {"start": 87, "end": 97, "anidb_id": 15146, "episode_offset": -86}
           ]}
        """
        key = season_key(folder_name, season)
        entry = self.manual_map.get(key)
        source = "manual-map-season"
        if entry is None:
            entry = self.manual_map.get(folder_name)
            source = "manual-map"
        if entry is None:
            return []

        # Range-based schema: match episode to the correct AniDB cour
        ranges = entry.get("ranges")
        if ranges:
            out: list[ResolveResult] = []
            matched = False
            for r in ranges:
                r_start = int(r.get("start", 0))
                r_end = int(r.get("end", 0))
                aid = int(r["anidb_id"])
                offset = int(r.get("episode_offset", 0))
                if episode is not None:
                    if r_start <= episode <= r_end:
                        out.append(ResolveResult(
                            aid, 1.0, "manual-map-range",
                            episode_offset=offset
                        ))
                        matched = True
                        break
                else:
                    out.append(ResolveResult(
                        aid, 1.0, "manual-map-range",
                        episode_offset=offset
                    ))
                    matched = True
            # If episode didn't match any range, return all ranges as fallback
            if not matched and episode is not None:
                for r in ranges:
                    out.append(ResolveResult(
                        int(r["anidb_id"]), 1.0, "manual-map-range-fallback",
                        episode_offset=int(r.get("episode_offset", 0))
                    ))
            return out

        # Legacy single-entry schema
        if "anidb_id" in entry:
            return [ResolveResult(
                int(entry["anidb_id"]), 1.0, source,
                episode_offset=int(entry.get("episode_offset", 0))
            )]

        return []

    def resolve_candidates(
        self,
        folder_name: str,
        tvdb_id: int | None,
        season: int,
        episode: int | None = None,
    ) -> list[ResolveResult]:
        # Manual map takes priority (confidence 1.0)
        manual = self._manual_candidates(folder_name, season, episode)
        if manual:
            return manual

        primary = self.resolve(folder_name, tvdb_id, season)
        seen: set[int] = set()
        out: list[ResolveResult] = []
        if primary:
            out.append(primary)
            seen.add(primary.anidb_id)
        if tvdb_id is not None:
            for alt in self.anime_lists.resolve_candidates(tvdb_id, season, episode):
                if alt.anidb_id not in seen:
                    out.append(alt)
                    seen.add(alt.anidb_id)
        return out

    def episode_override(self, file_id: int) -> int | None:
        val = self.episode_overrides.get(str(file_id)) or self.episode_overrides.get(file_id)
        if val is None:
            return None
        if isinstance(val, dict):
            return int(val.get("shoko_episode_id", val.get("episode_id")))
        return int(val)

    def learn(
        self,
        folder_name: str,
        anidb_id: int,
        tvdb_id: int | None,
        linked: int,
        season: int = 1,
    ) -> None:
        key = season_key(folder_name, season)
        self.learned_map[key] = {
            "anidb_id": anidb_id,
            "tvdb_id": tvdb_id,
            "source": "cron",
            "linked": linked,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_learned()

    def merge_manual_pins(self, pins: dict[str, Any], overwrite: bool = False) -> dict[str, Any]:
        """Merge bootstrap pins into anidb_map.json; returns diff of new keys."""
        diff: dict[str, Any] = {}
        for key, val in pins.items():
            if not overwrite and key in self.manual_map:
                continue
            if self.manual_map.get(key) != val:
                diff[key] = val
            self.manual_map[key] = val
        if diff:
            path = self.data_dir / "anidb_map.json"
            with path.open("w") as f:
                json.dump(self.manual_map, f, indent=2)
        return diff
