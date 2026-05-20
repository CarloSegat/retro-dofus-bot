"""Hit-and-run ("tofu") detection + retreat policy.

Some Dofus mobs (tofus being the archetype) kite — they back away every
turn so a melee character can never close. The detector observes the
distance to our nearest live enemy at *turn start* (before we move);
if the last N samples are all > threshold AND not strictly decreasing,
we declare we're being kited. The retreat helper then walks the
character a random 0..mp_remaining steps away from the kiter so they
have to commit MP closing on a moving target instead of free-shooting
at max range.

Both halves are class-agnostic. Each fighter brain decides what spells
to layer on top — Sacrieur attacks if a single-turn close+cast is
possible, then retreats; a long-range class with reach beyond the tofu
threshold may not need to retreat at all and can simply ignore the
detector. Per-class detector tuning (threshold, required_cycles) is
passed to the constructor, with sensible defaults read from CFG.

Module reads `tofu_detect_threshold` (default 4) and
`tofu_detect_required_cycles` (default 3) from utils.CFG for the
no-arg construction case.
"""
import random

from fighter.walking import walk_away
from utils import CFG


TOFU_THRESHOLD = int(CFG.get("tofu_detect_threshold", 4))
TOFU_REQUIRED_CYCLES = int(CFG.get("tofu_detect_required_cycles", 3))


class TofuTracker:
    """Detects hit-and-run kiter patterns via turn-start distance samples.

    Call observe_turn_start(dist) once per OUR turn-start with the Po
    distance to the nearest alive enemy BEFORE we move. That snapshot
    is the cycle's "max" -- where the enemy ended up after its retreat.
    If the last `required_cycles` samples are all > `threshold` AND the
    sequence is not strictly decreasing, flip tofu_detected.

    Sampling mid-cycle would conflate the enemy's approach distance with
    our own post-move position; the pre-move sample is the clean signal.

    `tofu_detected` is sticky once set; call clear() to reset (e.g. when
    we successfully glued the kiter to us via a pull spell)."""

    def __init__(self, threshold=None, required_cycles=None):
        self.threshold = TOFU_THRESHOLD if threshold is None else threshold
        self.required = TOFU_REQUIRED_CYCLES if required_cycles is None else required_cycles
        self.history = []
        self.tofu_detected = False

    def observe_turn_start(self, dist):
        """Record `dist` and re-evaluate. Returns the recorded value
        (None if `dist` was None or non-positive, meaning we couldn't
        measure this turn)."""
        if dist is None or dist <= 0:
            return None
        self.history.append(dist)
        if self.tofu_detected:
            return dist
        if len(self.history) >= self.required:
            recent = self.history[-self.required:]
            all_high = all(d > self.threshold for d in recent)
            strictly_decreasing = all(
                recent[i + 1] < recent[i] for i in range(len(recent) - 1)
            )
            if all_high and not strictly_decreasing:
                self.tofu_detected = True
        return dist

    def clear(self):
        """Drop the detected flag (caller broke the kite, e.g. via a pull)."""
        self.tofu_detected = False


def retreat_from_nearest(state, cal, static_obstacles, anchor_cell, mp_remaining):
    """Walk a random 0..mp_remaining steps away from `anchor_cell`.

    The randomized distance is the key trick: a predictable retreat
    distance lets the kiter compensate; randomized, they can't plan a
    fixed approach. Returns (me_cell, mp_remaining, pending_settle) --
    same shape walk_away returns. If steps==0, returns immediately
    without clicking (caller can fall through to pass turn)."""
    if mp_remaining <= 0:
        return None, 0, 0.0
    steps = random.randint(0, mp_remaining)
    print(f"  [tofu] retreating {steps} step(s) away from cell={anchor_cell} "
          f"(mp_left={mp_remaining})")
    if steps == 0:
        return None, mp_remaining, 0.0
    return walk_away(anchor_cell, state, cal, static_obstacles, max_steps=steps)
