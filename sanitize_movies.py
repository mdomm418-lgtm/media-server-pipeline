#!/usr/bin/env python3
"""
Fix Radarr/Jellyfin movies stuck with NzbDAV hash filenames.

Detects 32-char hash .mkv symlinks under /mnt/media/movies and either:
  - deletes untracked duplicates when Radarr already has a proper file, or
  - renames tracked hash files to Radarr's standard format via docker exec, then rescans.

Usage:
  sanitize_movies.py --dry-run
  sanitize_movies.py --execute
  sanitize_movies.py --execute --movie "The Crash"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

RADARR_URL = os.environ.get("RADARR_URL", "http://127.0.0.1:7878/api/v3")
RADARR_KEY = os.environ.get("RADARR_API_KEY")
MOVIES_ROOT = os.environ.get("MOVIES_ROOT", "/mnt/media/movies")
RADARR_CONTAINER = os.environ.get("RADARR_CONTAINER", "radarr")
REPORT_DIR = os.environ.get("MOVIE_SANITIZE_REPORT_DIR", "/home/admin/dedup-reports")

HEADERS = {"X-Api-Key": RADARR_KEY}
TIMEOUT = 30
ENC_NAME_RE = re.compile(r"^[A-Za-z0-9]{32}\.[Mm][Kk][Vv]$")
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v"}
ILLEGAL_CHARS_RE = re.compile(r'[<>:"/\\|?*]')


@dataclass
class Issue:
    kind: str
    movie: str
    movie_id: int
    path: str
    detail: str
    action: str


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [movie-sanitize] {msg}", flush=True)


def container_to_host(path: str) -> str:
    if path.startswith("/data/"):
        return "/mnt/media" + path[5:]
    return path


def host_to_container(path: str) -> str:
    if path.startswith("/mnt/media/"):
        return "/data" + path[len("/mnt/media") :]
    return path


def clean_title(title: str) -> str:
    title = ILLEGAL_CHARS_RE.sub("", title)
    return re.sub(r"\s+", " ", title).strip()


def radarr_filename(movie: dict, movie_file: dict) -> str:
    title = clean_title(movie["title"])
    year = movie.get("year") or ""
    quality = (movie_file.get("quality") or {}).get("quality", {}).get("name") or "Unknown"
    ext = os.path.splitext(movie_file.get("relativePath") or ".mkv")[1] or ".mkv"
    return f"{title} ({year}) {quality}{ext}"


def list_video_files(folder_host: str) -> list[str]:
    if not os.path.isdir(folder_host):
        return []
    out: list[str] = []
    for name in os.listdir(folder_host):
        if os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS:
            out.append(name)
    return out


def docker_mv(src_container: str, dest_container: str) -> None:
    subprocess.run(
        ["docker", "exec", RADARR_CONTAINER, "mv", src_container, dest_container],
        check=True,
        timeout=60,
    )


def docker_rm(path_container: str) -> None:
    subprocess.run(
        ["docker", "exec", RADARR_CONTAINER, "rm", "-f", path_container],
        check=True,
        timeout=60,
    )


def rescan_movie(movie_id: int) -> None:
    requests.post(
        f"{RADARR_URL}/command",
        headers=HEADERS,
        json={"name": "RescanMovie", "movieId": movie_id},
        timeout=TIMEOUT,
    ).raise_for_status()


def scan_issues(movie_filter: Optional[list[str]] = None) -> list[Issue]:
    issues: list[Issue] = []
    filters = [f.lower() for f in movie_filter] if movie_filter else None

    movies = requests.get(f"{RADARR_URL}/movie", headers=HEADERS, timeout=TIMEOUT).json()
    for movie in movies:
        title = movie["title"]
        year = movie.get("year")
        label = f"{title} ({year})" if year else title
        if filters and not any(f in label.lower() or f in title.lower() for f in filters):
            continue

        folder_host = container_to_host(movie["path"])
        files = list_video_files(folder_host)
        if not files:
            continue

        tracked_name = None
        mf = movie.get("movieFile") or {}
        if mf.get("relativePath"):
            tracked_name = os.path.basename(mf["relativePath"])

        hash_files = [f for f in files if ENC_NAME_RE.match(f)]
        if not hash_files:
            continue

        proper_files = [f for f in files if not ENC_NAME_RE.match(f)]
        for hash_name in hash_files:
            full_host = os.path.join(folder_host, hash_name)
            if tracked_name and hash_name != tracked_name and tracked_name in proper_files:
                issues.append(
                    Issue(
                        kind="duplicate_hash",
                        movie=label,
                        movie_id=movie["id"],
                        path=full_host,
                        detail=f"extra hash file while Radarr tracks {tracked_name}",
                        action="delete",
                    )
                )
            elif tracked_name == hash_name and mf:
                target = radarr_filename(movie, mf)
                issues.append(
                    Issue(
                        kind="tracked_hash",
                        movie=label,
                        movie_id=movie["id"],
                        path=full_host,
                        detail=f"rename to {target}",
                        action=f"rename:{target}",
                    )
                )
            else:
                issues.append(
                    Issue(
                        kind="untracked_hash",
                        movie=label,
                        movie_id=movie["id"],
                        path=full_host,
                        detail="hash file not tracked by Radarr",
                        action="review",
                    )
                )
    return issues


def apply_fixes(issues: list[Issue], execute: bool) -> int:
    fixed = 0
    rescans: set[int] = set()

    for issue in issues:
        folder_host = os.path.dirname(issue.path)
        folder_container = host_to_container(folder_host)
        basename = os.path.basename(issue.path)
        src_container = f"{folder_container}/{basename}"

        if issue.kind == "duplicate_hash":
            if execute:
                docker_rm(src_container)
                log(f"DELETED duplicate {issue.movie}: {basename}")
                fixed += 1
            else:
                log(f"DELETE duplicate {issue.movie}: {basename}")
                fixed += 1
            continue

        if issue.kind == "tracked_hash":
            target = issue.action.split(":", 1)[1]
            dest_container = f"{folder_container}/{target}"
            if execute:
                if os.path.exists(os.path.join(folder_host, target)):
                    log(f"SKIP rename {issue.movie}: target already exists ({target})")
                    continue
                docker_mv(src_container, dest_container)
                rescans.add(issue.movie_id)
                log(f"RENAMED {issue.movie}: {basename} -> {target}")
                fixed += 1
            else:
                log(f"RENAME {issue.movie}: {basename} -> {target}")
                fixed += 1

    if execute:
        for movie_id in sorted(rescans):
            try:
                rescan_movie(movie_id)
                log(f"Queued RescanMovie for id={movie_id}")
            except Exception as exc:
                log(f"ERROR RescanMovie id={movie_id}: {exc}")
    return fixed


def main() -> int:
    parser = argparse.ArgumentParser(description="Fix hash-named Radarr movie files")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Default")
    parser.add_argument("--movie", action="append", help="Limit to movie title substring")
    parser.add_argument("--report", help="JSON report path")
    args = parser.parse_args()

    execute = args.execute
    if not execute:
        log("DRY RUN — pass --execute to apply fixes")

    issues = scan_issues(args.movie)
    by_kind: dict[str, int] = {}
    for issue in issues:
        by_kind[issue.kind] = by_kind.get(issue.kind, 0) + 1

    log("─── Scan summary ───")
    for kind, count in sorted(by_kind.items()):
        log(f"  {kind}: {count}")
    for issue in issues:
        log(f"  [{issue.kind}] {issue.movie}: {issue.detail}")

    fixed = apply_fixes(issues, execute)

    report_path = args.report or os.path.join(
        REPORT_DIR,
        f"movie-sanitize-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json",
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "execute": execute,
        "counts": by_kind,
        "fixed": fixed,
        "issues": [asdict(i) for i in issues],
    }
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    log(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
