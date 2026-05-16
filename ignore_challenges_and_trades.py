"""Standalone: poll for trade/challenge dialog and click Ignore. Esc stops."""
import time
import mss
from utils import CFG, make_ctx, EscStop
from dialogs import dismiss_dialog


def run():
    esc = EscStop()
    poll = CFG.get("dialog_poll_sec", 1.0)
    print("[ignore] running. Press Esc to stop.")
    with mss.mss() as sct:
        ctx = make_ctx(sct)
        while not esc.stop:
            dismiss_dialog(ctx)
            time.sleep(poll)
    print("[ignore] stopped.")


if __name__ == "__main__":
    run()
