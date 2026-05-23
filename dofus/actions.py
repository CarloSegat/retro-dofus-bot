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

from dofus.cell_grid import cell_to_screen, cell_to_screen_fight, cell_to_subrow_pos
from mouse_keyboard import (
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

# Fraction of cell_h to bump the click upward when targeting the cell
# that sits DIRECTLY NORTH of the caster on screen. In iso coords
# that's the Po-distance-2 cell with same pos and sub_row - 2 (one
# NW + one NE step); on screen its pixel center sits exactly one
# cell_h above the caster's. The caster sprite extends up past that
# point, so the click lands inside the sprite and Dofus resolves it
# as a self-click -- spell stays armed and never fires (or fires on
# us). Empirical: 15% of cell_h clears the sprite without overshooting.
NORTH_ABOVE_Y_OFFSET_PCT = 0.15


def click_cell(cell, cal):
    """Click the pixel center of `cell`. Used to walk a step (Dofus
    interprets a click on a walkable cell as movement).

    Uses the focused variant (windowactivate --sync + click) so the
    click goes through even when Dofus briefly lost X focus -- common
    right after a map transition, where an unfocused click is silently
    dropped and the bot stalls waiting for a map change that never
    happens. The extra ~50ms of windowactivate is negligible vs the
    20s timeout when the click is lost."""
    x, y = cell_to_screen(cell, cal)
    click_at_focused(x, y)


def cast_at_cell(hotkey, cell, cal, caster_cell=None):
    """Cast a spell at `cell`: press the spell `hotkey`, wait for Dofus
    to enter spell-aim mode (show the reticle), then click the target
    cell with focus.

    The click must be focused -- in spell-aim mode Dofus's spell handler
    silently drops un-focused clicks, leaving the spell armed but never
    firing (`my_ap` doesn't decrement). See CLAUDE.md and memory
    feedback_spell_click_pynput.md.

    If `caster_cell` is supplied and `cell` is the one sitting DIRECTLY
    NORTH of the caster on screen (iso: same pos, sub_row - 2; Po
    distance 2), the click is bumped up by `NORTH_ABOVE_Y_OFFSET_PCT`
    of cell_h so it lands above the caster sprite (which would
    otherwise eat the click as a self-click)."""
    x, y = cell_to_screen_fight(cell, cal)
    if caster_cell is not None:
        t_sub, t_pos = cell_to_subrow_pos(cell)
        c_sub, c_pos = cell_to_subrow_pos(caster_cell)
        if t_pos == c_pos and t_sub == c_sub - 2:
            dy = int(round(cal["cell_h"] * NORTH_ABOVE_Y_OFFSET_PCT))
            print(f"  [cast_at_cell] N-above target_cell={cell} from "
                  f"caster_cell={caster_cell}; bumping click up {dy}px "
                  f"({NORTH_ABOVE_Y_OFFSET_PCT*100:.0f}% of cell_h)")
            y -= dy
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
