"""Combat: detect fight prompt, start fight, run scripted opening turn.

TODO(auto-fighter): this module is miner-era visual color-match code copied
verbatim from the miner project. The fighter should drive everything from
proxy data (fight_entities, my_cell, mob_groups, in_fight) instead of
screen detection. Refactor / replace each consumer below:

  - hunter.run_combat_cycle          (currently imported from here)
  - fight_bronze.find_attack_target / find_fight_target / _play_alert /
    _screenshot_loop / pass_turn
  - idle_wait_and_all_or_nothing.pass_turn

`pass_turn` is the only piece that is genuinely keyboard-only and worth
keeping; everything that calls cv2/mss/pyautogui locate on a screenshot
should go.
"""
import random
import subprocess
import threading
import time
from pathlib import Path
import cv2
import mss
import numpy as np
import pyautogui
from pynput.keyboard import Controller as KeyboardController, Key

_keyboard = KeyboardController()

# Map pyautogui-style key names to pynput Key enums. Unknown names fall
# through as literal characters (e.g. "e", "3").
_KEY_MAP = {
    "ctrl": Key.ctrl, "alt": Key.alt, "shift": Key.shift,
    "cmd": Key.cmd, "winleft": Key.cmd, "win": Key.cmd, "super": Key.cmd,
    "enter": Key.enter, "return": Key.enter,
    "esc": Key.esc, "escape": Key.esc,
    "tab": Key.tab, "space": Key.space, "backspace": Key.backspace,
}
for _i in range(1, 13):
    _KEY_MAP[f"f{_i}"] = getattr(Key, f"f{_i}")


def _resolve_key(name):
    return _KEY_MAP.get(name.lower(), name)


def _press_combo(keys):
    """Press a (possibly multi-key) combo via pynput: press in order, release
    in reverse. Equivalent to pyautogui.hotkey but goes through the same
    X11-friendly path the working right-click uses."""
    resolved = [_resolve_key(k) for k in keys]
    for k in resolved:
        _keyboard.press(k)
    time.sleep(0.05)
    for k in reversed(resolved):
        _keyboard.release(k)


def _play_alert():
    try:
        subprocess.Popen(
            ["paplay", "/usr/share/sounds/freedesktop/stereo/bell.oga"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print("\a", end="", flush=True)


ATTACK_TPL = cv2.imread(str(Path(__file__).with_name("attack_target.png")), cv2.IMREAD_COLOR)
ATTACK_COLOR_BGR = tuple(int(c) for c in ATTACK_TPL.reshape(-1, 3)[0])
ATTACK_TPL_H, ATTACK_TPL_W = ATTACK_TPL.shape[:2]

DEBUG_DIR = Path(__file__).with_name("debug")
DEBUG_DIR.mkdir(exist_ok=True)


def pass_turn(ctx):
    """Move mouse into the game area, then fire the pass-turn hotkey via
    pynput. Dofus retro on X11 does not reliably pick up pyautogui's
    keyboard events (same root cause as the right-click bug -- see
    feedback_right_click_pynput memory). pynput's KeyboardController works.
    The combo is configurable via cfg['pass_turn_hotkey'] (list of key
    names, default ['ctrl','e']; user typically sets to ['e'] or
    ['winleft','e'])."""
    cfg = ctx.cfg
    ga = cfg["game_area"]
    sx, sy = ga[0]
    pyautogui.moveTo(sx + 10, sy + 10, duration=0.1)
    pre_delay = cfg.get("pass_turn_pre_delay_sec", 1.5)
    time.sleep(pre_delay)
    keys = cfg.get("pass_turn_hotkey", ["ctrl", "e"])
    print(f"  pass_turn: pressing {keys} via pynput (after {pre_delay}s settle)")
    _press_combo(keys)
    time.sleep(cfg["fight_turn_wait_sec"])


def find_fight_target(ctx):
    """Find fight button by matching fight_rgb in game area.
    Returns (cx, cy) of the largest matching blob, or None."""
    cfg = ctx.cfg
    sa = cfg.get("fight_search_area") or cfg["game_area"]
    p1, p2 = sa
    gx1, gy1 = min(p1[0], p2[0]), min(p1[1], p2[1])
    gx2, gy2 = max(p1[0], p2[0]), max(p1[1], p2[1])
    gw, gh = gx2 - gx1, gy2 - gy1
    img = ctx.grab_region(ctx.sct, gx1, gy1, gw, gh)
    target = np.array(cfg["fight_rgb"], dtype=np.int16)
    tol = cfg.get("fight_tolerance", 40)
    arr = img.astype(np.int16)
    mask = (np.abs(arr - target).max(axis=2) <= tol).astype(np.uint8)
    count = int(mask.sum())
    blob_min = cfg.get("fight_min_blob_pixels", 20)
    if count == 0:
        return None
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 1:
        return None
    candidates = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= blob_min]
    if not candidates:
        return None
    idx = max(candidates, key=lambda i: stats[i, cv2.CC_STAT_AREA])
    cx, cy = cents[idx]
    ts = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time()*1000)%1000:03d}"
    region_path = DEBUG_DIR / f"fight-region-{ts}.png"
    mask_path = DEBUG_DIR / f"fight-mask-{ts}.png"
    cv2.imwrite(str(region_path), img[:, :, ::-1])
    cv2.imwrite(str(mask_path), mask * 255)
    all_blobs = sorted(
        ((int(stats[i, cv2.CC_STAT_AREA]), gx1 + int(cents[i][0]), gy1 + int(cents[i][1])) for i in range(1, n)),
        reverse=True,
    )
    print(f"  fight pixels={count} blob_min={blob_min} target_rgb={cfg['fight_rgb']} tol={tol} area=({gx1},{gy1})-({gx2},{gy2}) saved={region_path}")
    print(f"  fight blobs ({n-1}): {all_blobs[:10]}")
    return (gx1 + int(cx), gy1 + int(cy))


def find_attack_target(ctx):
    """Find exact-blue strip on screen matching attack_target.png.
    Returns (cx, cy) of the largest matching blob, or None."""
    cfg = ctx.cfg
    sa = cfg.get("attack_search_area") or cfg["game_area"]
    p1, p2 = sa
    gx1, gy1 = min(p1[0], p2[0]), min(p1[1], p2[1])
    gx2, gy2 = max(p1[0], p2[0]), max(p1[1], p2[1])
    gw, gh = gx2 - gx1, gy2 - gy1
    img = ctx.grab_region(ctx.sct, gx1, gy1, gw, gh)
    bgr = img[:, :, ::-1]
    b, g, r = ATTACK_COLOR_BGR
    tol = cfg.get("attack_color_tolerance", 20)
    arr = bgr.astype(np.int16)
    mask = (
        (np.abs(arr[:, :, 0] - b) <= tol) &
        (np.abs(arr[:, :, 1] - g) <= tol) &
        (np.abs(arr[:, :, 2] - r) <= tol)
    ).astype(np.uint8)
    count = int(mask.sum())
    blob_min = cfg.get("attack_min_blob_pixels", 20)
    ts = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time()*1000)%1000:03d}"
    region_path = DEBUG_DIR / f"attack-region-{ts}.png"
    mask_path = DEBUG_DIR / f"attack-mask-{ts}.png"
    cv2.imwrite(str(region_path), bgr)
    cv2.imwrite(str(mask_path), mask * 255)
    print(f"  attack pixels={count} blob_min={blob_min} target_bgr={ATTACK_COLOR_BGR} area=({gx1},{gy1})-({gx2},{gy2}) saved={region_path}")
    if count == 0:
        return None
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 1:
        return None
    all_blobs = [(int(stats[i, cv2.CC_STAT_AREA]), gx1 + int(cents[i][0]), gy1 + int(cents[i][1])) for i in range(1, n)]
    print(f"  attack blobs ({n-1}): {sorted(all_blobs, reverse=True)}")
    candidates = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= blob_min]
    if not candidates:
        return None
    idx = random.choice(candidates)
    cx, cy = cents[idx]
    return (gx1 + int(cx), gy1 + int(cy))


def _screenshot_loop(cfg, stop_evt, interval=3):
    """Save a full game-area screenshot every `interval` seconds until stop_evt is set."""
    ga = cfg["game_area"]
    (gx1, gy1), (gx2, gy2) = ga
    gw, gh = gx2 - gx1, gy2 - gy1
    with mss.mss() as sct:
        while not stop_evt.wait(interval):
            img = sct.grab({"left": gx1, "top": gy1, "width": gw, "height": gh})
            arr = np.array(img)[:, :, :3][:, :, ::-1]
            ts = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time()*1000)%1000:03d}"
            path = DEBUG_DIR / f"fight-{ts}.png"
            cv2.imwrite(str(path), arr)
            print(f"  [fight-screenshot] saved {path.name}")


def run_combat_cycle(ctx):
    """In-fight combat loop. Assumes we're on (or about to enter) the
    placement screen. Waits fight_ready_wait_sec, passes placement, then
    loops attack-attack-pass until no attack target is visible."""
    cfg = ctx.cfg
    time.sleep(cfg["fight_ready_wait_sec"])
    pass_turn(ctx)
    while True:
        found_any = False
        for _ in range(2):
            if not try_to_attack(ctx):
                break
            found_any = True
            time.sleep(cfg["fight_attack_wait_sec"])
        if not found_any:
            break
        pass_turn(ctx)


def try_fight(ctx):
    """If fight button detected, click it and run the attack sequence. Returns True if fight started."""
    pt = find_fight_target(ctx)
    if pt is None:
        return False
    fx, fy = pt
    print(f"  FIGHT -> {fx},{fy}")
    _play_alert()
    cfg = ctx.cfg
    stop_screenshots = threading.Event()
    screenshot_thread = threading.Thread(
        target=_screenshot_loop, args=(cfg, stop_screenshots), daemon=True
    )
    screenshot_thread.start()
    try:
        ctx.click(fx, fy)
        run_combat_cycle(ctx)
    finally:
        stop_screenshots.set()
        screenshot_thread.join()
    return True


def try_to_attack(ctx):
    """Find attack target on screen; on hit press 3 and click it."""
    pt = find_attack_target(ctx)
    if pt is None:
        return False
    cx, cy = pt
    print(f"  ATTACK -> {cx},{cy}")
    pyautogui.press("3")
    time.sleep(0.2)
    ctx.click(cx, cy)
    return True
