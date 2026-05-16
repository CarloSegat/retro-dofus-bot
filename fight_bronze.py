"""Combat for bronze resource protector: scripted placement + spell cycle."""
import threading
import time
import pyautogui
from pynput.mouse import Button, Controller as MouseController

# TODO(auto-fighter): every name below except pass_turn is screen-detection
# (cv2 template / color match). Replace with proxy data:
#   find_fight_target -> mob_groups from ProxyState
#   find_attack_target -> alive enemy in fight_entities
#   _play_alert / _screenshot_loop -> keep or drop, but not from fight.py
from fight import (
    _play_alert,
    _screenshot_loop,
    find_attack_target,
    find_fight_target,
    pass_turn,
)

_mouse = MouseController()


def _right_click(x, y):
    # Hybrid: pyautogui reliably warps the cursor even after prior pyautogui activity;
    # pynput is what Dofus actually picks up for the right button on X11.
    before = _mouse.position
    pyautogui.moveTo(x, y, duration=0.1)
    time.sleep(0.12)
    arrived = _mouse.position
    _mouse.press(Button.right)
    time.sleep(0.1)
    _mouse.release(Button.right)
    print(f"    right-click target=({x},{y}) before={before} arrived={arrived}")


def _cast_spell(label, x, y, wait_sec):
    print(f"  CAST {label} (right-click) -> {x},{y}")
    _right_click(x, y)
    time.sleep(wait_sec)


def run_in_fight_cycle(ctx, check_active=True):
    """In-fight portion: placement_wait -> click placement -> pass_turn -> spell cycle.
    Assumes we are on the pre-fight placement screen.
    check_active=False skips the timeline-strip end-of-fight probe (use for manual testing
    against non-protector monsters where the probe is mis-calibrated)."""
    cfg = ctx.cfg
    placement = cfg["bronze_placement_points"]
    placement_wait = cfg["bronze_placement_wait_sec"]
    spell_a = cfg["bronze_spell_a_point"]
    spell_b = cfg["bronze_spell_b_point"]
    pass_turns = int(cfg["bronze_pass_turns"])
    cast_wait = cfg["bronze_cast_wait_sec"]
    time.sleep(placement_wait)
    for px, py in placement:
        ctx.click(px, py)
        time.sleep(0.3)
    pass_turn(ctx)

    def fight_over():
        return check_active and find_attack_target(ctx) is None

    while True:
        if fight_over():
            break
        _cast_spell("spell A", spell_a[0], spell_a[1], cast_wait)
        pass_turn(ctx)
        if fight_over():
            break
        _cast_spell("spell B", spell_b[0], spell_b[1], cast_wait)
        pass_turn(ctx)
        ended = False
        for i in range(pass_turns):
            if fight_over():
                ended = True
                break
            print(f"  pass-only turn {i + 1}/{pass_turns}")
            pass_turn(ctx)
        if ended:
            break


def try_fight_bronze(ctx):
    """Bronze protector fight: detect -> click fight -> placement + spell cycle.
    Returns True if a fight was started."""
    pt = find_fight_target(ctx)
    if pt is None:
        return False
    fx, fy = pt
    print(f"  BRONZE FIGHT -> {fx},{fy}")
    _play_alert()
    stop_screenshots = threading.Event()
    screenshot_thread = threading.Thread(
        target=_screenshot_loop, args=(ctx.cfg, stop_screenshots), daemon=True
    )
    screenshot_thread.start()
    try:
        ctx.click(fx, fy)
        run_in_fight_cycle(ctx)
    finally:
        stop_screenshots.set()
        screenshot_thread.join()
    return True
