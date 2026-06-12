#!/usr/bin/env python3
"""Wrapper: skip /opt/jellyfin_safe_refresh when Scan Media Library is active."""
import json
import sqlite3
import subprocess
import sys
import urllib.request

JELLYFIN_URL = "http://localhost:8096"
JELLYFIN_DB = "/opt/jellyfin/config/data/data/jellyfin.db"
SCAN_TASK_ID = "7738148ffcd07979c7ceb148e06b3aed"


def scan_active() -> bool:
    try:
        conn = sqlite3.connect(JELLYFIN_DB, timeout=2)
        row = conn.execute(
            "SELECT AccessToken FROM ApiKeys ORDER BY DateCreated DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            return False
        req = urllib.request.Request(
            f"{JELLYFIN_URL}/ScheduledTasks/{SCAN_TASK_ID}",
            headers={"X-Emby-Token": row[0]},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            state = json.loads(resp.read()).get("State")
        return state in ("Running", "Cancelling")
    except Exception:
        return False


if scan_active():
    print("[jellyfin_safe_refresh] Scan Media Library active — skipping refresh")
    sys.exit(0)

raise SystemExit(subprocess.call([sys.executable, "/opt/jellyfin_safe_refresh.py", *sys.argv[1:]]))
