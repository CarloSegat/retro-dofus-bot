"""Continuously probe host internet connectivity and log state
transitions. Use this to debug correlated events on the bot side --
e.g. flatmate restarts the router, 3 bot containers all log out at
the same time, watchdog trips three pids simultaneously. Without
this monitor the bot logs just show "client disconnected" with no
upstream context; with it, you can grep `network.log` for the matching
DOWN -> UP window and prove it was the network, not the bot.

Probes 3 independent public DNS resolvers via TCP-connect to port 53:
Cloudflare, Google, Quad9. Treating "all three down" as the only
definitive outage signal avoids false positives when any single
provider is having a bad minute. Pure stdlib, no project imports --
runs anywhere Python 3 is available.

Logged lines (each is one record, prefixed with timestamp):

  [net] starting connectivity monitor: targets=[...] ...
  [net] initial state: UP|DOWN (ok=N/3 failed=[...])
  [net] DOWN -- all 3 probes failed (...). previous UP duration: 2.3h
  [net] UP -- 3/3 probes succeeded. previous DOWN duration: 47s
  [net] still UP (for 1.2h, ok=3/3)        # heartbeat while UP
  [net] still DOWN (for 5.4m, failed=[...]) # heartbeat while DOWN
  [net] stopped by SIGINT

Output: stdout AND a rotating file at logs/network.log (daily
rotation, 30 days kept). Cross-correlate with
`logs/<instance>/restart_history.jsonl` or fighter.log timestamps.

Usage:
    python3 scripts/monitor_connectivity.py
    python3 scripts/monitor_connectivity.py --interval 10 --heartbeat-min 60
    nohup python3 -u scripts/monitor_connectivity.py >/dev/null 2>&1 &
"""
import argparse
import logging
import os
import signal
import socket
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


# 3 independent operators -- "all three failed" is a much stronger
# signal than "one failed", and we don't pay anything noticeable
# (sub-second total when network is healthy, ~6s when totally down).
PROBE_TARGETS = [
    ("1.1.1.1", 53),  # Cloudflare DNS
    ("8.8.8.8", 53),  # Google DNS
    ("9.9.9.9", 53),  # Quad9 DNS
]
PROBE_TIMEOUT_SEC = 2.0

# logs/ relative to repo root (scripts/.. -> repo root).
_DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_DEFAULT_LOG_NAME = "network.log"


def _probe(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _probe_all(targets, timeout):
    """Returns (ok_count, failed_list_of_'host:port'_strings)."""
    failed = []
    ok = 0
    for host, port in targets:
        if _probe(host, port, timeout):
            ok += 1
        else:
            failed.append(f"{host}:{port}")
    return ok, failed


def _fmt_duration(secs):
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs/60:.1f}m"
    return f"{secs/3600:.2f}h"


def _setup_log(log_path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("connectivity")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    fh = TimedRotatingFileHandler(
        log_path, when="D", interval=1, backupCount=30,
        encoding="utf-8", utc=False,
    )
    fh.suffix = "%Y-%m-%d"
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def main():
    parser = argparse.ArgumentParser(
        description="Continuously probe internet connectivity and log "
                    "state transitions to logs/network.log")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="seconds between probes (default 5)")
    parser.add_argument("--heartbeat-min", type=float, default=30.0,
                        help="minutes between 'still UP/DOWN' heartbeat "
                             "lines (default 30). Prevents a quiet log "
                             "from looking like a dead monitor.")
    parser.add_argument("--log-dir", default=None,
                        help="dir to write network.log into "
                             "(default <repo>/logs/)")
    args = parser.parse_args()

    log_dir = Path(args.log_dir) if args.log_dir else _DEFAULT_LOG_DIR
    log_path = log_dir / _DEFAULT_LOG_NAME
    log = _setup_log(log_path)

    # Clean exit on SIGTERM (so `kill <pid>` from a wrapper logs the
    # stop instead of leaving the file with no closing marker).
    def _stop(signum, frame):
        log.info(f"[net] stopped by signal {signum}")
        sys.exit(0)
    signal.signal(signal.SIGTERM, _stop)

    targets_str = ", ".join(f"{h}:{p}" for h, p in PROBE_TARGETS)
    log.info(f"[net] starting connectivity monitor: pid={os.getpid()} "
             f"targets=[{targets_str}] interval={args.interval}s "
             f"heartbeat={args.heartbeat_min}min log={log_path}")

    ok, failed = _probe_all(PROBE_TARGETS, PROBE_TIMEOUT_SEC)
    state_up = ok >= 1
    state_changed_at = time.time()
    last_heartbeat_at = state_changed_at
    log.info(f"[net] initial state: {'UP' if state_up else 'DOWN'} "
             f"(ok={ok}/{len(PROBE_TARGETS)} failed={failed})")

    try:
        while True:
            time.sleep(args.interval)
            ok, failed = _probe_all(PROBE_TARGETS, PROBE_TIMEOUT_SEC)
            now_up = ok >= 1
            now = time.time()
            if now_up != state_up:
                duration = now - state_changed_at
                if state_up:
                    log.warning(
                        f"[net] DOWN -- all {len(PROBE_TARGETS)} probes "
                        f"failed ({failed}). previous UP duration: "
                        f"{_fmt_duration(duration)}")
                else:
                    log.info(
                        f"[net] UP -- {ok}/{len(PROBE_TARGETS)} probes "
                        f"succeeded. previous DOWN duration: "
                        f"{_fmt_duration(duration)}")
                state_up = now_up
                state_changed_at = now
                last_heartbeat_at = now
                continue
            if (now - last_heartbeat_at) >= args.heartbeat_min * 60:
                in_state_for = now - state_changed_at
                if state_up:
                    log.info(f"[net] still UP (for "
                             f"{_fmt_duration(in_state_for)}, "
                             f"ok={ok}/{len(PROBE_TARGETS)})")
                else:
                    log.info(f"[net] still DOWN (for "
                             f"{_fmt_duration(in_state_for)}, "
                             f"failed={failed})")
                last_heartbeat_at = now
    except KeyboardInterrupt:
        log.info("[net] stopped by SIGINT")


if __name__ == "__main__":
    main()
