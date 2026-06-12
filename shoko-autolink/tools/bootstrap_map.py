from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from anime_lists import AnimeListsDB
from config import load_config
from models import VideoFile, parse_series_folder
from resolver import AnidbResolver, season_key
from shoko_client import ShokoClient
from sonarr_client import SonarrClient


def collect_season_groups(
    files: list[VideoFile],
    sonarr: SonarrClient,
    import_prefix: str,
) -> dict[tuple[str, int], dict[str, Any]]:
    """Group unrecognized files by (folder, sonarr_season)."""
    groups: dict[tuple[str, int], dict[str, Any]] = defaultdict(
        lambda: {"files": [], "tvdb_id": None}
    )
    index = sonarr.load_anime_series_index()
    for vf in files:
        folder, folder_season = parse_series_folder(vf.relative_path, import_prefix)
        if not folder:
            continue
        season = folder_season if folder_season is not None else 1
        key = (folder, season)
        groups[key]["files"].append(vf)
        series = index.get(folder)
        if series:
            groups[key]["tvdb_id"] = series.get("tvdbId")
    return groups


def bootstrap_pins(
    cfg: dict[str, Any],
    *,
    dry_run: bool = False,
    min_confidence: float = 0.85,
) -> dict[str, Any]:
    paths = cfg.get("paths", {})
    data_dir = Path(paths.get("data_dir", "/opt/shoko-autolink/data"))
    import_prefix = paths.get("shoko_import_prefix", "/mnt/anime")

    shoko = ShokoClient.from_config(cfg)
    shoko.authenticate()
    files = shoko.list_unrecognized_files()

    sonarr = SonarrClient.from_config(cfg)
    anime_lists = AnimeListsDB.from_config(cfg)
    anime_lists.load()
    resolver = AnidbResolver(data_dir, anime_lists, min_confidence)

    groups = collect_season_groups(files, sonarr, import_prefix)
    proposed: dict[str, Any] = {}
    skipped: list[dict[str, Any]] = []

    for (folder, season), info in sorted(groups.items()):
        tvdb_id = info.get("tvdb_id")
        if tvdb_id is None:
            skipped.append({"folder": folder, "season": season, "reason": "no_sonarr_series"})
            continue
        key = season_key(folder, season)
        if key in resolver.manual_map or folder in resolver.manual_map:
            continue
        resolved = anime_lists.resolve(int(tvdb_id), season)
        if resolved and resolved.confidence >= min_confidence:
            proposed[key] = {
                "anidb_id": resolved.anidb_id,
                "note": f"bootstrap-map ({resolved.source})",
            }
        else:
            skipped.append(
                {
                    "folder": folder,
                    "season": season,
                    "tvdb_id": tvdb_id,
                    "reason": "ambiguous_or_unresolved",
                }
            )

    diff: dict[str, Any] = {}
    if not dry_run and proposed:
        diff = resolver.merge_manual_pins(proposed, overwrite=False)

    return {
        "unrecognized_files": len(files),
        "groups": len(groups),
        "proposed": proposed,
        "applied": diff,
        "skipped": skipped,
        "dry_run": dry_run,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Bootstrap anidb_map.json from unrecognized queue")
    parser.add_argument("--config", default="/opt/shoko-autolink/config.yaml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cfg = load_config(args.config)
    result = bootstrap_pins(cfg, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
