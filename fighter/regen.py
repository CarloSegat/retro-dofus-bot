"""HP regeneration between fights.

Sits via /sit when HP is below threshold, waits until HP catches up,
gets back up when combat starts again. Tracks ProxyState.sitting -- the
server doesn't broadcast sit-state, so the Python side has to set it
explicitly on /sit and clear it on fight_engage.
"""
import time

from dofus.actions import sit
from fighter.helpers import wait_for
from utils import CFG

HP_WAIT_TIMEOUT = 300.0
HP_POLL_SEC = 1.0
HP_LOG_SEC = 10.0


class HpRegen:
    """Wait-until-healed gate. Registered on Combat.on_fight_engaged
    to clear sitting state once a fight starts."""

    def __init__(self, state, min_hp):
        self.state = state
        self.min_hp = min_hp

    def on_fight_engaged(self, snap):
        """Combat callback: a fight started, so we're standing again
        whether we like it or not. Clear the sitting flag so the next
        between-fights cycle can /sit again."""
        if self.state.snapshot().sitting:
            self.state.set_sitting(False)

    def wait_for_threshold(self):
        """Block until estimated HP >= min(min_hp, my_life_max), or
        timeout, or we entered a fight mid-wait. Returns True iff HP
        threshold reached.

        Sit-once: /sit fires the first iteration below threshold and
        stays on until on_fight_engaged clears it. Refusing to engage
        when my_life_max == 0 (no As packet yet) is intentional --
        engaging blind is the bug this method prevents."""
        deadline = time.time() + HP_WAIT_TIMEOUT
        announced = False
        last_log = 0.0
        sat_down = False
        while time.time() < deadline:
            snap = self.state.snapshot()
            if snap.in_fight:
                print(f"[fighter] entered fight while waiting for HP; aborting wait")
                return False
            if snap.my_life_max > 0:
                cap = min(self.min_hp, snap.my_life_max)
                est = snap.estimated_life()
                eff = snap.effective_regen_ms()
                rate_str = (f"regen={eff}ms/hp (sitting, raw={snap.my_life_regen_ms})"
                            if snap.sitting else f"regen={eff}ms/hp")
                if est >= cap:
                    if announced:
                        print(f"[fighter] HP ~{est}/{snap.my_life_max} >= {cap}; engaging "
                              f"(anchor={snap.my_life}, {rate_str})")
                    return True
                if not sat_down and not snap.sitting:
                    self._sit_for_regen()
                    sat_down = True
                    continue
                if not announced:
                    print(f"[fighter] HP ~{est}/{snap.my_life_max} below threshold {cap}; "
                          f"waiting (anchor={snap.my_life}, {rate_str})")
                    announced = True
                    last_log = time.time()
                elif time.time() - last_log >= HP_LOG_SEC:
                    print(f"[fighter] still regenerating: HP ~{est}/{snap.my_life_max} (need {cap})")
                    last_log = time.time()
            else:
                if not announced or time.time() - last_log >= HP_LOG_SEC:
                    print(f"[fighter] no HP info from proxy yet (no 'As' packet seen); "
                          f"holding engage. Complete any stat change to populate it.")
                    announced = True
                    last_log = time.time()
            time.sleep(HP_POLL_SEC)
        snap = self.state.snapshot()
        print(f"[fighter] HP wait timed out after {HP_WAIT_TIMEOUT}s at "
              f"~{snap.estimated_life()}/{snap.my_life_max}; refusing to engage blind")
        return False

    def _sit_for_regen(self):
        """Pre-condition: we are currently STANDING. /sit is a toggle
        so the caller must know this. on_fight_engaged clears the
        sitting flag whenever combat starts."""
        print("[fighter] /sit to regen faster")
        sit()
        self.state.set_sitting(True)
