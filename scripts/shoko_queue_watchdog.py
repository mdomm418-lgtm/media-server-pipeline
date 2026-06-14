#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


def load_env(path: Path) -> dict[str, str]:
    env = {}
    if path.exists():
        with path.open() as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k] = v
    return env


def parse_iso_time(time_str: str) -> datetime:
    # Python's fromisoformat only supports up to 6 digits of fractional seconds
    # Shoko's .NET output often includes 7 digits. Strip the 7th.
    time_str = re.sub(r"(\.\d{6})\d+([+-Z])", r"\1\2", time_str)
    return datetime.fromisoformat(time_str).astimezone(timezone.utc)


def main() -> int:
    env_path = Path("/home/admin/shoko-autolink/.env")
    env = load_env(env_path)

    user = env.get("SHOKO_USERNAME")
    pwd = env.get("SHOKO_PASSWORD")
    if not user or not pwd:
        print("Missing Shoko credentials in .env", file=sys.stderr)
        return 1

    try:
        r = requests.post(
            "http://127.0.0.1:8111/api/auth",
            json={"user": user, "pass": pwd, "device": "watchdog"},
            timeout=10,
        )
        r.raise_for_status()
        apikey = r.json().get("apikey")

        r = requests.get(
            "http://127.0.0.1:8111/api/v3/Queue/Items",
            headers={"apikey": apikey},
            params={"pageSize": 50},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        print(f"Failed to fetch queue: {e}", file=sys.stderr)
        return 1

    items = data.get("List", [])
    now = datetime.now(timezone.utc)

    for item in items:
        if item.get("IsRunning") and "StartTime" in item:
            start_time = parse_iso_time(item["StartTime"])
            age_sec = (now - start_time).total_seconds()

            # If an AniDB UDP task hangs, it freezes forever.
            # Hash File tasks can legitimately take 1-2 hours on a network mount.
            task_type = item.get("Type", "")
            title = item.get("Title", "Unknown")
            
            is_anidb = "anidb" in task_type.lower() or "anidb" in title.lower()
            timeout_sec = 45 * 60 if is_anidb else 4 * 60 * 60

            if age_sec > timeout_sec:
                print(f"Watchdog triggered: Task '{title}' ({task_type}) running for {age_sec/60:.1f} minutes. Restarting Shoko container...")
                subprocess.run(["docker", "restart", "shoko_server"], check=True)
                return 0

    print("All tasks are healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
