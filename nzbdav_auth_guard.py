#!/usr/bin/env python3
"""
NzbDAV Usenet auth-storm circuit breaker.

Prevents a transient provider hiccup from snowballing into an account-level
abuse lock (the failure mode that took the library offline after the WAN IP
change: NzbDAV retry-stormed ~5,500 failed AUTHINFO attempts per hour, which
tripped NewsDemon's security lock).

NzbDAV's connection pool retries Usenet logins aggressively and has no backoff.
This watchdog watches NzbDAV's logs for a burst of login failures. On a burst it
STOPS the nzbdav container to halt the storm, then probes the provider with a
SINGLE authenticated NNTP handshake on an exponential backoff. It only restarts
nzbdav once a probe genuinely succeeds (281). That caps login attempts to a few
per hour instead of thousands, so a provider blip can never escalate to a lock.

Cron (every 5 min):
  */5 * * * * flock -n /home/admin/nzbdav-auth-guard.lock \
      /usr/bin/python3 /home/admin/nzbdav_auth_guard.py --execute \
      >> /home/admin/nzbdav-auth-guard.log 2>&1

Usage:
  nzbdav_auth_guard.py            # dry-run (report only, no actions)
  nzbdav_auth_guard.py --execute
  nzbdav_auth_guard.py --status
  nzbdav_auth_guard.py --reset    # clear tripped state manually
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import subprocess
import time
from datetime import datetime
from pathlib import Path

NZBDAV_CONTAINER = os.environ.get("NZBDAV_CONTAINER", "nzbdav")
NZBDAV_DB = os.environ.get("NZBDAV_DB", "/opt/nzbdav/config/db.sqlite")
NZBDAV_MOUNT = os.environ.get("NZBDAV_MOUNT", "/mnt/nzbdav")
NZBDAV_WEBDAV = os.environ.get("NZBDAV_WEBDAV", "http://127.0.0.1:3000/")
STATE_FILE = os.environ.get(
    "NZBDAV_AUTH_GUARD_STATE", "/home/admin/nzbdav-auth-guard-state.json"
)
ALERT_FILE = os.environ.get(
    "NZBDAV_AUTH_GUARD_ALERT", "/home/admin/.nzbdav_auth_guard_ALERT"
)

# A "storm" is this many failed logins within the lookback window.
FAILURE_THRESHOLD = int(os.environ.get("NZBDAV_FAIL_THRESHOLD", "30"))
LOOKBACK_MIN = int(os.environ.get("NZBDAV_FAIL_LOOKBACK_MIN", "5"))

# Backoff schedule for auth probes while tripped (minutes); last value is the cap.
BACKOFF_MIN = [5, 10, 20, 40, 60]

AUTH_TIMEOUT = int(os.environ.get("NZBDAV_AUTH_TIMEOUT", "20"))
FAIL_PATTERN = "Could not login"


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [nzbdav-auth-guard] {msg}", flush=True)


def load_state() -> dict:
    p = Path(STATE_FILE)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    p = Path(STATE_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2) + "\n")


def run(cmd: list[str], timeout: int = 30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        log(f"command failed {cmd}: {e}")
        return None


def read_provider() -> dict | None:
    import sqlite3

    try:
        con = sqlite3.connect(NZBDAV_DB, timeout=5)
        row = con.execute(
            "SELECT ConfigValue FROM ConfigItems WHERE ConfigName=?",
            ("usenet.providers",),
        ).fetchone()
        con.close()
        if not row:
            return None
        provs = (json.loads(row[0]) or {}).get("Providers") or []
        return provs[0] if provs else None
    except Exception as e:
        log(f"could not read provider config: {e}")
        return None


def auth_is_healthy(resp: str) -> bool:
    """Decide whether a probe response means the account is usable again.

    - "281 ..."            -> authenticated; definitively healthy.
    - "... connection limit ..." -> account is NOT locked; auth works, slots are
      just busy (expected if anything is mid-stream). Safe to resume.
    Everything else (the abuse-lock "Connection failure / contact support",
    "Access Denied", timeouts, socket errors) keeps the breaker tripped.
    """
    r = (resp or "").lower()
    if resp.startswith("281"):
        return True
    if "connection limit" in r:
        return True
    return False


def nntp_auth_probe(prov: dict) -> tuple[bool, str]:
    """One clean authenticated NNTP handshake. Returns (ok, server_response)."""
    host = prov.get("Host")
    port = int(prov.get("Port", 563))
    user = prov.get("User", "")
    pasw = prov.get("Pass", "")
    use_ssl = bool(prov.get("UseSsl", True))

    def readline(sock) -> str:
        sock.settimeout(AUTH_TIMEOUT)
        buf = b""
        try:
            while not buf.endswith(b"\r\n"):
                ch = sock.recv(1)
                if not ch:
                    return buf.decode(errors="replace") + "<EOF>"
                buf += ch
        except socket.timeout:
            return buf.decode(errors="replace") + "<TIMEOUT>"
        return buf.decode(errors="replace").strip()

    s = None
    try:
        raw = socket.create_connection((host, port), timeout=AUTH_TIMEOUT)
        if use_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            s = ctx.wrap_socket(raw, server_hostname=host)
        else:
            s = raw
        readline(s)  # greeting
        s.sendall(b"AUTHINFO USER " + user.encode() + b"\r\n")
        readline(s)
        s.sendall(b"AUTHINFO PASS " + pasw.encode() + b"\r\n")
        resp = readline(s)
        try:
            s.sendall(b"QUIT\r\n")
        except Exception:
            pass
        return auth_is_healthy(resp), resp
    except Exception as e:
        return False, f"<error: {e}>"
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:
            pass


def count_recent_failures() -> int:
    r = run(
        ["sudo", "docker", "logs", "--since", f"{LOOKBACK_MIN}m", NZBDAV_CONTAINER],
        timeout=30,
    )
    if r is None:
        return 0
    text = (r.stdout or "") + (r.stderr or "")
    return sum(1 for line in text.splitlines() if FAIL_PATTERN in line)


def nzbdav_running() -> bool:
    r = run(
        ["sudo", "docker", "ps", "--filter", f"name={NZBDAV_CONTAINER}",
         "--format", "{{.Names}}"]
    )
    return bool(r and NZBDAV_CONTAINER in (r.stdout or ""))


def mount_is_dead() -> bool:
    """True if the mountpoint is a stale FUSE endpoint (ENOTCONN) that would
    block `docker start nzbdav` with a bind-mount error."""
    r = run(["timeout", "5", "ls", NZBDAV_MOUNT], timeout=8)
    if r is None:
        return True
    if r.returncode == 0:
        return False
    err = (r.stderr or "").lower()
    return "not connected" in err or "transport endpoint" in err


def clear_dead_mount() -> None:
    log(f"clearing stale FUSE endpoint at {NZBDAV_MOUNT}")
    run(["fusermount3", "-uz", NZBDAV_MOUNT], timeout=15)
    run(["umount", "-l", NZBDAV_MOUNT], timeout=15)


def webdav_up() -> bool:
    r = run(
        ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
         "--max-time", "4", NZBDAV_WEBDAV],
        timeout=10,
    )
    return bool(r and r.stdout.strip() not in ("", "000"))


def start_nzbdav() -> None:
    """Bring nzbdav back safely: clear any dead mount first (else the bind
    mount fails), start the container, then let systemd remount /mnt/nzbdav."""
    if mount_is_dead():
        clear_dead_mount()
    run(["sudo", "docker", "start", NZBDAV_CONTAINER], timeout=60)
    # wait for webdav to answer
    for _ in range(10):
        if webdav_up():
            break
        time.sleep(3)
    # systemd rclone-media (Restart=on-failure) remounts once :3000 is up
    for _ in range(12):
        r = run(["mountpoint", "-q", NZBDAV_MOUNT], timeout=8)
        if r is not None and r.returncode == 0:
            log(f"{NZBDAV_MOUNT} remounted")
            return
        time.sleep(4)
    log(f"WARNING: {NZBDAV_MOUNT} not remounted after start; "
        f"systemd rclone-media may need attention")


def stop_nzbdav() -> None:
    run(["sudo", "docker", "stop", NZBDAV_CONTAINER], timeout=60)


def write_alert(msg: str) -> None:
    try:
        Path(ALERT_FILE).write_text(
            f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n"
        )
    except OSError:
        pass


def clear_alert() -> None:
    try:
        Path(ALERT_FILE).unlink()
    except OSError:
        pass


def backoff_minutes(idx: int) -> int:
    return BACKOFF_MIN[min(idx, len(BACKOFF_MIN) - 1)]


def main() -> None:
    ap = argparse.ArgumentParser(description="NzbDAV Usenet auth-storm circuit breaker")
    ap.add_argument("--execute", action="store_true",
                    help="take action (default: dry-run / report only)")
    ap.add_argument("--status", action="store_true", help="print state and exit")
    ap.add_argument("--reset", action="store_true", help="clear tripped state")
    args = ap.parse_args()

    state = load_state()

    if args.reset:
        save_state({})
        clear_alert()
        log("state reset; alert cleared")
        return

    if args.status:
        prov = read_provider()
        print(json.dumps({
            "tripped": state.get("tripped", False),
            "backoff_idx": state.get("backoff_idx", 0),
            "tripped_at": state.get("tripped_at"),
            "next_probe_in_sec": (
                max(0, int(state.get("next_probe_at", 0) - time.time()))
                if state.get("tripped") else None
            ),
            "provider": (prov or {}).get("Host"),
            "nzbdav_running": nzbdav_running(),
            "recent_login_failures": count_recent_failures(),
            "threshold": FAILURE_THRESHOLD,
            "lookback_min": LOOKBACK_MIN,
        }, indent=2))
        return

    now = time.time()
    dry = not args.execute

    # ---- tripped: probe provider on backoff, only resume when auth works ----
    if state.get("tripped"):
        nxt = state.get("next_probe_at", 0)
        if now < nxt:
            log(f"tripped; next single auth probe in {int(nxt - now)}s "
                f"(nzbdav held down to protect account)")
            return
        prov = read_provider()
        if not prov:
            log("tripped but cannot read provider creds; backing off")
            state["next_probe_at"] = now + backoff_minutes(state.get("backoff_idx", 0)) * 60
            if not dry:
                save_state(state)
            return
        ok, resp = nntp_auth_probe(prov)
        log(f"auth probe -> {'OK' if ok else 'FAIL'}: {resp}")
        if ok:
            if dry:
                log("[dry-run] would restart nzbdav and clear tripped state")
                return
            start_nzbdav()
            save_state({})
            clear_alert()
            log("provider auth recovered; nzbdav restarted; guard cleared")
        else:
            idx = min(state.get("backoff_idx", 0) + 1, len(BACKOFF_MIN) - 1)
            wait = backoff_minutes(idx)
            state["backoff_idx"] = idx
            state["next_probe_at"] = now + wait * 60
            log(f"provider still refusing auth; next probe in {wait}m")
            if not dry:
                save_state(state)
        return

    # ---- healthy: watch for a login-failure storm ----
    failures = count_recent_failures()
    if failures >= FAILURE_THRESHOLD:
        msg = (f"AUTH STORM: {failures} '{FAIL_PATTERN}' errors in {LOOKBACK_MIN}m "
               f"(threshold {FAILURE_THRESHOLD}). Halting nzbdav to protect the "
               f"Usenet account from an abuse lock.")
        log(msg)
        if dry:
            log("[dry-run] would stop nzbdav and enter backoff auth-probing")
            return
        stop_nzbdav()
        save_state({
            "tripped": True,
            "backoff_idx": 0,
            "tripped_at": datetime.now().isoformat(timespec="seconds"),
            "next_probe_at": now + backoff_minutes(0) * 60,
        })
        write_alert(msg + f" First auth probe in {backoff_minutes(0)}m.")
        log(f"nzbdav stopped; first auth probe in {backoff_minutes(0)}m")
    else:
        log(f"healthy: {failures} login failures in last {LOOKBACK_MIN}m "
            f"(threshold {FAILURE_THRESHOLD})")


if __name__ == "__main__":
    main()
