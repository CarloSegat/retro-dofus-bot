"""ProgressWatchdog: detect "the bot is stuck" and trigger a full restart.

Two flavors of "stuck" the bot recovers from autonomously:

  Idle stall   -- main loop iterates but state never advances. The
                  signal tuple (map_id, last_fight_engage_ts,
                  last_fight_end_ts, estimated_life()) is constant for
                  more than `threshold_sec`. estimated_life() is in
                  the tuple so sit-regen counts as progress -- HP
                  climbs at the server's ILS rate, so the metric ticks
                  even when no other state changes.

  In-fight stall -- Combat._wait_for_my_turn times out (no GTS for
                  `turn_wait_timeout_sec`). Combat raises
                  StaleClientError; Orchestrator catches it and routes
                  to the same restart routine.

Both funnel into `Orchestrator._restart_everything`, which calls
`restart_dofus_client(...)` then os.execv-s the python process back
into `main.py --auto-reuse`.
"""
import time


class StaleClientError(Exception):
    """Raised by Combat when no turn has fired for too long. Caught by
    Orchestrator, which routes to ProgressWatchdog's on_stuck callback
    (the same path used by the idle-stall detection)."""


class ProgressWatchdog:
    """Idle-state progress watchdog. See module docstring.

    `on_stuck(reason: str)` is called at most once per instance; the
    expected implementation re-execs the process, so re-arming is
    unnecessary.
    """

    def __init__(self, threshold_sec, on_stuck):
        self.threshold_sec = float(threshold_sec)
        self.on_stuck = on_stuck
        self._last_sig = None
        self._last_progress_ts = time.time()
        # Periodic "still idling" cadence -- one line every threshold/4
        # while stuck, so a long flat stretch leaves a breadcrumb trail
        # across rotated log chunks.
        self._next_warn_at = self._last_progress_ts + self.threshold_sec / 4.0
        self._fired = False

    def _signature(self, snap):
        # estimated_life() ticks continuously during sit-regen, so a
        # bot legitimately healing for 15 min does not look stuck.
        # Wrapped in a try because estimated_life() can be touched
        # before the snapshot has any HP info (returns 0 then, which
        # is fine -- it's still a deterministic tuple).
        try:
            life = snap.estimated_life()
        except Exception:
            life = 0
        return (
            snap.map_id,
            round(snap.last_fight_engage_ts, 2),
            round(snap.last_fight_end_ts, 2),
            life,
        )

    @staticmethod
    def _format_sig(sig):
        map_id, engage_ts, end_ts, life = sig
        # Human-readable form. Bare ts ints would be unix-epoch noise
        # in logs; printing them as "Xs ago" makes idle-since obvious.
        now = time.time()
        engage_age = (now - engage_ts) if engage_ts > 0 else None
        end_age = (now - end_ts) if end_ts > 0 else None
        eng = f"{engage_age:.0f}s ago" if engage_age is not None else "never"
        ended = f"{end_age:.0f}s ago" if end_age is not None else "never"
        return (f"map={map_id} last_engage={eng} "
                f"last_fight_end={ended} hp={life}")

    def reset(self, reason=""):
        """Explicit heartbeat. Used for events the snapshot signal
        doesn't capture (e.g. a successful engage click that the proxy
        will only reflect a beat later)."""
        self._last_progress_ts = time.time()
        self._next_warn_at = self._last_progress_ts + self.threshold_sec / 4.0
        if reason:
            print(f"[watchdog] reset ({reason})")

    def check(self, snap):
        """Call once per main-loop tick. Fires on_stuck (once) when
        the threshold is crossed. Returns True if on_stuck fired."""
        if self._fired:
            return False
        sig = self._signature(snap)
        now = time.time()
        if sig != self._last_sig:
            self._last_sig = sig
            self._last_progress_ts = now
            self._next_warn_at = now + self.threshold_sec / 4.0
            return False
        idle = now - self._last_progress_ts
        if now >= self._next_warn_at and idle < self.threshold_sec:
            print(f"[watchdog] no progress for {idle:.0f}s / "
                  f"{self.threshold_sec:.0f}s; {self._format_sig(sig)}")
            self._next_warn_at = now + self.threshold_sec / 4.0
        if idle >= self.threshold_sec:
            self._fired = True
            reason = (f"no progress for {idle:.0f}s "
                      f"(threshold {self.threshold_sec:.0f}s); "
                      f"{self._format_sig(sig)}")
            print(f"[watchdog] TRIPPED: {reason}")
            self.on_stuck(reason)
            return True
        return False
