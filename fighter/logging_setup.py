"""Stdout/stderr tee + rotating log file.

The bot is `print()`-heavy: dozens of modules write status lines to
stdout. To debug a "came back to find the bot logged out" scenario we
want those lines persisted to disk with timestamps, but split into
manageable chunks so the last few minutes before failure are easy to
find.

`setup_logging()` does two things:

  1. Builds a TimedRotatingFileHandler under `<project>/logs/` that
     rotates every `interval_min` minutes (default 10) and keeps
     `backup_count` files (default 144 = 24h).
  2. Replaces sys.stdout / sys.stderr with a tee that forwards each
     write to BOTH the original stream (so `tee /tmp/proxy.log` and
     interactive runs still scroll live) AND the rotating handler
     (timestamped, line-buffered, persisted across rotations).

Every existing `print(...)` call is captured automatically -- no
module-level refactor needed.
"""
import logging
import os
import socket
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


_DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def instance_log_dir(base_dir=None):
    """Return the per-instance log directory (without creating it).
    Same resolution as `setup_logging` so callers writing sibling
    files (e.g. `restart_history.jsonl`) land in the same place as
    `fighter.log` / `proxy.log`."""
    base = Path(base_dir) if base_dir else _DEFAULT_LOG_DIR
    return base / _resolve_instance()


def _resolve_instance():
    """Pick a stable identifier for the per-instance log subdirectory.

    Priority:
      1. $FIGHTER_INSTANCE -- set explicitly in docker-compose per service
         (`desktop-1`, `desktop-2`, ...). This is the authoritative tag.
      2. socket.gethostname() -- stable on the host, but in Docker it's a
         random container ID hash. Worse for grep but better than nothing
         if the env was forgotten.
      3. "host" -- final fallback so we never produce a `fighter..log` or
         `logs/<empty>/` path.
    """
    raw = os.environ.get("FIGHTER_INSTANCE") or socket.gethostname() or "host"
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in raw)


class _TeeWriter:
    """File-like object that writes to a real stream AND emits each
    complete line to a logger.

    Buffers partial lines so a `print(...)` that arrives in fragments
    still hits the file as one timestamped record."""

    def __init__(self, real_stream, logger, level):
        self._real = real_stream
        self._logger = logger
        self._level = level
        self._buf = ""

    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        try:
            self._real.write(s)
        except Exception:
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line:
                self._logger.log(self._level, line)

    def flush(self):
        try:
            self._real.flush()
        except Exception:
            pass
        if self._buf.strip():
            self._logger.log(self._level, self._buf.rstrip())
            self._buf = ""

    def isatty(self):
        try:
            return self._real.isatty()
        except Exception:
            return False

    def fileno(self):
        return self._real.fileno()


def setup_logging(log_dir=None, interval_min=10, backup_count=144):
    """Install the rotating-file tee. Call once at process start.

    Layout: each instance gets its own subdirectory
        <log_dir>/<instance>/fighter.log
    rotated to fighter.log.YYYY-MM-DD_HH-MM. The matching Go proxy
    writes to <log_dir>/<instance>/proxy.log -- so a single
    `ls logs/<instance>/` shows both sides of one bot. Rule of the
    project: one container per Dofus character, so <instance> is a
    1:1 stand-in for the character running there.

    Returns the Path to the active log file. Failures (e.g. dir not
    writable) are reported on stderr and fall back to stdout-only --
    the bot keeps running."""
    base_dir = Path(log_dir) if log_dir else _DEFAULT_LOG_DIR
    instance = _resolve_instance()
    inst_dir = base_dir / instance
    try:
        inst_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.__stderr__.write(f"[logging] could not create {inst_dir}: {e}; "
                             f"falling back to stdout-only\n")
        return None

    log_path = inst_dir / "fighter.log"
    logger = logging.getLogger("autofighter.stdout")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # Idempotent: clear handlers on repeat setup so re-imports during
    # tests / REPL don't stack duplicates.
    logger.handlers.clear()

    handler = TimedRotatingFileHandler(
        log_path,
        when="M",
        interval=interval_min,
        backupCount=backup_count,
        encoding="utf-8",
        utc=False,
    )
    # Rotated files get a `.YYYY-MM-DD_HH-MM` suffix instead of just
    # `.N`, so `ls logs/` is chronologically obvious without stat.
    handler.suffix = "%Y-%m-%d_%H-%M"
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)

    sys.stdout = _TeeWriter(sys.__stdout__, logger, logging.INFO)
    sys.stderr = _TeeWriter(sys.__stderr__, logger, logging.WARNING)

    print(f"[logging] rotating log: {log_path} "
          f"(every {interval_min} min, keep {backup_count})")
    # Session banner: prints once per process so any future log search
    # for the character name or class succeeds. Required because the
    # bot's print() lines themselves don't carry character identity.
    character = os.environ.get("FIGHTER_CHARACTER") or "?"
    print(f"[session] instance={instance} character={character} "
          f"pid={os.getpid()}")
    return log_path
