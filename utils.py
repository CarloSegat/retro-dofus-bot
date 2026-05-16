"""Shared helpers: config, screen capture, simulated input, Esc stop.

All simulated input -- clicks, key presses -- goes through pyautogui.
This matches the miner project's working pattern; pyautogui clicks land
reliably on Dofus retro / Wine for both engagement and walk clicks.

If a specific Dofus mode silently drops pyautogui clicks (we hit this
with spell-aim casts), debug THAT case in isolation -- don't replace
the library wholesale; the working clicks are too important to risk.

pynput is imported only for event capture (keyboard.Listener in
EscStop / calibrate_map_cells.py). pyautogui can inject but can't listen."""
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pyautogui
from pynput import keyboard

CFG = json.loads(Path(__file__).with_name("config.json").read_text())

pyautogui.FAILSAFE = True  # move mouse to top-left corner to abort
pyautogui.PAUSE = 0.05


def grab_region(sct, x, y, w, h):
    """RGB ndarray of a screen rectangle. Used by dialogs.py for OCR."""
    mon = {"left": x, "top": y, "width": w, "height": h}
    img = np.array(sct.grab(mon))[:, :, :3]
    return img[:, :, ::-1]


def _move_mouse(x, y, duration=0.1):
    """Warp cursor to absolute screen (x, y)."""
    pyautogui.moveTo(x, y, duration=duration)


def cursor_pos():
    """Current cursor screen position as (x, y)."""
    pos = pyautogui.position()
    return int(pos.x), int(pos.y)


def click(x, y):
    """Move to (x, y), then left-click."""
    _move_mouse(x, y)
    pyautogui.click()


def right_click(x, y):
    """Move to (x, y), then right-click."""
    _move_mouse(x, y)
    pyautogui.rightClick()


def press(key, hold_sec=0.1):
    """Press a single key with a real human-length hold.

    pyautogui.press fires keyDown+keyUp microseconds apart, which Dofus
    retro silently drops for spell-hotkey arming (its handler samples
    the key state at intervals and misses sub-millisecond taps).
    ~100ms is enough to mimic a real keystroke; pass-turn keys work at
    this hold too."""
    pyautogui.keyDown(key)
    time.sleep(hold_sec)
    pyautogui.keyUp(key)


_DOFUS_WINDOW_ID = None


def dofus_window_id():
    """Cached lookup of the Dofus Retro game window. Returns the X11
    window id as a string, or None if no match. Matches on "Dofus Retro"
    rather than bare "Dofus" because the box also runs an Electron app
    whose windows are titled "dofus1electron" -- we must not Esc those."""
    global _DOFUS_WINDOW_ID
    if _DOFUS_WINDOW_ID is not None:
        return _DOFUS_WINDOW_ID
    try:
        out = subprocess.check_output(
            ["xdotool", "search", "--name", "Dofus Retro"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except subprocess.CalledProcessError:
        return None
    if not out:
        return None
    _DOFUS_WINDOW_ID = out.split("\n")[0]
    return _DOFUS_WINDOW_ID


def press_xdotool(key, hold_sec=0.1):
    """Press a key via xdotool keydown/sleep/keyup, targeted at the
    Dofus Retro window when it can be found.

    Use when pyautogui's keystroke is silently dropped by Dofus (we hit
    this for the post-fight Esc that should close the XP-summary popup
    -- pyautogui press "esc" never reached the game because focus was
    on the terminal). We `windowactivate` the Dofus window first
    *and* pass `--window` to the key event: under Wine, synthetic key
    events targeted at an unfocused window are dropped, so focus is
    actually required despite the `--window` flag. Mirrors the
    keydown+hold+keyup shape of `press` so sub-millisecond taps aren't
    dropped by the client's input sampling. `key` uses X11 names
    (e.g. "Escape", not "esc")."""
    wid = dofus_window_id()
    if wid:
        subprocess.run(["xdotool", "windowactivate", "--sync", wid], check=False)
    base = ["xdotool"]
    args_down = base + ["keydown"]
    args_up = base + ["keyup"]
    if wid:
        args_down += ["--window", wid]
        args_up += ["--window", wid]
    args_down.append(key)
    args_up.append(key)
    subprocess.run(args_down, check=True)
    time.sleep(hold_sec)
    subprocess.run(args_up, check=True)


def type_xdotool(text, delay_ms=20):
    """Type a literal string into the Dofus window via xdotool.

    Used for chat commands like "/sit" -- press Enter first to open the
    chat, type the command, press Enter again to send. We windowactivate
    so xdotool's `type` lands in Dofus rather than the terminal; without
    focus, Wine drops synthetic input."""
    wid = dofus_window_id()
    if wid:
        subprocess.run(["xdotool", "windowactivate", "--sync", wid], check=False)
    args = ["xdotool", "type", "--delay", str(delay_ms)]
    if wid:
        args += ["--window", wid]
    args.append(text)
    subprocess.run(args, check=True)


def make_ctx(sct):
    return SimpleNamespace(cfg=CFG, sct=sct, grab_region=grab_region, click=click)


class EscStop:
    """Esc-to-stop flag with pause/resume so callers can synthesize Esc
    presses without our own listener catching them. Uses
    pynput.keyboard.Listener for capture (pyautogui can't listen)."""

    def __init__(self):
        self.stop = False
        self._listener = None
        self.start()

    def _on_press(self, k):
        if k == keyboard.Key.esc:
            self.stop = True
            return False

    def start(self):
        self._listener = keyboard.Listener(on_press=self._on_press, daemon=True)
        self._listener.start()

    def pause(self):
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def resume(self):
        if self._listener is None:
            self.start()
