#!/usr/bin/env python3
"""
Library sanitizer for Sonarr + NzbDAV symlink setups.

Detects and optionally fixes:
  - orphan_encrypted: hash-named NZB symlinks not tracked by Sonarr
  - false_multi: one episodefile linked to multiple episodes with short runtime
  - orphan_unparsed: video files without SxxExx not in Sonarr

Usage:
  sanitize_library.py --dry-run
  sanitize_library.py --execute --fix encrypted_orphans
  sanitize_library.py --execute --fix false_multi --series "Death Parade"
  sanitize_library.py --execute --fix all
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

SONARR_URL = os.environ.get("SONARR_URL", "http://localhost:8989/api/v3")
SONARR_KEY = os.environ.get("SONARR_API_KEY")
HEADERS = {"X-Api-Key": SONARR_KEY}
TIMEOUT = 30

LIBRARY_ROOTS = ["/mnt/media/anime", "/mnt/media/tv"]
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v"}
ENC_NAME_RE = re.compile(r"^[A-Za-z0-9]{32}\.[Mm][Kk][Vv]$")
EPISODE_RE = re.compile(r"[Ss](\d+)[Ee](\d+)", re.IGNORECASE)
BATCH_TITLE_RE = re.compile(
    r"complete\.series|full\.season|\.batch\.|\.pseudo\b|\[batch\]|"
    r"01-12|01-24|season\.pack",
    re.IGNORECASE,
)
REPORT_DIR = "/home/admin/dedup-reports"
# Runtime threshold: if one file serves N>1 episodes and runtime < this, it's false multi
FALSE_MULTI_MAX_RUNTIME_MIN = 35


@dataclass
class Issue:
    kind: str
    series: str
    series_id: Optional[int]
    path: str
    detail: str
    episode_file_id: Optional[int] = None
    episode_count: int = 0


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [sanitize] {msg}", flush=True)


def container_to_host(path: str) -> str:
    if path.startswith("/data/"):
        return "/mnt/media" + path[5:]
    return path


def build_sonarr_maps():
    """Returns (series_by_path, known_basenames_by_path, episodefiles_by_series_id)."""
    series_list = requests.get(f"{SONARR_URL}/series", headers=HEADERS, timeout=TIMEOUT).json()
    series_by_path: dict[str, dict] = {}
    known_by_path: dict[str, set[str]] = {}
    efiles_by_sid: dict[int, list[dict]] = {}

    for s in series_list:
        host = container_to_host(s["path"])
        series_by_path[host] = s
        efs = requests.get(
            f"{SONARR_URL}/episodefile",
            params={"seriesId": s["id"]},
            headers=HEADERS,
            timeout=TIMEOUT,
        ).json()
        efiles_by_sid[s["id"]] = efs
        known_by_path[host] = {os.path.basename(ef["path"]) for ef in efs}

    return series_by_path, known_by_path, efiles_by_sid


def parse_runtime_minutes(ef: dict) -> Optional[float]:
    mi = ef.get("mediaInfo") or {}
    rt = mi.get("runTime") or ""
    if not rt:
        return None
    parts = rt.split(":")
    try:
        if len(parts) == 3:
            h, m, s = map(int, parts)
            return h * 60 + m + s / 60
        if len(parts) == 2:
            m, s = map(int, parts)
            return m + s / 60
    except ValueError:
        pass
    return None


def scan_orphans(
    series_filter: Optional[list[str]],
    known_by_path: dict[str, set[str]],
    series_by_path: dict[str, dict],
) -> list[Issue]:
    issues: list[Issue] = []
    filters = [f.lower() for f in series_filter] if series_filter else None

    for root in LIBRARY_ROOTS:
        if not os.path.isdir(root):
            continue
        for series_name in os.listdir(root):
            if filters and not any(f in series_name.lower() for f in filters):
                continue
            sp = os.path.join(root, series_name)
            if not os.path.isdir(sp):
                continue
            known = known_by_path.get(sp, set())
            sinfo = series_by_path.get(sp, {})
            sid = sinfo.get("id")

            for dirpath, _, filenames in os.walk(sp):
                for fname in filenames:
                    ext = os.path.splitext(fname)[1].lower()
                    if ext not in VIDEO_EXTENSIONS:
                        continue
                    if fname in known:
                        continue
                    full = os.path.join(dirpath, fname)
                    rel = os.path.relpath(full, sp)
                    if ENC_NAME_RE.match(fname):
                        issues.append(
                            Issue(
                                kind="orphan_encrypted",
                                series=series_name,
                                series_id=sid,
                                path=full,
                                detail=f"hash-named orphan: {rel}",
                            )
                        )
                    elif not EPISODE_RE.search(fname):
                        issues.append(
                            Issue(
                                kind="orphan_unparsed",
                                series=series_name,
                                series_id=sid,
                                path=full,
                                detail=f"no SxxExx in filename: {rel}",
                            )
                        )
    return issues


def episodes_per_file_id(series_id: int) -> dict[int, list[dict]]:
    """Map episodeFileId -> episodes (reliable; episodeFileId query param is broken)."""
    r = requests.get(
        f"{SONARR_URL}/episode",
        params={"seriesId": series_id},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    by_ef: dict[int, list[dict]] = defaultdict(list)
    for ep in r.json():
        efid = ep.get("episodeFileId")
        if efid:
            by_ef[efid].append(ep)
    return by_ef


def scan_false_multi(
    series_filter: Optional[list[str]],
    efiles_by_sid: dict[int, list[dict]],
    series_by_path: dict[str, dict],
) -> list[Issue]:
    issues: list[Issue] = []
    filters = [f.lower() for f in series_filter] if series_filter else None
    id_to_series = {}
    for host, s in series_by_path.items():
        id_to_series[s["id"]] = {**s, "host_path": host}

    for sid, efs in efiles_by_sid.items():
        s = id_to_series.get(sid)
        if not s:
            continue
        title = s["title"]
        if filters and not any(f in title.lower() for f in filters):
            continue

        ep_by_ef = episodes_per_file_id(sid)

        for ef in efs:
            linked = ep_by_ef.get(ef["id"], [])
            ep_ids = [e["id"] for e in linked]
            if not ep_ids:
                ep_ids = ef.get("episodeIds") or []
            if len(ep_ids) <= 1:
                continue
            rt = parse_runtime_minutes(ef)
            basename = os.path.basename(ef["path"])
            if rt is not None and rt <= FALSE_MULTI_MAX_RUNTIME_MIN:
                issues.append(
                    Issue(
                        kind="false_multi",
                        series=title,
                        series_id=sid,
                        path=container_to_host(ef["path"]),
                        detail=(
                            f"1 file ({rt:.0f}min) linked to {len(ep_ids)} episodes: "
                            f"{basename[:70]}"
                        ),
                        episode_file_id=ef["id"],
                        episode_count=len(ep_ids),
                    )
                )
            elif BATCH_TITLE_RE.search(basename) and len(ep_ids) > 1:
                issues.append(
                    Issue(
                        kind="false_multi",
                        series=title,
                        series_id=sid,
                        path=container_to_host(ef["path"]),
                        detail=(
                            f"batch-named file linked to {len(ep_ids)} episodes: "
                            f"{basename[:70]}"
                        ),
                        episode_file_id=ef["id"],
                        episode_count=len(ep_ids),
                    )
                )
    return issues


def delete_file(path: str) -> None:
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        raise IsADirectoryError(path)


def fix_encrypted_orphans(issues: list[Issue], execute: bool) -> int:
    count = 0
    for issue in issues:
        if issue.kind != "orphan_encrypted":
            continue
        if not any(issue.path.startswith(r) for r in LIBRARY_ROOTS):
            log(f"SKIP outside library: {issue.path}")
            continue
        if execute:
            try:
                delete_file(issue.path)
                log(f"DELETED {issue.series}: {os.path.basename(issue.path)}")
                count += 1
            except OSError as e:
                log(f"ERROR deleting {issue.path}: {e}")
        else:
            log(f"DELETE {issue.series}: {os.path.basename(issue.path)}")
            count += 1
    return count


def fix_false_multi(issues: list[Issue], execute: bool) -> set[int]:
    rescanned: set[int] = set()
    for issue in issues:
        if issue.kind != "false_multi" or not issue.episode_file_id:
            continue
        if execute:
            if os.path.exists(issue.path):
                try:
                    delete_file(issue.path)
                    log(f"DELETED file: {os.path.basename(issue.path)}")
                except OSError as e:
                    log(f"ERROR deleting file: {e}")
            try:
                r = requests.delete(
                    f"{SONARR_URL}/episodefile/{issue.episode_file_id}",
                    headers=HEADERS,
                    params={"deleteFiles": "false"},
                    timeout=TIMEOUT,
                )
                r.raise_for_status()
                log(f"Removed Sonarr episodefile {issue.episode_file_id} for {issue.series}")
            except Exception as e:
                log(f"ERROR removing episodefile: {e}")
            if issue.series_id:
                try:
                    requests.post(
                        f"{SONARR_URL}/command",
                        headers=HEADERS,
                        json={"name": "SeriesSearch", "seriesId": issue.series_id},
                        timeout=TIMEOUT,
                    ).raise_for_status()
                    log(f"Queued SeriesSearch for {issue.series}")
                    rescanned.add(issue.series_id)
                except Exception as e:
                    log(f"ERROR SeriesSearch: {e}")
        else:
            log(
                f"FIX false_multi {issue.series}: drop epfile {issue.episode_file_id}, "
                f"delete {os.path.basename(issue.path)}, SeriesSearch"
            )
    return rescanned


def trigger_rescan(series_ids: set[int]) -> None:
    for sid in sorted(series_ids):
        try:
            requests.post(
                f"{SONARR_URL}/command",
                headers=HEADERS,
                json={"name": "RescanSeries", "seriesId": sid},
                timeout=TIMEOUT,
            ).raise_for_status()
            log(f"Queued RescanSeries for seriesId={sid}")
        except Exception as e:
            log(f"RescanSeries failed {sid}: {e}")


def queue_episode_search(series_id: int, season: int, episode: int) -> None:
    try:
        eps = requests.get(
            f"{SONARR_URL}/episode",
            params={"seriesId": series_id},
            headers=HEADERS,
            timeout=TIMEOUT,
        ).json()
        ep = next(
            (e for e in eps if e["seasonNumber"] == season and e["episodeNumber"] == episode),
            None,
        )
        if not ep:
            return
        requests.post(
            f"{SONARR_URL}/command",
            headers=HEADERS,
            json={
                "name": "EpisodeSearch",
                "episodeIds": [ep["id"]],
            },
            timeout=TIMEOUT,
        ).raise_for_status()
        log(f"Queued EpisodeSearch S{season:02d}E{episode:02d}")
    except Exception as e:
        log(f"EpisodeSearch failed: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sanitize Sonarr library orphans and false imports")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Default; report only")
    parser.add_argument("--series", action="append", help="Limit to series title(s)")
    parser.add_argument(
        "--fix",
        choices=["encrypted_orphans", "false_multi", "all"],
        default="all",
        help="Which fixes to apply",
    )
    parser.add_argument("--report", help="JSON report path")
    args = parser.parse_args()

    execute = args.execute
    if not execute:
        log("DRY RUN — pass --execute to apply fixes")

    log("Building Sonarr index...")
    series_by_path, known_by_path, efiles_by_sid = build_sonarr_maps()

    orphans = scan_orphans(args.series, known_by_path, series_by_path)
    false_multi = scan_false_multi(args.series, efiles_by_sid, series_by_path)
    all_issues = orphans + false_multi

    by_kind: dict[str, int] = defaultdict(int)
    for i in all_issues:
        by_kind[i.kind] += 1

    log("─── Scan summary ───")
    for kind, n in sorted(by_kind.items()):
        log(f"  {kind}: {n}")

    for issue in all_issues[:50]:
        log(f"  [{issue.kind}] {issue.series}: {issue.detail[:90]}")
    if len(all_issues) > 50:
        log(f"  ... and {len(all_issues) - 50} more")

    rescanned: set[int] = set()
    if args.fix in ("encrypted_orphans", "all"):
        fix_encrypted_orphans(orphans, execute)
    if args.fix in ("false_multi", "all"):
        rescanned |= fix_false_multi(false_multi, execute)

    if execute and rescanned:
        trigger_rescan(rescanned)

    report_path = args.report or os.path.join(
        REPORT_DIR,
        f"sanitize-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json",
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "execute": execute,
        "fix": args.fix,
        "counts": dict(by_kind),
        "issues": [asdict(i) for i in all_issues],
    }
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    log(f"Report: {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
