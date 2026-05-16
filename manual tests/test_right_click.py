"""Right-click isolation tests at [1821, 654].

Tries several methods one at a time so you can see which one Dofus picks up.
Usage:
    python3 test_right_click.py          # list and run all methods, 4s prep each
    python3 test_right_click.py 3        # run only method #3
    python3 test_right_click.py 2 5      # run methods #2 and #5

Watch for the spell context menu / cast effect in-game.
"""
import sys
import time
import pyautogui
from pynput.mouse import Button, Controller as MouseController

pyautogui.PAUSE = 0.05
pyautogui.FAILSAFE = True

X, Y = 1821, 654
PREP = 4.0
_mouse = MouseController()


def m1_pynput_press_release():
    _mouse.position = (X, Y)
    time.sleep(0.05)
    _mouse.press(Button.right)
    time.sleep(0.08)
    _mouse.release(Button.right)


def m2_pynput_click():
    _mouse.position = (X, Y)
    time.sleep(0.05)
    _mouse.click(Button.right, 1)


def m3_pynput_press_hold_release():
    """Some games need the button held for ~200ms."""
    _mouse.position = (X, Y)
    time.sleep(0.05)
    _mouse.press(Button.right)
    time.sleep(0.25)
    _mouse.release(Button.right)


def m4_pyautogui_rightClick():
    pyautogui.rightClick(X, Y)


def m5_pyautogui_move_then_click():
    pyautogui.moveTo(X, Y, duration=0.1)
    pyautogui.click(button="right")


def m6_pyautogui_mousedown_mouseup():
    pyautogui.moveTo(X, Y, duration=0.1)
    pyautogui.mouseDown(button="right")
    time.sleep(0.15)
    pyautogui.mouseUp(button="right")


def m7_pyautogui_move_pynput_click():
    """Mix: pyautogui smooth move (mimics human), pynput for the click."""
    pyautogui.moveTo(X, Y, duration=0.15)
    time.sleep(0.1)
    _mouse.press(Button.right)
    time.sleep(0.08)
    _mouse.release(Button.right)


def m8_pynput_double_right_click():
    _mouse.position = (X, Y)
    time.sleep(0.05)
    _mouse.click(Button.right, 1)
    time.sleep(0.1)
    _mouse.click(Button.right, 1)


METHODS = [
    ("pynput press+release", m1_pynput_press_release),
    ("pynput .click(right)", m2_pynput_click),
    ("pynput press+hold 250ms+release", m3_pynput_press_hold_release),
    ("pyautogui.rightClick(x,y)", m4_pyautogui_rightClick),
    ("pyautogui moveTo + click(right)", m5_pyautogui_move_then_click),
    ("pyautogui mouseDown/mouseUp right", m6_pyautogui_mousedown_mouseup),
    ("pyautogui move + pynput press/release", m7_pyautogui_move_pynput_click),
    ("pynput double right-click", m8_pynput_double_right_click),
]


def prep(label):
    print(f"\n=== {label} at ({X},{Y}) ===  switch to game NOW")
    for i in range(int(PREP), 0, -1):
        print(f"  {i}")
        time.sleep(1)


def run_one(idx):
    name, fn = METHODS[idx]
    prep(f"#{idx + 1} {name}")
    before = _mouse.position
    fn()
    time.sleep(0.3)
    after = _mouse.position
    print(f"  done. mouse before={before} after={after}")
    print(f"  -> did the menu/spell trigger? (#{idx + 1} {name})")


def main():
    if len(sys.argv) > 1:
        indices = [int(a) - 1 for a in sys.argv[1:]]
    else:
        print("methods:")
        for i, (n, _) in enumerate(METHODS, 1):
            print(f"  {i}. {n}")
        indices = list(range(len(METHODS)))
    for i in indices:
        if not (0 <= i < len(METHODS)):
            print(f"skip out-of-range #{i + 1}")
            continue
        try:
            run_one(i)
            time.sleep(1.5)
        except KeyboardInterrupt:
            print("\ninterrupted.")
            return


if __name__ == "__main__":
    main()
