"""Combat: the in-fight turn loop with event callbacks.

Combat doesn't know about spells, hotkeys, or the character class. It
waits for turn boundaries, fires four callbacks, and lets registered
handlers do the work:

  on_fight_engaged(snap)   fires once when the fight loop begins
  on_turn_start(ctx)       fires when our turn begins; ctx has snap, turn_n
  on_turn_end(ctx)         fires after on_turn_start handlers return
                           (handlers presumably called pass_turn)
  on_fight_ended(snap)     fires once when in_combat goes False

Sacrieur registers on on_turn_start (its play_turn is the brain).
HpRegen registers on on_fight_engaged (clears sitting). Orchestrator
registers on on_fight_ended to transition state.
"""
import time
from dataclasses import dataclass
from typing import Callable, List

from mouse_keyboard import press_focused
from utils import CFG
from vision import ensure_safe_to_resume
from fighter.helpers import wait_for, append_fight_stats, STATS_FILE


TURN_START_SETTLE_SEC = float(CFG.get("turn_start_settle_sec", 1.5))
TURN_WAIT_TIMEOUT_SEC = float(CFG.get("turn_wait_timeout_sec", 90.0))
POST_FIGHT_PRE_WAIT_SEC = 4.0  # let level-up + XP-summary popups render
POST_FIGHT_RETURN_TO_ESC_GAP = 1.0


@dataclass
class TurnContext:
    """Argument passed to on_turn_start / on_turn_end handlers."""
    snap: object  # Snapshot
    turn_n: int


class Combat:
    """In-fight loop: wait for our turn, fire callbacks, repeat until
    the fight ends. Post-fight: dismiss level-up Return + Esc summary,
    then verify the screen is clean via vision.ensure_safe_to_resume."""

    def __init__(self, state, ctx):
        self.state = state
        # `ctx` is the legacy make_ctx bundle (cfg/sct/grab_region/click)
        # consumed by ensure_safe_to_resume. Stays for now until vision
        # OCR helpers stop taking it.
        self.ctx = ctx
        self._fight_engaged_handlers: List[Callable] = []
        self._turn_start_handlers: List[Callable] = []
        self._turn_end_handlers: List[Callable] = []
        self._fight_ended_handlers: List[Callable] = []

    # --- Callback registration ---

    def on_fight_engaged(self, fn):
        """Register `fn(snap)` to be called once when the fight loop
        begins. HpRegen uses this to clear its sitting flag."""
        self._fight_engaged_handlers.append(fn)

    def on_turn_start(self, fn):
        """Register `fn(ctx)` to be called when our turn begins.
        Sacrieur.play_turn is the primary handler -- it does the
        actual per-turn work and calls pass_turn at the end."""
        self._turn_start_handlers.append(fn)

    def on_turn_end(self, fn):
        """Register `fn(ctx)` to be called after the on_turn_start
        handlers return. Useful for logging/stats; not load-bearing."""
        self._turn_end_handlers.append(fn)

    def on_fight_ended(self, fn):
        """Register `fn(snap)` to be called once when in_combat goes
        False. Orchestrator uses this to transition state."""
        self._fight_ended_handlers.append(fn)

    # --- Main loop ---

    def run(self):
        """Run one fight from start to end. Returns when the fight
        is over and the screen is safe to resume."""
        snap = self.state.snapshot()
        my_id = snap.my_id
        fight_mob_size = sum(1 for e in snap.fight_entities.values()
                             if e.id != snap.my_id)
        fight_start_ts = snap.last_fight_engage_ts

        for fn in self._fight_engaged_handlers:
            fn(snap)

        last_turn_n = 0
        while self.state.snapshot().in_combat:
            new_turn = self._wait_for_my_turn(my_id, last_turn_n)
            if new_turn == 0:
                break
            print(f"  TURN {new_turn} start (actor={my_id}); settling "
                  f"{TURN_START_SETTLE_SEC}s before acting")
            last_turn_n = new_turn
            time.sleep(TURN_START_SETTLE_SEC)

            ctx = TurnContext(snap=self.state.snapshot(), turn_n=new_turn)
            for fn in self._turn_start_handlers:
                fn(ctx)
            for fn in self._turn_end_handlers:
                fn(ctx)

        end_snap = self.state.snapshot()
        for fn in self._fight_ended_handlers:
            fn(end_snap)
        self._record_stats_if_clean_end(fight_mob_size, fight_start_ts, end_snap)
        self._post_fight_cleanup()

    # --- Internals ---

    def _wait_for_my_turn(self, my_id, last_turn_n):
        """Block until GTS<my_id> with turn_number > last_turn_n.
        Returns the new turn_number on success, 0 if combat ended or
        we timed out. Polls at 50ms so the post-GTS settle dominates
        time-to-first-click latency, not our poll cadence."""
        deadline = time.time() + TURN_WAIT_TIMEOUT_SEC
        while time.time() < deadline:
            snap = self.state.snapshot()
            if not snap.in_combat:
                return 0
            if snap.turn_actor == my_id and snap.turn_number > last_turn_n:
                return snap.turn_number
            time.sleep(0.05)
        return 0

    def _record_stats_if_clean_end(self, fight_mob_size, fight_start_ts, end_snap):
        """Append a stats record only on a real fight end -- guards
        against double-counting if the loop returned due to the
        turn-wait timeout."""
        if (not end_snap.in_combat
                and fight_mob_size > 0
                and fight_start_ts > 0
                and end_snap.last_fight_end_ts > fight_start_ts):
            duration = end_snap.last_fight_end_ts - fight_start_ts
            append_fight_stats(fight_mob_size, duration)
            print(f"[fighter] stats: mob_size={fight_mob_size} "
                  f"fight_duration={duration:.2f}s -> {STATS_FILE}")

    def _post_fight_cleanup(self):
        """Dismiss the level-up popup (Return) and the XP summary (Esc),
        then verify nothing blocks the screen via OCR."""
        time.sleep(POST_FIGHT_PRE_WAIT_SEC)
        press_focused("Return")
        time.sleep(POST_FIGHT_RETURN_TO_ESC_GAP)
        press_focused("Escape")
        time.sleep(0.3)
        if not ensure_safe_to_resume(self.ctx):
            print("[fighter] menu still open after Esc -- aborting")
            import sys
            sys.exit(1)
