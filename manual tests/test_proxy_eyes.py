"""Verify the proxy event stream works.

Run this WHILE the Go proxy is up (`sudo go run ./proxy/cmd/proxy`) and a
Dofus session is active. It connects to 127.0.0.1:9999, prints every event,
and emits a short state summary on each change.

    python3 test_proxy_eyes.py

Ctrl-C to stop.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from proxy_client import ProxyState


def main():
    state = ProxyState("127.0.0.1:9999")

    def on_event(ev):
        t = ev.get("type", "?")
        if t == "state":
            mobs = ev.get("mobs") or []
            mob_str = ", ".join(f"cell={m['cell']} id={m['group_id']} mobs={m.get('members')}" for m in mobs[:5])
            print(f"  STATE map={ev.get('map_id')} my_cell={ev.get('my_cell')} "
                  f"in_fight={ev.get('in_fight')} mob_groups={len(mobs)} "
                  f"players={len(ev.get('players') or [])} | {mob_str}")
        elif t == "fight_start":
            print("  FIGHT START")
        elif t == "fight_end":
            print("  FIGHT END")
        else:
            print(f"  ? {ev}")

    state.on_event(on_event)
    state.start()
    print("listening for proxy events. Ctrl-C to stop.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("stopping.")
    finally:
        state.stop()


if __name__ == "__main__":
    main()
