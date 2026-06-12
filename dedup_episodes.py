#!/usr/bin/env python3
"""
Episode deduplication for Sonarr-managed libraries (symlink / NzbDAV).

Detects multiple video files for the same SxxExx within a series folder,
keeps the file Sonarr tracks as canonical, and removes orphans/superseded copies.

Usage:
  dedup_episodes.py --dry-run
  dedup_episodes.py --execute --series "Cowboy Bebop"
  dedup_episodes.py --execute --root all
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

# ── Configuration ─────────────────────────────────────────────────────────────
SONARR_URL = os.environ.get("SONARR_URL", "http://localhost:8989/api/v3")
SONARR_KEY = os.environ.get("SONARR_API_KEY")
HEADERS = {"X-Api-Key": SONARR_KEY}
TIMEOUT = 30

LIBRARY_ROOTS = [
    {"path": "/mnt/media/anime", "label": "anime"},
    {"path": "/mnt/media/tv", "label": "tv"},
]
CONTAINER_PREFIX = "/data"
HOST_PREFIX = "/mnt/media"
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v"}
EPISODE_RE = re.compile(r"[Ss](\d+)[Ee](\d+)", re.IGNORECASE)
DUPLICATE_SUFFIX_RE = re.compile(r" \(\d+\)(?=\.[^.]+$)")
EXTRA_RE = re.compile(
    r"NCED|NCOP|NC\.?ED|NC\.?OP|Creditless|Textless|"
    r"Clean\s*(Opening|Ending)|PV\d|Trailer|CM\d|Menu|Bonus",
    re.IGNORECASE,
)
REPORT_DIR = "/home/admin/dedup-reports"


@dataclass
class FileInfo:
    path: str
    basename: str
    season: int
    episode: int
    size: int
    mtime: float
    is_symlink: bool
    exists: bool
    symlink_target: Optional[str] = None


@dataclass
class DedupAction:
    series: str
    season: int
    episode: int
    action: str  # delete | keep | skip
    path: str
    reason: str
    keeper_path: Optional[str] = None
    episode_file_id: Optional[int] = None


@dataclass
class SeriesIndex:
    series_id: int
    title: str
    host_path: str
    canonical: dict[tuple[int, int], dict] = field(default_factory=dict)
    # (season, episode) -> {relativePath, basename, episodeFileId, quality}


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [dedup] {msg}", flush=True)


def container_to_host(path: str) -> str:
    if path.startswith(CONTAINER_PREFIX + "/"):
        return HOST_PREFIX + path[len(CONTAINER_PREFIX) :]
    return path


def parse_episode(filename: str) -> Optional[tuple[int, int]]:
    m = EPISODE_RE.search(filename)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def stat_file(full_path: str) -> FileInfo:
    basename = os.path.basename(full_path)
    ep = parse_episode(basename)
    if not ep:
        raise ValueError(f"No SxxExx in {basename}")
    season, episode = ep
    try:
        st = os.lstat(full_path)
        is_symlink = os.path.islink(full_path)
        exists = os.path.exists(full_path)
        target = os.readlink(full_path) if is_symlink else None
        size = st.st_size if exists else 0
        mtime = st.st_mtime
    except OSError:
        is_symlink, exists, target, size, mtime = False, False, None, 0, 0
    return FileInfo(
        path=full_path,
        basename=basename,
        season=season,
        episode=episode,
        size=size,
        mtime=mtime,
        is_symlink=is_symlink,
        exists=exists,
        symlink_target=target,
    )


def scan_series_folder(series_path: str, series_title: str) -> dict[tuple[int, int], list[FileInfo]]:
    groups: dict[tuple[int, int], list[FileInfo]] = defaultdict(list)
    if not os.path.isdir(series_path):
        return groups
    for dirpath, _, files in os.walk(series_path):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in VIDEO_EXTENSIONS:
                continue
            if EXTRA_RE.search(fname):
                continue
            full = os.path.join(dirpath, fname)
            try:
                info = stat_file(full)
            except ValueError:
                continue
            groups[(info.season, info.episode)].append(info)
    return {k: v for k, v in groups.items() if len(v) > 1}


def build_sonarr_index() -> dict[str, SeriesIndex]:
    """Map series title (lower) and host path -> SeriesIndex with canonical files."""
    index: dict[str, SeriesIndex] = {}
    series_list = requests.get(f"{SONARR_URL}/series", headers=HEADERS, timeout=TIMEOUT).json()
    for s in series_list:
        host_path = container_to_host(s["path"])
        title = s["title"]
        sid = s["id"]
        episodes = requests.get(
            f"{SONARR_URL}/episode", params={"seriesId": sid}, headers=HEADERS, timeout=TIMEOUT
        ).json()
        ep_map = {e["id"]: (e["seasonNumber"], e["episodeNumber"]) for e in episodes}
        efiles = requests.get(
            f"{SONARR_URL}/episodefile", params={"seriesId": sid}, headers=HEADERS, timeout=TIMEOUT
        ).json()
        canonical: dict[tuple[int, int], dict] = {}
        for ef in efiles:
            rel = ef["relativePath"]
            basename = os.path.basename(rel)
            host_file = os.path.join(host_path, rel)
            for eid in ef.get("episodeIds", []):
                ep_key = ep_map.get(eid)
                if ep_key:
                    canonical[ep_key] = {
                        "relativePath": rel,
                        "basename": basename,
                        "host_path": host_file,
                        "episodeFileId": ef["id"],
                        "quality": ef.get("quality", {}),
                        "dateAdded": ef.get("dateAdded", ""),
                    }
        si = SeriesIndex(series_id=sid, title=title, host_path=host_path, canonical=canonical)
        index[title.lower()] = si
        index[host_path.lower()] = si
    return index


def normalize_duplicate_basename(name: str) -> str:
    return DUPLICATE_SUFFIX_RE.sub("", name)


def choose_keeper(
    files: list[FileInfo],
    canonical: Optional[dict],
) -> tuple[FileInfo, list[FileInfo], bool]:
    """Return (keeper, losers, canonical_broken)."""
    canonical_broken = False
    if canonical:
        canon_base = canonical["basename"]
        canon_path = canonical["host_path"]
        for f in files:
            if f.basename == canon_base or f.path == canon_path:
                if f.exists:
                    return f, [x for x in files if x.path != f.path], False
                canonical_broken = True
                break

    def dup_score(f: FileInfo) -> tuple:
        has_dup_suffix = bool(re.search(r" \(\d+\)\.", f.basename))
        broken = 0 if f.exists else 1
        return (broken, has_dup_suffix, -f.mtime)

    sorted_files = sorted(files, key=dup_score)
    keeper = sorted_files[0]
    return keeper, [x for x in files if x.path != keeper.path], canonical_broken


def resolve_series(
    series_path: str,
    series_title: str,
    sonarr_index: dict[str, SeriesIndex],
    min_age_seconds: float,
    execute: bool,
) -> list[DedupAction]:
    actions: list[DedupAction] = []
    si = sonarr_index.get(series_title.lower()) or sonarr_index.get(series_path.lower())
    dup_groups = scan_series_folder(series_path, series_title)

    broken_ef_ids: set[int] = set()

    for (season, episode), files in sorted(dup_groups.items()):
        canon = si.canonical.get((season, episode)) if si else None
        keeper, losers, canon_broken = choose_keeper(files, canon)
        if canon_broken and canon:
            broken_ef_ids.add(canon["episodeFileId"])

        actions.append(
            DedupAction(
                series=series_title,
                season=season,
                episode=episode,
                action="keep",
                path=keeper.path,
                reason="sonarr_canonical" if canon and keeper.basename == canon.get("basename") else "heuristic",
                keeper_path=keeper.path,
            )
        )

        for loser in losers:
            reason = "orphan_superseded"
            if not loser.exists:
                reason = "broken_symlink"
            elif canon and loser.basename == canon.get("basename"):
                reason = "unexpected_duplicate_of_canonical"
            elif DUPLICATE_SUFFIX_RE.search(loser.basename):
                reason = "duplicate_suffix_copy"

            age = time.time() - loser.mtime
            if age < min_age_seconds:
                actions.append(
                    DedupAction(
                        series=series_title,
                        season=season,
                        episode=episode,
                        action="skip",
                        path=loser.path,
                        reason=f"too_new_{int(age)}s",
                        keeper_path=keeper.path,
                    )
                )
                continue

            # Safety: only delete under library roots
            if not any(loser.path.startswith(r["path"]) for r in LIBRARY_ROOTS):
                actions.append(
                    DedupAction(
                        series=series_title,
                        season=season,
                        episode=episode,
                        action="skip",
                        path=loser.path,
                        reason="outside_library_root",
                        keeper_path=keeper.path,
                    )
                )
                continue

            if execute:
                try:
                    os.unlink(loser.path)
                    actions.append(
                        DedupAction(
                            series=series_title,
                            season=season,
                            episode=episode,
                            action="deleted",
                            path=loser.path,
                            reason=reason,
                            keeper_path=keeper.path,
                        )
                    )
                except OSError as e:
                    actions.append(
                        DedupAction(
                            series=series_title,
                            season=season,
                            episode=episode,
                            action="error",
                            path=loser.path,
                            reason=str(e),
                            keeper_path=keeper.path,
                        )
                    )
            else:
                actions.append(
                    DedupAction(
                        series=series_title,
                        season=season,
                        episode=episode,
                        action="delete",
                        path=loser.path,
                        reason=reason,
                        keeper_path=keeper.path,
                    )
                )

    if execute and broken_ef_ids:
        for efid in broken_ef_ids:
            remove_broken_episodefile(efid)

    return actions


def find_series_dirs(
    roots: list[dict],
    series_filter: Optional[list[str]],
) -> list[tuple[str, str]]:
    """Return list of (title, host_path)."""
    targets = []
    filters = [s.lower() for s in series_filter] if series_filter else None
    for root in roots:
        base = root["path"]
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            sp = os.path.join(base, name)
            if not os.path.isdir(sp):
                continue
            if filters and name.lower() not in filters and not any(
                f in name.lower() for f in filters
            ):
                continue
            targets.append((name, sp))
    return targets


def remove_broken_episodefile(episode_file_id: int) -> None:
    """Drop a stale Sonarr episodefile record (file already gone or being replaced)."""
    try:
        r = requests.delete(
            f"{SONARR_URL}/episodefile/{episode_file_id}",
            headers=HEADERS,
            params={"deleteFiles": "false"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        log(f"Removed stale episodefile id={episode_file_id}")
    except Exception as e:
        log(f"episodefile delete failed id={episode_file_id}: {e}")


def trigger_rescan(series_ids: set[int]) -> None:
    for sid in sorted(series_ids):
        try:
            r = requests.post(
                f"{SONARR_URL}/command",
                headers=HEADERS,
                json={"name": "RescanSeries", "seriesId": sid},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            log(f"Queued RescanSeries for seriesId={sid}")
        except Exception as e:
            log(f"RescanSeries failed for {sid}: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Deduplicate Sonarr episode files on disk")
    parser.add_argument("--execute", action="store_true", help="Actually delete files (default: dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="Explicit dry-run (default)")
    parser.add_argument("--series", action="append", help="Limit to series title(s)")
    parser.add_argument("--root", choices=["anime", "tv", "all"], default="all")
    parser.add_argument("--min-age-hours", type=float, default=1.0, help="Skip files newer than N hours")
    parser.add_argument("--report", help="JSON report path")
    args = parser.parse_args()

    execute = args.execute and not args.dry_run
    if not execute:
        log("DRY RUN — pass --execute to apply deletions")

    roots = LIBRARY_ROOTS
    if args.root != "all":
        roots = [r for r in LIBRARY_ROOTS if r["label"] == args.root]

    series_filter = args.series
    min_age = args.min_age_hours * 3600

    log("Building Sonarr index...")
    sonarr_index = build_sonarr_index()

    series_dirs = find_series_dirs(roots, series_filter)
    if not series_dirs:
        log("No matching series directories found.")
        return 1

    log(f"Scanning {len(series_dirs)} series: {[t for t, _ in series_dirs]}")

    all_actions: list[DedupAction] = []
    rescanned: set[int] = set()

    for title, path in series_dirs:
        actions = resolve_series(path, title, sonarr_index, min_age, execute)
        all_actions.extend(actions)
        deleted = [a for a in actions if a.action in ("delete", "deleted")]
        if deleted:
            si = sonarr_index.get(title.lower())
            if si:
                rescanned.add(si.series_id)

    # Summary
    counts: dict[str, int] = defaultdict(int)
    for a in all_actions:
        counts[a.action] += 1

    log("─── Summary ───")
    for action, n in sorted(counts.items()):
        log(f"  {action}: {n}")

    for a in all_actions:
        if a.action in ("delete", "deleted", "skip", "error"):
            base = os.path.basename(a.path)
            keeper = os.path.basename(a.keeper_path) if a.keeper_path else "?"
            log(
                f"  [{a.action.upper():7}] {a.series} S{a.season:02d}E{a.episode:02d} "
                f"| {a.reason} | - {base[:55]} | + {keeper[:55]}"
            )

    if execute and rescanned:
        trigger_rescan(rescanned)

    report_path = args.report or os.path.join(
        REPORT_DIR,
        f"dedup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json",
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "execute": execute,
        "series": [t for t, _ in series_dirs],
        "counts": dict(counts),
        "actions": [a.__dict__ for a in all_actions],
    }
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    log(f"Report written to {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
