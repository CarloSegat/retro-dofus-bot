"""Shared helpers: config, screen capture, click, ctx, Esc stop."""
import json
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pyautogui
from pynput import keyboard

CFG = json.loads(Path(__file__).with_name("config.json").read_text())


def grab_region(sct, x, y, w, h):
    """RGB ndarray of a screen rectangle. Used by dialogs.py for OCR."""
    mon = {"left": x, "top": y, "width": w, "height": h}
    img = np.array(sct.grab(mon))[:, :, :3]
    return img[:, :, ::-1]


def click(x, y):
    pyautogui.moveTo(x, y, duration=0.1)
    pyautogui.click()


def make_ctx(sct):
    return SimpleNamespace(cfg=CFG, sct=sct, grab_region=grab_region, click=click)


class EscStop:
    """Esc-to-stop flag with pause/resume so callers can synthesize Esc presses."""

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
