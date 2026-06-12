#!/usr/bin/env python3
"""
Detect stuck Sonarr searches and unimported NzbDAV completed downloads.

When completed-symlinks backlog grows or search commands stay on "started" too
long, triggers ProcessMonitoredDownloads. If searches cannot be cancelled and
remain stuck, restarts the Sonarr container (rate-limited).

Designed for cron every 15 minutes alongside backlog_fetcher.

Usage:
  sonarr_import_guard.py              # dry-run
  sonarr_import_guard.py --execute
  sonarr_import_guard.py --status
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

SONARR_CONFIG = os.environ.get("SONARR_CONFIG", "/opt/sonarr/config/config.xml")
SONARR_URL = os.environ.get("SONARR_URL", "http://localhost:8989/api/v3")
SONARR_CONTAINER = os.environ.get("SONARR_CONTAINER", "sonarr")
STATE_FILE = os.environ.get(
    "SONARR_IMPORT_GUARD_STATE", "/home/admin/backlog-fetcher/import-guard-state.json"
)
COMPLETED_ROOTS = [
    os.environ.get("COMPLETED_ANIME", "/mnt/nzbdav/completed-symlinks/anime"),
    os.environ.get("COMPLETED_TV", "/mnt/nzbdav/completed-symlinks/tv"),
]
NZBDAV_MOUNT = os.environ.get("NZBDAV_MOUNT", "/mnt/nzbdav")
TIMEOUT = 45

STUCK_SEARCH_MINUTES = int(os.environ.get("STUCK_SEARCH_MINUTES", "90"))
BACKLOG_THRESHOLD = int(os.environ.get("COMPLETED_BACKLOG_THRESHOLD", "40"))
RESTART_COOLDOWN_MINUTES = int(os.environ.get("SONARR_RESTART_COOLDOWN_MINUTES", "60"))
SEARCH_COMMAND_NAMES = frozenset(
    {"SeriesSearch", "SeasonSearch", "EpisodeSearch", "MissingEpisodeSearch"}
)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [import-guard] {msg}", flush=True)


def headers() -> dict[str, str]:
    key = os.environ.get("SONARR_API_KEY")
    if not key:
        key = ET.parse(SONARR_CONFIG).getroot().findtext("ApiKey")
    if not key:
        raise SystemExit(f"No Sonarr API key in {SONARR_CONFIG}")
    return {"X-Api-Key": key, "Content-Type": "application/json"}


def load_state() -> dict[str, Any]:
    path = Path(STATE_FILE)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    path = Path(STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def mount_ok() -> bool:
    try:
        return os.path.ismount(NZBDAV_MOUNT) and bool(os.listdir(NZBDAV_MOUNT))
    except OSError:
        return False


def count_completed_backlog() -> tuple[int, dict[str, int]]:
    counts: dict[str, int] = {}
    total = 0
    for root in COMPLETED_ROOTS:
        path = Path(root)
        if not path.is_dir():
            counts[root] = 0
            continue
        n = sum(1 for p in path.iterdir() if p.is_dir())
        counts[root] = n
        total += n
    return total, counts


def sonarr_online() -> bool:
    try:
        r = requests.get(f"{SONARR_URL}/system/status", headers=headers(), timeout=5)
        return r.status_code == 200
    except requests.RequestException:
        return False


def get_commands() -> list[dict[str, Any]]:
    r = requests.get(f"{SONARR_URL}/command", headers=headers(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def parse_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def stuck_searches(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    stuck: list[dict[str, Any]] = []
    for c in commands:
        if c.get("name") not in SEARCH_COMMAND_NAMES:
            continue
        if c.get("status") != "started":
            continue
        started = parse_utc(c.get("started"))
        if not started:
            continue
        age_min = (now - started).total_seconds() / 60
        if age_min >= STUCK_SEARCH_MINUTES:
            c = dict(c)
            c["_age_minutes"] = round(age_min, 1)
            stuck.append(c)
    return stuck


def import_running(commands: list[dict[str, Any]]) -> bool:
    return any(
        c.get("name") == "ProcessMonitoredDownloads"
        and c.get("status") in ("queued", "started")
        for c in commands
    )


def trigger_import(execute: bool) -> bool:
    if not execute:
        return True
    r = requests.post(
        f"{SONARR_URL}/command",
        headers=headers(),
        json={"name": "ProcessMonitoredDownloads"},
        timeout=TIMEOUT,
    )
    if r.status_code == 201:
        log("Queued ProcessMonitoredDownloads")
        return True
    log(f"ProcessMonitoredDownloads failed: {r.status_code} {r.text[:120]}")
    return False


def cancel_command(cmd_id: int, execute: bool) -> bool:
    if not execute:
        return True
    r = requests.delete(f"{SONARR_URL}/command/{cmd_id}", headers=headers(), timeout=TIMEOUT)
    if r.status_code in (200, 204):
        return True
    log(f"Cancel command {cmd_id} failed: {r.status_code} {r.text[:120]}")
    return False


def start_sonarr_if_offline(execute: bool) -> bool:
    if sonarr_online():
        return True
    if not execute:
        log(f"Sonarr offline — would start container {SONARR_CONTAINER!r}")
        return False

    log(f"Sonarr offline — starting container {SONARR_CONTAINER!r}")
    proc = subprocess.run(
        ["docker", "start", SONARR_CONTAINER],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        log(f"docker start failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return False

    for _ in range(30):
        time.sleep(2)
        if sonarr_online():
            log("Sonarr API is back after start")
            return True
    log("Sonarr API did not respond after start")
    return False


def restart_sonarr(execute: bool, state: dict[str, Any]) -> bool:
    last = state.get("last_sonarr_restart_at")
    if last:
        last_dt = parse_utc(last)
        if last_dt:
            elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
            if elapsed < RESTART_COOLDOWN_MINUTES:
                log(
                    f"Sonarr restart skipped — cooldown "
                    f"({int(elapsed)}m < {RESTART_COOLDOWN_MINUTES}m)"
                )
                return False

    if not execute:
        log(f"Would restart Sonarr container {SONARR_CONTAINER!r}")
        return True

    log(f"Restarting Sonarr container {SONARR_CONTAINER!r}...")
    proc = subprocess.run(
        ["docker", "restart", SONARR_CONTAINER],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        log(f"docker restart failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return False

    state["last_sonarr_restart_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    log("Sonarr restarted — waiting for API...")
    for _ in range(30):
        time.sleep(2)
        if sonarr_online():
            log("Sonarr API is back")
            return True
    log("Sonarr API did not respond after restart")
    return False


def run_guard(execute: bool) -> int:
    state = load_state()

    if not mount_ok():
        log(f"Skip — {NZBDAV_MOUNT} not mounted/healthy")
        return 0

    if not sonarr_online():
        if not start_sonarr_if_offline(execute):
            return 0 if not execute else 1

    backlog, breakdown = count_completed_backlog()
    commands = get_commands()
    stuck = stuck_searches(commands)
    import_active = import_running(commands)

    log(
        f"backlog={backlog} (anime={breakdown.get(COMPLETED_ROOTS[0], 0)}, "
        f"tv={breakdown.get(COMPLETED_ROOTS[1], 0)}) "
        f"stuck_searches={len(stuck)} import_running={import_active}"
    )

    for c in stuck:
        log(
            f"  stuck {c.get('name')} id={c.get('id')} age={c.get('_age_minutes')}m "
            f"msg={(c.get('message') or '')[:70]}"
        )

    needs_import = backlog >= BACKLOG_THRESHOLD
    needs_action = needs_import or bool(stuck)

    if not needs_action:
        log("OK — no import guard action needed")
        return 0

    if not execute:
        log("DRY RUN — would attempt import recovery")
        if stuck:
            log("DRY RUN — would try cancel stuck searches, restart if cancel fails")
        return 0

    if (needs_import or stuck) and not import_active:
        trigger_import(execute=True)

    if stuck:
        cancelled = 0
        for c in stuck:
            if cancel_command(c["id"], execute=True):
                cancelled += 1
        time.sleep(3)
        commands = get_commands()
        still_stuck = stuck_searches(commands)
        if still_stuck:
            log(f"{len(still_stuck)} search command(s) still stuck after cancel")
            if restart_sonarr(execute=True, state=state):
                time.sleep(5)
                if not import_running(get_commands()):
                    trigger_import(execute=True)
        elif cancelled:
            log(f"Cancelled {cancelled} stuck search command(s)")

    save_state(state)
    return 0


def print_status() -> None:
    state = load_state()
    backlog, breakdown = count_completed_backlog()
    log("=== Sonarr import guard status ===")
    log(f"  nzbdav mount ok: {mount_ok()}")
    log(f"  sonarr online: {sonarr_online()}")
    log(f"  completed backlog: {backlog} (threshold {BACKLOG_THRESHOLD})")
    for path, n in breakdown.items():
        log(f"    {path}: {n}")
    log(f"  stuck search threshold: {STUCK_SEARCH_MINUTES} minutes")
    log(f"  last sonarr restart: {state.get('last_sonarr_restart_at', 'never')}")
    if sonarr_online():
        stuck = stuck_searches(get_commands())
        log(f"  stuck searches now: {len(stuck)}")
        for c in stuck:
            log(
                f"    {c.get('name')} age={c.get('_age_minutes')}m "
                f"{(c.get('message') or '')[:60]}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Sonarr import guard for NzbDAV backlog")
    parser.add_argument("--execute", action="store_true", help="Apply fixes")
    parser.add_argument("--status", action="store_true", help="Print status and exit")
    args = parser.parse_args()

    if args.status:
        print_status()
        return 0
    return run_guard(execute=args.execute)


if __name__ == "__main__":
    sys.exit(main())
