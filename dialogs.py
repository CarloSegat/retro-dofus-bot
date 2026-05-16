"""Detect and click 'Ignore [player]' link in trade/challenge dialogs via OCR."""
import time
from pathlib import Path
import pytesseract
from PIL import Image

from utils import press_xdotool

DEBUG_DIR = Path(__file__).with_name("debug")
DEBUG_DIR.mkdir(exist_ok=True)

# Words that signal the in-game popup menu is open. Hitting any of these
# coordinates with a mining click is dangerous (Logout = lose the session).
MENU_KEYWORDS = ("logout", "log out", "character selection", "quit", "exit")


def _grab_dialog_region(ctx):
    area = ctx.cfg["dialog_search_area"]
    x1, y1 = area[0]
    x2, y2 = area[1]
    w, h = x2 - x1, y2 - y1
    img = ctx.grab_region(ctx.sct, x1, y1, w, h)
    pil_img = Image.fromarray(img)
    pil_img.save(DEBUG_DIR / "dialog_search_area.png")
    return x1, y1, pil_img


def find_ignore_link(ctx):
    """Return screen (x, y) of the 'Ignore' link if found, else None."""
    x1, y1, pil_img = _grab_dialog_region(ctx)
    data = pytesseract.image_to_data(pil_img, output_type=pytesseract.Output.DICT)

    for i, word in enumerate(data["text"]):
        if "ignore" in word.lower() and int(data["conf"][i]) > 0:
            cx = data["left"][i] + data["width"][i] // 2
            cy = data["top"][i] + data["height"][i] // 2
            return int(x1 + cx), int(y1 + cy)
    return None


def dismiss_dialog(ctx):
    """Click 'Ignore' if dialog is visible. Returns True if clicked."""
    pos = find_ignore_link(ctx)
    if pos is None:
        return False
    print(f"[dialog] clicking Ignore at {pos}")
    ctx.click(*pos)
    time.sleep(ctx.cfg.get("dialog_dismiss_wait_sec", 0.5))
    return True


def _read_popup_text(ctx, tag="check"):
    """OCR the popup search area. Saves a timestamped screenshot + OCR text
    dump under debug/ for postmortem when detection misfires.

    Returns (lowercased text, image path)."""
    cfg = ctx.cfg
    area = cfg.get("character_popup_search_area") or cfg["game_area"]
    p1, p2 = area
    x1, y1 = min(p1[0], p2[0]), min(p1[1], p2[1])
    x2, y2 = max(p1[0], p2[0]), max(p1[1], p2[1])
    w, h = x2 - x1, y2 - y1
    img = ctx.grab_region(ctx.sct, x1, y1, w, h)
    pil_img = Image.fromarray(img)
    text = pytesseract.image_to_string(pil_img).lower()
    ts = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    img_path = DEBUG_DIR / f"popup-{tag}-{ts}.png"
    txt_path = DEBUG_DIR / f"popup-{tag}-{ts}.txt"
    pil_img.save(img_path)
    txt_path.write_text(text)
    # Also keep the legacy fixed-name copy for callers that look for it.
    pil_img.save(DEBUG_DIR / "character_popup_search_area.png")
    print(f"[popup] OCR area=({x1},{y1})-{w}x{h} -> {img_path.name} text={text!r}")
    return text, img_path


def is_character_selection_open(ctx):
    """Return True if the in-game menu showing 'Character selection' is visible."""
    text, _ = _read_popup_text(ctx, tag="charsel")
    return "character selection" in text


def detect_menu_keyword(ctx, tag="check"):
    """Return the first MENU_KEYWORDS hit in the popup area, or None.

    Used to confirm the in-game menu (which includes Logout/Quit/Character
    selection) is NOT covering mining spots before we resume clicking."""
    text, _ = _read_popup_text(ctx, tag=tag)
    for kw in MENU_KEYWORDS:
        if kw in text:
            return kw
    return None


def ensure_safe_to_resume(ctx, max_retries=4, wait_sec=0.4):
    """Press Esc until no menu keyword is detected. Returns True if the screen
    is clean (safe to click), False if a menu is still visible after retries."""
    for attempt in range(1, max_retries + 1):
        hit = detect_menu_keyword(ctx, tag=f"attempt{attempt}")
        if hit is None:
            return True
        print(f"[popup] attempt {attempt}/{max_retries}: '{hit}' visible -> pressing Esc")
        press_xdotool("Escape")
        time.sleep(wait_sec)
    final_hit = detect_menu_keyword(ctx, tag="final")
    if final_hit is None:
        return True
    print(f"[popup] '{final_hit}' still visible after {max_retries} Esc presses")
    return False
