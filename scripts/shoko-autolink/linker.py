from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from anime_lists import AnimeListsDB, ResolveResult
from models import EpisodeMap, LinkResult, SeriesBatch, VideoFile, parse_series_folder
from parsers import parse_filename
from parsers.sonarr_standard import SONARR_EP_RE
from resolver import AnidbResolver, season_key
from review import ReviewWriter
from shoko_client import ShokoClient
from sonarr_client import SonarrClient


def _is_hash_filename(filename: str) -> bool:
    """Detect opaque/hash filenames like 'DFvu0QSzshH33ABhYlslRJ5HqfdWzAxV.mkv'."""
    stem = Path(filename).stem
    return len(stem) > 16 and stem.isalnum() and not any(c in stem for c in ' -_.')


class _AnidbRunCache:
    def __init__(self) -> None:
        self.refreshed: set[int] = set()
        self.shoko_series: dict[int, int] = {}
        self.ep_maps: dict[int, EpisodeMap] = {}


class Linker:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.behavior = cfg.get("behavior", {})
        self.paths = cfg.get("paths", {})
        self.dry_run = bool(self.behavior.get("dry_run", False))
        self.require_standard = bool(cfg.get("sonarr", {}).get("require_standard_names", True))
        self.min_parse = float(self.behavior.get("min_parse_confidence", 0.6))
        self.min_resolve = float(self.behavior.get("min_resolve_confidence", 0.85))
        self.ban_active = bool(self.behavior.get('anidb_ban_active', False))

        state_dir = Path(self.paths.get("state_dir", "/opt/shoko-autolink/state"))
        data_dir = Path(self.paths.get("data_dir", "/opt/shoko-autolink/data"))
        self.review = ReviewWriter(state_dir / "needs_review.jsonl")
        self.state_path = state_dir / "last_run.json"
        self.import_prefix = cfg.get("paths", {}).get("shoko_import_prefix", "/mnt/anime")

        self.shoko = ShokoClient.from_config(cfg)
        self.sonarr = SonarrClient.from_config(cfg)
        self.anime_lists = AnimeListsDB.from_config(cfg)
        self.resolver = AnidbResolver(data_dir, self.anime_lists, self.min_resolve)
        self._anidb_cache = _AnidbRunCache()
        self._learn_counts: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"linked": 0, "total": 0, "anidb_id": None, "tvdb_id": None, "season": 1}
        )

    def run(self, series_filter: str | None = None) -> dict[str, Any]:
        self.shoko.authenticate()
        
        # Trigger Shoko to scan for new files imported by Sonarr
        self.shoko.trigger_import()
        
        # Auto-detect AniDB HTTP ban status from Shoko
        live_ban = self.shoko.get_anidb_ban_status()
        if live_ban:
            print("Notice: Shoko reports an active AniDB HTTP ban. Enforcing ban_active=True.", file=sys.stderr)
            self.ban_active = True
        elif self.ban_active and not live_ban:
            print("Notice: Config has ban_active=True, but Shoko reports no ban. Automatically lifting ban flag in config.", file=sys.stderr)
            self.ban_active = False
            self._update_config_ban_flag(False)
            
        files = self.shoko.list_unrecognized_files()
        grouped = self._group_files(files, series_filter)

        stats = {
            "unrecognized_total": len(files),
            "series_batches": len(grouped),
            "linked": 0,
            "skipped": 0,
            "review": 0,
            "dry_run": self.dry_run,
        }

        max_series = int(self.behavior.get("max_series_per_run", 100))
        max_files = int(self.behavior.get("max_files_per_run", 500))
        files_processed = 0

        for folder, batch in list(grouped.items())[:max_series]:
            if files_processed >= max_files:
                break
            linked_in_batch = self._process_series_batch(
                batch, stats, max_files - files_processed
            )
            files_processed += min(len(batch.files), max_files)
            if linked_in_batch and self.behavior.get("learn_mappings", True):
                for key, info in list(self._learn_counts.items()):
                    if not key.startswith(folder):
                        continue
                    if info["total"] and info["linked"] / info["total"] >= 0.8:
                        if info["anidb_id"]:
                            self.resolver.learn(
                                folder,
                                int(info["anidb_id"]),
                                info.get("tvdb_id"),
                                int(info["linked"]),
                                int(info.get("season", 1)),
                            )

        stats["finished_at"] = datetime.now(timezone.utc).isoformat()
        self._save_state(stats)
        return stats

    def _group_files(
        self, files: list[VideoFile], series_filter: str | None
    ) -> dict[str, SeriesBatch]:
        grouped: dict[str, SeriesBatch] = defaultdict(lambda: SeriesBatch(folder_name=""))
        for vf in files:
            folder, _ = parse_series_folder(vf.relative_path, self.import_prefix)
            if not folder:
                continue
            if series_filter and folder != series_filter:
                continue
            if grouped[folder].folder_name == "":
                grouped[folder] = SeriesBatch(folder_name=folder)
            grouped[folder].files.append(vf)
        return {k: v for k, v in grouped.items() if v.files}

    def _ensure_anidb_series(self, anidb_id: int) -> tuple[int | None, EpisodeMap | None]:
        if anidb_id in self._anidb_cache.ep_maps:
            return self._anidb_cache.shoko_series[anidb_id], self._anidb_cache.ep_maps[anidb_id]

        if not self.dry_run and anidb_id not in self._anidb_cache.refreshed:
            try:
                self.shoko.refresh_anidb_series(
                    anidb_id,
                    float(self.behavior.get("anidb_refresh_delay_sec", 8)),
                    ban_active=self.ban_active,
                )
            except Exception as e:
                print(f"Warning: Failed to refresh AniDB series {anidb_id}: {e}", file=sys.stderr)
            self._anidb_cache.refreshed.add(anidb_id)

        shoko_series_id = self.shoko.get_shoko_series_id(anidb_id)
        if not shoko_series_id:
            return None, None
        ep_map = self.shoko.get_episode_map(shoko_series_id)
        self._anidb_cache.shoko_series[anidb_id] = shoko_series_id
        self._anidb_cache.ep_maps[anidb_id] = ep_map
        return shoko_series_id, ep_map

    def _process_series_batch(
        self, batch: SeriesBatch, stats: dict[str, Any], file_budget: int
    ) -> int:
        self._current_batch_files = batch.files
        folder = batch.folder_name
        sonarr_series = self.sonarr.get_series_by_folder(folder)
        if not sonarr_series:
            for vf in batch.files[:file_budget]:
                self.review.append(
                    folder=folder,
                    file_id=vf.file_id,
                    relative_path=vf.relative_path,
                    reason="sonarr_series_not_found",
                )
                stats["review"] += 1
            return 0

        batch.sonarr_series_id = sonarr_series["id"]
        batch.tvdb_id = sonarr_series.get("tvdbId")
        series_type = sonarr_series.get("seriesType", "anime")

        linked = 0
        for vf in batch.files[:file_budget]:
            result = self._process_file(vf, folder, batch, series_type, sonarr_series)
            if result.success:
                linked += 1
                stats["linked"] += 1
            elif result.reason == "already_linked":
                stats["skipped"] += 1
            else:
                stats["review"] += 1
        return linked

    def _shoko_episode_number(
        self,
        sonarr_ep: dict[str, Any],
        season: int,
        series_type: str,
        resolve_source: str,
    ) -> int:
        ep_num = sonarr_ep.get("episodeNumber")
        abs_num = sonarr_ep.get("absoluteEpisodeNumber")
        use_absolute = "absolute" in resolve_source or (
            series_type == "anime" and season > 0 and ep_num is None and abs_num
        )
        if use_absolute and abs_num:
            return int(abs_num)
        if ep_num is not None and season > 0:
            return int(ep_num)
        if abs_num:
            return int(abs_num)
        return int(ep_num or 0)

    def _match_shoko_episode(
        self,
        ep_map: EpisodeMap,
        sonarr_ep: dict[str, Any],
        ep_type: str,
        ep_num: int,
    ) -> int | None:
        sonarr_tvdb = sonarr_ep.get("tvdbId")
        if sonarr_tvdb is not None:
            hit = ep_map.by_tvdb.get(int(sonarr_tvdb))
            if hit:
                return hit
        hit = ep_map.by_key.get((ep_type, ep_num))
        if hit:
            return hit
        if ep_type == "Episode":
            return ep_map.by_key.get(("Regular", ep_num))
        return None

    def _process_file(
        self,
        vf: VideoFile,
        folder: str,
        batch: SeriesBatch,
        series_type: str,
        sonarr_series: dict[str, Any],
    ) -> LinkResult:
        filename = os.path.basename(vf.relative_path)

        override_ep = self.resolver.episode_override(vf.file_id)
        if override_ep is not None and not self.dry_run:
            try:
                self.shoko.link_file_to_episode(vf.file_id, override_ep)
                return LinkResult(
                    vf.file_id, vf.relative_path, True, shoko_episode_id=override_ep
                )
            except Exception as e:
                self.review.append(
                    folder=folder,
                    file_id=vf.file_id,
                    relative_path=vf.relative_path,
                    reason="link_api_error",
                    extra={"error": str(e)},
                )
                return LinkResult(vf.file_id, vf.relative_path, False, reason="link_api_error")

        if not self.dry_run and self.shoko.file_is_linked(vf.file_id):
            return LinkResult(vf.file_id, vf.relative_path, False, reason="already_linked")

        sonarr_ep = self.sonarr.find_episode_by_relative_path(
            batch.sonarr_series_id, vf.relative_path
        )
        # Hash/opaque filename fallback: match by directory position
        if sonarr_ep is None and _is_hash_filename(filename):
            sonarr_ep = self._match_hash_file_by_position(
                batch.sonarr_series_id, vf, folder
            )
        parsed = None
        if sonarr_ep is None:
            parsed = parse_filename(
                filename, folder_name=folder, relative_path=vf.relative_path
            )
            if not parsed or parsed.confidence < self.min_parse:
                if self.require_standard and not SONARR_EP_RE.search(filename):
                    if parsed is None:
                        self.review.append(
                            folder=folder,
                            file_id=vf.file_id,
                            relative_path=vf.relative_path,
                            reason="sonarr_rename_required",
                        )
                        return LinkResult(
                            vf.file_id, vf.relative_path, False, reason="sonarr_rename_required"
                        )
                self.review.append(
                    folder=folder,
                    file_id=vf.file_id,
                    relative_path=vf.relative_path,
                    reason="parse_failed",
                )
                return LinkResult(vf.file_id, vf.relative_path, False, reason="parse_failed")

            season = parsed.season if parsed.season is not None else 1
            sonarr_ep = self.sonarr.find_episode(
                batch.sonarr_series_id, season, parsed.episode, series_type
            )
            if not sonarr_ep:
                self.review.append(
                    folder=folder,
                    file_id=vf.file_id,
                    relative_path=vf.relative_path,
                    reason="sonarr_episode_not_found",
                    extra={"season": season, "episode": parsed.episode},
                )
                return LinkResult(
                    vf.file_id, vf.relative_path, False, reason="sonarr_episode_not_found"
                )
        else:
            season = int(sonarr_ep.get("seasonNumber") or 1)

        _, folder_season = parse_series_folder(vf.relative_path, self.import_prefix)
        season_for_resolve = (
            folder_season if folder_season is not None else (parsed.season if parsed else season)
        )
        if season_for_resolve is None:
            season_for_resolve = 1

        ep_for_candidates = (
            parsed.episode
            if parsed
            else int(
                sonarr_ep.get("episodeNumber")
                or sonarr_ep.get("absoluteEpisodeNumber")
                or 0
            )
        )
        candidates = self.resolver.resolve_candidates(
            folder, batch.tvdb_id, season_for_resolve, ep_for_candidates
        )
        if not candidates:
            self.review.append(
                folder=folder,
                file_id=vf.file_id,
                relative_path=vf.relative_path,
                reason="resolve_failed",
                extra={"tvdb_id": batch.tvdb_id, "season": season_for_resolve},
            )
            return LinkResult(vf.file_id, vf.relative_path, False, reason="resolve_failed")

        ep_type = "Special" if season == 0 or (parsed and parsed.episode_type == "Special") else "Episode"

        for resolved in candidates:
            if resolved.confidence < self.min_resolve and resolved.source not in (
                "manual-map",
                "manual-map-season",
            ):
                continue
            shoko_series_id, ep_map = self._ensure_anidb_series(resolved.anidb_id)
            if not shoko_series_id or ep_map is None:
                continue

            ep_num = self._shoko_episode_number(
                sonarr_ep, season, series_type, resolved.source
            )
            # Apply episode offset from mapping (translates TVDB ep# to AniDB ep#)
            if resolved.episode_offset:
                ep_num = ep_num + resolved.episode_offset
            shoko_ep_id = self._match_shoko_episode(ep_map, sonarr_ep, ep_type, ep_num)
            if shoko_ep_id is None:
                continue

            learn_key = season_key(folder, season_for_resolve)
            self._learn_counts[learn_key]["total"] += 1
            self._learn_counts[learn_key]["anidb_id"] = resolved.anidb_id
            self._learn_counts[learn_key]["tvdb_id"] = batch.tvdb_id
            self._learn_counts[learn_key]["season"] = season_for_resolve

            if self.dry_run:
                print(
                    f"  [dry-run] link file {vf.file_id} -> ep {shoko_ep_id} "
                    f"(AniDB {resolved.anidb_id}, {filename[:50]}...)"
                )
                self._learn_counts[learn_key]["linked"] += 1
                return LinkResult(
                    vf.file_id, vf.relative_path, True, shoko_episode_id=shoko_ep_id
                )

            try:
                self.shoko.link_file_to_episode(vf.file_id, shoko_ep_id)
                self._learn_counts[learn_key]["linked"] += 1
                return LinkResult(
                    vf.file_id, vf.relative_path, True, shoko_episode_id=shoko_ep_id
                )
            except Exception as e:
                self.review.append(
                    folder=folder,
                    file_id=vf.file_id,
                    relative_path=vf.relative_path,
                    reason="link_api_error",
                    extra={"error": str(e), "anidb_id": resolved.anidb_id},
                )
                return LinkResult(vf.file_id, vf.relative_path, False, reason="link_api_error")

        self.review.append(
            folder=folder,
            file_id=vf.file_id,
            relative_path=vf.relative_path,
            reason="episode_not_found",
            extra={
                "anidb_ids": [c.anidb_id for c in candidates],
                "season": season,
                "episode": ep_for_candidates,
                "ep_type": ep_type,
            },
        )
        return LinkResult(vf.file_id, vf.relative_path, False, reason="episode_not_found")

    def _match_hash_file_by_position(
        self,
        sonarr_series_id: int,
        vf: VideoFile,
        folder: str,
    ) -> dict[str, Any] | None:
        """For hash filenames, try to match by file position in directory."""
        # Get all files in the same season directory
        season_dir = str(Path(vf.relative_path).parent)
        same_dir_files = sorted(
            [f for f in self._current_batch_files
             if str(Path(f.relative_path).parent) == season_dir],
            key=lambda f: f.relative_path,
        )
        if not same_dir_files:
            return None
        # Find this file's index in the sorted directory listing
        idx = next(
            (i for i, f in enumerate(same_dir_files) if f.file_id == vf.file_id),
            None,
        )
        if idx is None:
            return None
        # Get Sonarr episodes for this season
        _, folder_season = parse_series_folder(vf.relative_path, self.import_prefix)
        if folder_season is None:
            return None
        season_eps = sorted(
            [ep for ep in self.sonarr.get_episodes(sonarr_series_id)
             if ep.get('seasonNumber') == folder_season and ep.get('hasFile', False)],
            key=lambda ep: ep.get('episodeNumber', 0),
        )
        if idx < len(season_eps):
            return season_eps[idx]
        return None

    def _save_state(self, stats: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.state_path.open("w") as f:
            json.dump(stats, f, indent=2)

    def clear_review_log(self) -> None:
        if self.review.path.exists():
            self.review.path.write_text("")

    def _update_config_ban_flag(self, value: bool) -> None:
        path_str = self.cfg.get("_config_path")
        if not path_str:
            return
        path = Path(path_str)
        if not path.exists():
            return
        try:
            with open(path, "r") as f:
                content = f.read()
            import re
            if re.search(r"anidb_ban_active:\s*(true|false|True|False)", content):
                new_content = re.sub(r"(anidb_ban_active:\s*)(true|false|True|False)", r"\g<1>" + ("true" if value else "false"), content)
            else:
                return # Don't try to append blindly
            with open(path, "w") as f:
                f.write(new_content)
        except Exception as e:
            print(f"Warning: Failed to update ban flag in config: {e}", file=sys.stderr)
