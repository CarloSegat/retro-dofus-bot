"""Print screen coordinates on every left-click. Exits on right-click.

Usage:
  python3 -u picker.py

Hover over the spot you want, left-click to print its (x, y), repeat.
Right-click anywhere to quit. Coords are absolute screen pixels, in the
same coordinate space as pyautogui/xdotool/config.json -- paste them
straight into config knobs that take a screen position.

Uses pynput's global mouse listener (works while another window has
focus, e.g. the Dofus client). The click itself still goes through to
whatever's underneath -- the listener just observes it; if you don't
want to actually click on the target, move your finger off the button
first or use a benign area.
"""
from pynput import mouse


def on_click(x, y, button, pressed):
    if not pressed:
        return  # only fire on press, not release
    if button == mouse.Button.left:
        print(f"({int(x)}, {int(y)})")
    elif button == mouse.Button.right:
        print("right-click -> exit")
        return False  # stop the listener


def main():
    print("picker: left-click to print coords, right-click to quit")
    with mouse.Listener(on_click=on_click) as listener:
        listener.join()


if __name__ == "__main__":
    main()
