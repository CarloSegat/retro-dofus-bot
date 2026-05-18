"""Pass-turn hotkey. Thin wrapper around mouse_keyboard.press (pyautogui)."""
import time

from mouse_keyboard import press


def pass_turn(ctx):
    """Fire the pass-turn hotkey configured via cfg['pass_turn_hotkey']
    (a single key string, e.g. 'e').

    Settles briefly first so the server has observed our last action,
    then presses the key. The caller is responsible for waiting until
    our next GTS<myID> turn-start (see main.wait_for_my_turn) -- this
    function returns immediately after the keypress."""
    cfg = ctx.cfg
    pre_delay = cfg.get("pass_turn_pre_delay_sec", 1.5)
    time.sleep(pre_delay)
    key = cfg.get("pass_turn_hotkey", "e")
    print(f"  pass_turn: pressing {key!r} (after {pre_delay}s settle)")
    press(key)
