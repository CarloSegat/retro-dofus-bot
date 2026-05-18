"""Mouse and keyboard I/O for the bot.

Input simulation is split between two backends by Dofus's requirements:

  pyautogui  -- regular clicks (engage / walk) and regular key presses
                (`click`, `right_click`, `press`, `cursor_pos`,
                `_move_mouse`).
  xdotool    -- spell-aim clicks and any key event that needs the Dofus
                window focused (`spell_click`, `press_xdotool`,
                `type_xdotool`). pyautogui is silently dropped in
                spell-aim mode; xdotool also lets us `windowactivate
                --sync` first, which Wine requires before it accepts
                synthetic input.
  pynput     -- LISTENER ONLY (EscStop). pyautogui can inject but can't
                listen.

If a new Dofus mode silently drops pyautogui clicks, debug THAT case in
isolation -- don't replace the working pyautogui paths wholesale.
"""
import subprocess
import time

import pyautogui
from pynput import keyboard

pyautogui.FAILSAFE = True  # move mouse to top-left corner to abort
pyautogui.PAUSE = 0.05


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


def spell_click(x, y):
    """Click (x, y) via xdotool with a 120ms button-down delay,
    targeted at the Dofus Retro window.

    Use this for the second click of a spell cast (after pressing the
    spell hotkey, when Dofus is in spell-aim mode). Empirically,
    pyautogui's click is silently dropped in that mode -- the spell
    stays armed, cursor on target, but `my_ap` never decrements and
    the cast never fires. `xdotool click --delay 120 1` goes through.

    We `windowactivate --sync` the Dofus window first (same workaround
    as `press_xdotool`): under Wine, synthetic input is dropped when
    the target window isn't focused, even when `--window` is set on
    the xdotool command. See CLAUDE.md and memory
    `feedback_xdotool_focus.md`."""
    _move_mouse(x, y)
    wid = dofus_window_id()
    if wid:
        subprocess.run(["xdotool", "windowactivate", "--sync", wid], check=False)
    args = ["xdotool", "click", "--delay", "120"]
    if wid:
        args += ["--window", wid]
    args.append("1")
    subprocess.run(args, check=True)


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
