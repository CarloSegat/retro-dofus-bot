"""Mouse and keyboard I/O for the bot.

One backend for injection (xdotool). One for listening (pynput, EscStop
only). Layered:

  Primitives      move_to(x,y) / click() / press(key)
  Composed        click_at(x,y) = move_to + click
                  type_text(text) = press per character
  Focused         click_at_focused / press_focused / type_text_focused
                  -- windowactivate --sync the Dofus window first
                  before the action. Use when focus may have shifted
                  (spell-aim clicks, post-fight Esc, chat typing).

The un-focused variants assume Dofus already has focus from a prior
user interaction. That's true in practice once the bot has clicked
inside the game once. If a click silently no-ops, try the focused
variant.

Why no pyautogui: it was the original backend for the easy cases but
silently dropped clicks in spell-aim mode and couldn't windowactivate.
xdotool handles every case, so pyautogui is gone.
"""
import subprocess
import time

from pynput import keyboard

# Sleep after each xdotool call. The bot's timing was tuned with
# pyautogui's PAUSE=0.05 implicit-pause in place; preserve it so the
# click/key cadence stays roughly identical post-migration.
# TODO(verify-bot-run): is 0.05s the right pause? If clicks now register
# too fast (Dofus drops some) or too slow (bot feels sluggish), tune.
_POST_ACTION_PAUSE_SEC = 0.05


def _xdotool(*args):
    """Run xdotool with args (no leading "xdotool"), raising on
    non-zero exit, then sleep the post-action pause."""
    subprocess.run(["xdotool", *args], check=True)
    time.sleep(_POST_ACTION_PAUSE_SEC)


def _focus_dofus_window():
    """windowactivate --sync the Dofus Retro window so Wine accepts
    synthetic input. Returns the window id (or None if not found) so
    callers can pass --window to their xdotool action.

    No POST_ACTION_PAUSE here -- windowactivate --sync already blocks
    until the activation completes."""
    wid = dofus_window_id()
    if wid:
        subprocess.run(
            ["xdotool", "windowactivate", "--sync", wid], check=False
        )
    return wid


def _maybe_window(wid):
    """['--window', WID] when WID is set, else []. For inlining into
    xdotool arg lists."""
    return ["--window", wid] if wid else []


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


# ============================================================
# PRIMITIVES
# ============================================================

def move_to(x, y):
    """Move the cursor to absolute screen (x, y). No click."""
    _xdotool("mousemove", str(x), str(y))


def click():
    """Left-click at the current cursor position. No motion."""
    _xdotool("click", "1")


def press(key, hold_sec=0.1):
    """Press one key with a 100ms button-down hold.

    The hold mimics a real keystroke; Dofus retro samples input and
    silently drops sub-millisecond down+up taps (we hit this with both
    pyautogui.press and bare `xdotool key`). `key` uses X11 names
    (e.g. 'Escape', not 'esc')."""
    _xdotool("keydown", key)
    time.sleep(hold_sec)
    _xdotool("keyup", key)


# ============================================================
# COMPOSED (no focus)
# ============================================================

def click_at(x, y):
    """Move cursor + left-click at (x, y). Standard map click
    (engage / walk).

    No windowactivate -- relies on Dofus already having focus. If a
    click silently no-ops, use click_at_focused instead."""
    # TODO(verify-bot-run): pre-migration this was pyautogui.click();
    # now it's xdotool. Did engage/walk clicks still land in your last
    # run? If yes, remove this TODO.
    move_to(x, y)
    click()


def type_text(text):
    """Type a string by pressing each character in sequence.

    Lowercase + symbols only -- no shift modifier handling. Built on
    `press` rather than `xdotool type` so the 'typing = pressing keys'
    model is visible in the code."""
    for c in text:
        press(c, hold_sec=0.02)


# ============================================================
# FOCUSED VARIANTS (windowactivate Dofus first)
# ============================================================
# Use when focus may have shifted -- spell-aim clicks, post-fight Esc,
# chat typing. These pay the focus cost; the un-focused variants
# above rely on Dofus already owning keyboard/click focus from the
# user's last interaction.

def click_at_focused(x, y):
    """Move cursor, windowactivate Dofus, then click with a 120ms
    button-down delay targeted at the Dofus window.

    Use for the second click of a spell cast (spell-aim mode). Regular
    click_at is silently dropped there -- the spell stays armed,
    cursor on target, but my_ap never decrements. The 120ms delay plus
    explicit --window targeting gets the cast through. See CLAUDE.md
    and memory feedback_spell_click_pynput.md."""
    move_to(x, y)
    wid = _focus_dofus_window()
    _xdotool("click", "--delay", "120", *_maybe_window(wid), "1")


def press_focused(key, hold_sec=0.1):
    """Press one key after windowactivate, with keyup/keydown
    --window-targeted at Dofus.

    Use when focus may have shifted (post-fight Esc -- the XP-summary
    transition can leave focus on the terminal, and an un-focused key
    event is dropped by Wine)."""
    wid = _focus_dofus_window()
    _xdotool("keydown", *_maybe_window(wid), key)
    time.sleep(hold_sec)
    _xdotool("keyup", *_maybe_window(wid), key)


def type_text_focused(text):
    """Type a string into the focused Dofus window: focus once, then
    `press` each character.

    Used for chat commands like '/sit'. Lowercase + symbols only --
    same shift-handling caveat as type_text."""
    # TODO(verify-bot-run): pre-migration this was xdotool type
    # --delay 20 in one shot; now it's press-per-char (focus + plain
    # press). Did the /sit chat command still type correctly in your
    # last run? If yes, remove this TODO.
    _focus_dofus_window()
    for c in text:
        press(c, hold_sec=0.02)


class EscStop:
    """Esc-to-stop flag with pause/resume so callers can synthesize Esc
    presses without our own listener catching them. Uses
    pynput.keyboard.Listener for capture (xdotool can't listen)."""

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
