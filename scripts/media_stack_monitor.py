#!/usr/bin/env python3
"""Self-healing monitor for the media stack. Runs via cron every 15 min.

Restarts Sonarr/Radarr when remounting (they must see fresh FUSE handles).
Does NOT restart Jellyfin automatically — doing so mid library scan corrupts
the SQLite DB and looks like a "crash". After a real rclone remount, restart
Jellyfin yourself once, or run: python3 /opt/jellyfin_library_recover.py scan --no-wait
"""
import os
import sqlite3
import subprocess
import time
import urllib.request

MOUNTS = [
    {"path": "/mnt/nzbdav", "container": None, "systemd": "rclone-media"},
    {"path": "/mnt/remote/realdebrid", "container": "rclone"},
]
JELLYFIN_DB = os.environ.get(
    "JELLYFIN_DB", "/opt/jellyfin/config/data/data/jellyfin.db"
)
SCAN_TASK_ID = "7738148ffcd07979c7ceb148e06b3aed"
VERIFY_DELAY_SEC = int(os.environ.get("MOUNT_VERIFY_DELAY", "45"))


def run(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    except Exception:
        return None


def is_mount_healthy(path):
    """True if path is a mount and listable.

    Do NOT use a file canary here: during heavy Jellyfin/rclone IO, symlink
    resolution can briefly fail and would falsely trigger full stack restarts.
    """
    try:
        if not os.path.ismount(path):
            return False
        os.listdir(path)
        return True
    except OSError:
        return False


def jellyfin_library_scan_running():
    """Avoid killing Jellyfin while Scan Media Library is in progress."""
    try:
        conn = sqlite3.connect(JELLYFIN_DB, timeout=2)
        row = conn.execute(
            "SELECT AccessToken FROM ApiKeys ORDER BY DateCreated DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            return False
        token = row[0]
        req = urllib.request.Request(
            f"http://localhost:8096/ScheduledTasks/{SCAN_TASK_ID}",
            headers={"X-Emby-Token": token},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            import json

            data = json.loads(resp.read())
        return data.get("State") == "Running"
    except Exception:
        return False


def container_nzbdav_ok(name):
    r = run(
        f"docker exec {name} sh -c 'test -d /mnt/nzbdav && ls /mnt/nzbdav >/dev/null 2>&1'"
    )
    return r and r.returncode == 0


def kill_zombie_ffprobes():
    """Kill any ffprobe processes stuck in D-state (uninterruptible sleep)."""
    result = run("ps aux | awk '$8 ~ /D/ && /ffprobe/ {print $2}'")
    if result and result.stdout.strip():
        pids = result.stdout.strip().split("\n")
        for pid in pids:
            run(f"sudo kill -9 {pid}")
        print(f"Killed {len(pids)} zombie ffprobe processes (Sonarr not restarted — preserves import queue)")
        return True
    return False


def repair_mount(mount):
    if mount.get("systemd"):
        run(f"sudo systemctl restart {mount['systemd']}")
        time.sleep(8)
    elif mount.get("container"):
        run(f"sudo umount -l {mount['path']}")
        run(f"sudo docker restart {mount['container']}")
        time.sleep(10)
    run("sudo docker restart sonarr radarr shoko_server")
    print(
        "Sonarr/Radarr/Shoko restarted. Jellyfin was NOT restarted (avoids DB corruption "
        "during scans). If the UI still shows broken paths, after the mount is stable run: "
        "sudo docker restart jellyfin"
    )


def remount_still_needed(mount):
    """Re-check after a delay — ignore transient glitches during heavy IO."""
    time.sleep(VERIFY_DELAY_SEC)
    return not is_mount_healthy(mount["path"])


def main():
    repaired = False

    if kill_zombie_ffprobes():
        repaired = True

    for m in MOUNTS:
        if not is_mount_healthy(m["path"]):
            print(f"Mount possibly unhealthy: {m['path']} — verifying in {VERIFY_DELAY_SEC}s...")
            if not remount_still_needed(m):
                print(f"Mount {m['path']} recovered (transient); no repair.")
                continue
            print(f"Mount unhealthy (confirmed): {m['path']}")
            repair_mount(m)
            repaired = True

    scan_on = jellyfin_library_scan_running()
    for c in ("sonarr", "radarr"):
        if not container_nzbdav_ok(c):
            print(f"Container mount stale: {c}")
            run("sudo docker restart sonarr radarr")
            time.sleep(10)
            repaired = True
            break

    # Shoko also bind-mounts /mnt/nzbdav. After an rclone remount its handle goes
    # stale ("Transport endpoint is not connected"); its ImportFolderWatcher then
    # spins on thousands of unreadable symlinks and the whole server becomes
    # unreachable (API/healthcheck time out). It has no Jellyfin-style scan/DB
    # corruption risk, so restart it freely.
    if not container_nzbdav_ok("shoko_server"):
        print("Container mount stale: shoko_server — restarting")
        run("sudo docker restart shoko_server")
        time.sleep(10)
        repaired = True

    if not container_nzbdav_ok("jellyfin"):
        if scan_on:
            print(
                "Jellyfin container sees stale/broken nzbdav mount but Scan Media Library "
                "is running — NOT restarting Jellyfin (would corrupt DB). "
                "Finish or cancel the scan, then: sudo docker restart jellyfin"
            )
        else:
            print("Jellyfin nzbdav stale — restarting Jellyfin only (no scan running)")
            run("sudo docker restart jellyfin")
            time.sleep(10)
            repaired = True

    if repaired:
        print("Repairs made. System should self-recover.")


if __name__ == "__main__":
    main()
