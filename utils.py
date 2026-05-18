"""Top-level shared state: config (CFG) and the legacy `ctx` bundle.

The I/O helpers that used to live here moved to dedicated packages:
  mouse_keyboard/  -- move_to/click/press primitives, click_at, ...
  vision/          -- grab_region, OCR-based popup detection, ...

`make_ctx` is the last hold-over from the miner project's "context
namespace" pattern. Newer code should import what it needs directly
instead of going through `ctx`. `ctx.click(x, y)` resolves to
mouse_keyboard.click_at."""
import json
from pathlib import Path
from types import SimpleNamespace

from mouse_keyboard import click_at
from vision import grab_region

CFG = json.loads(Path(__file__).with_name("config.json").read_text())


def make_ctx(sct):
    """Bundle shared resources for callers that still take a `ctx`
    (vision OCR helpers, fight.pass_turn). The fields it exposes match
    historical usage: cfg dict, mss instance, grab_region fn, click fn."""
    return SimpleNamespace(cfg=CFG, sct=sct, grab_region=grab_region, click=click_at)
