"""Dofus task-agnostic verbs.

Game-aware operations built on top of the mouse_keyboard primitives.
None of these functions know what task the bot is doing -- they don't
know we're fighting, what character we are, or which mob is the
target. Higher layers (e.g. main.py's fighter) compose these into
task-specific behaviour.

Verbs:
  click_cell(cell, cal)
  cast_at_cell(hotkey, cell, cal)
  pass_turn(hotkey, pre_delay_sec=1.5)
  say(text)
  sit()
"""
import time

from dofus.cell_grid import cell_to_screen
from mouse_keyboard import (
    click_at,
    click_at_focused,
    press,
    press_focused,
    type_text_focused,
)

# Pause between pressing a spell hotkey and clicking its target.
# Dofus needs this window to enter spell-aim mode (show the reticle);
# without it the click can land before the spell is armed, and Dofus
# interprets it as a plain move click instead of the cast.
SPELL_AIM_SETTLE_SEC = 0.4

# Pause around each chat keystroke: open chat line / send. Without
# these, Return-then-type can race the UI animation and the first
# characters land in the game canvas instead of the chat input.
CHAT_SETTLE_SEC = 0.3


def click_cell(cell, cal):
    """Click the pixel center of `cell`. Used to walk a step (Dofus
    interprets a click on a walkable cell as movement)."""
    x, y = cell_to_screen(cell, cal)
    click_at(x, y)


def cast_at_cell(hotkey, cell, cal):
    """Cast a spell at `cell`: press the spell `hotkey`, wait for Dofus
    to enter spell-aim mode (show the reticle), then click the target
    cell with focus.

    The click must be focused -- in spell-aim mode Dofus's spell handler
    silently drops un-focused clicks, leaving the spell armed but never
    firing (`my_ap` doesn't decrement). See CLAUDE.md and memory
    feedback_spell_click_pynput.md."""
    x, y = cell_to_screen(cell, cal)
    press(hotkey)
    time.sleep(SPELL_AIM_SETTLE_SEC)
    click_at_focused(x, y)


def pass_turn(hotkey, pre_delay_sec=1.5):
    """End your turn. Settles for `pre_delay_sec` first so the server
    has observed your last action, then presses the pass-turn hotkey.

    The caller is responsible for waiting until our next turn-start
    (GTS<myID>) -- this function returns immediately after the keypress."""
    time.sleep(pre_delay_sec)
    press(hotkey)


def say(text):
    """Send arbitrary text via chat: Return to open the chat line,
    type the text, Return to send. settle pauses give the chat-line
    UI time to focus -- without them the first characters can land in
    the game canvas instead."""
    press_focused("Return")
    time.sleep(CHAT_SETTLE_SEC)
    type_text_focused(text)
    time.sleep(CHAT_SETTLE_SEC)
    press_focused("Return")


def sit():
    """Send /sit via chat. /sit is a toggle in Dofus retro and the
    server doesn't broadcast sit-state, so callers must track it
    themselves (note: combat auto-stands the character)."""
    say("/sit")
