"""Pass-turn hotkey via pynput.

Dofus Retro on X11 does not reliably pick up pyautogui keyboard events
(same root cause as the right-click bug -- see feedback memory
`feedback_right_click_pynput.md`). pynput's KeyboardController does.

This module exists only to drive the pass-turn key combo for the fighter
loop. Visual color-match combat (the miner-era `run_combat_cycle`,
`find_attack_target`, etc.) has been removed -- the fighter reads
everything it needs from the proxy."""
import time

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


def pass_turn(ctx):
    """Move mouse into the game area, then fire the pass-turn hotkey via
    pynput. Combo is configurable via cfg['pass_turn_hotkey'] (list of key
    names, default ['ctrl','e']; on Linux typically ['e'] or
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
