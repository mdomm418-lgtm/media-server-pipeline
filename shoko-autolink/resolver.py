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
        if key in self.manual_map:
            entry = self.manual_map[key]
            return ResolveResult(
                int(entry["anidb_id"]), 1.0, "manual-map-season",
                episode_offset=int(entry.get("episode_offset", 0))
            )
        if folder_name in self.manual_map:
            entry = self.manual_map[folder_name]
            return ResolveResult(
                int(entry["anidb_id"]), 1.0, "manual-map",
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

    def resolve_candidates(
        self,
        folder_name: str,
        tvdb_id: int | None,
        season: int,
        episode: int | None = None,
    ) -> list[ResolveResult]:
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
