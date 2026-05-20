"""Inventory: pod-weight wrapper around ProxyState.

The Dofus retro `Ow` packet (`Ow<cur>|<?>|<soft>|<hard>`) carries the
character's current inventory weight in pods, a soft cap (1043 on
Marx-Rockfeller), and a hard cap (3900). Movement penalties are
threshold-based, not gradual:

  pods <  soft_cap : no penalty, walks at full speed
  soft_cap <= pods <  hard_cap : OVERWEIGHT -- each step takes
       noticeably longer; long enough that the engager's 5s walk-to-mob
       timeout starts firing on otherwise-successful click-engages
  pods >= hard_cap : character cannot move at all (per operator)

This class is a thin read-through on top of `ProxyState.snapshot()`.
It holds no state of its own -- pods are always live from the proxy.

API:
  Inventory(state) -- wrap a ProxyState
  inv.pods / pods_max / pods_max_overweight -- raw numbers
  inv.is_overweight -- True iff walk speed is degraded (>= soft cap)
  inv.is_blocked    -- True iff movement is impossible (>= hard cap)
  inv.known         -- False until the first Ow packet arrives
  inv.summary()     -- one-line string for logs
  inv.on_fight_ended(snap) -- combat hook; logs current weight

The engager checks `is_overweight` before each engage attempt and
refuses to start a new fight while degraded; that's the only place
inventory affects bot behaviour today. Combat-end is wired up just
for observability -- the per-Ow snapshot already drives the gate.
"""
from dataclasses import dataclass


@dataclass
class Inventory:
    """Pod-weight view over a ProxyState. Stateless; reads through to
    the latest snapshot on every property access."""
    state: object  # ProxyState (avoid circular import)

    @property
    def pods(self) -> int:
        return self.state.snapshot().pods

    @property
    def pods_max(self) -> int:
        return self.state.snapshot().pods_max

    @property
    def pods_max_overweight(self) -> int:
        return self.state.snapshot().pods_max_overweight

    @property
    def known(self) -> bool:
        """False until the first Ow packet has arrived. The proxy
        initialises all three fields to 0; pods_max == 0 is the
        unambiguous 'no data yet' signal."""
        return self.state.snapshot().pods_max > 0

    @property
    def is_overweight(self) -> bool:
        """Soft cap exceeded -- walk speed degraded. Returns False
        when inventory data hasn't arrived yet so a missed Ow doesn't
        wedge the bot."""
        snap = self.state.snapshot()
        if snap.pods_max <= 0:
            return False
        return snap.pods >= snap.pods_max

    @property
    def is_blocked(self) -> bool:
        """Hard cap reached -- movement impossible."""
        snap = self.state.snapshot()
        if snap.pods_max_overweight <= 0:
            return False
        return snap.pods >= snap.pods_max_overweight

    def summary(self) -> str:
        snap = self.state.snapshot()
        if snap.pods_max <= 0:
            return "pods=? (no Ow seen yet)"
        flag = ""
        if snap.pods >= snap.pods_max_overweight > 0:
            flag = " BLOCKED"
        elif snap.pods >= snap.pods_max:
            flag = " OVERWEIGHT"
        return (f"pods={snap.pods}/{snap.pods_max} "
                f"(hard={snap.pods_max_overweight}){flag}")

    def on_fight_ended(self, snap):
        """Combat.on_fight_ended hook. Logs the post-fight weight so
        the operator can see the loot delta accumulating across
        fights; flips the engager's gate by exposing is_overweight
        next tick (no action needed here)."""
        print(f"[inventory] fight ended: {self.summary()}")
