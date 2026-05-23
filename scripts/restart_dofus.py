"""Restart a hung Dofus client end-to-end.

The Dofus Retro client (and its Ankama Launcher) occasionally freezes
inside the docker VNC desktop -- UI stops responding to mouse/key
input even when driven manually. The known fix is to kill both
processes, relaunch the launcher, click PLAY, and maximize the new
game window.

Two entry points:

  CLI -- manual invocation from a VNC terminal when you notice a
  freeze and the bot isn't running auto-recovery for some reason.

  restart_dofus_client(...) -- programmatic entry point used by
  fighter/orchestrator.py's ProgressWatchdog. The bot calls this
  itself when no progress is detected for `watchdog_idle_no_progress_sec`
  (idle) or `turn_wait_timeout_sec` (in-fight), then re-execs main.py
  with --auto-reuse to resume from a fresh process.

Pre-req: calibrate the three click positions once per screen:
    python3 calibrate_play_button.py <screen_name>          # launcher PLAY
    python3 calibrate_post_launcher_clicks.py <screen_name> # server + char

Usage (CLI):
    python3 scripts/restart_dofus.py
    python3 scripts/restart_dofus.py --screen docker_ubuntu
    python3 scripts/restart_dofus.py --no-character-click  # use when last seen
                                                           # mid-fight (server
                                                           # auto-enters)

Screen resolution: --screen > $FIGHTER_SCREEN > config.json[default_screen].

Caveat on --no-character-click: when Dofus reconnects mid-fight, the
server auto-selects the character after server selection. With the
default behavior, the script's character double-click then lands on
the game world (possibly walking the character). If you know the
character was mid-fight, pass --no-character-click. If you're unsure,
the misclick is usually only a wasted MP step the bot recovers from.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# scripts/restart_dofus.py -> ../config.json (repo root)
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
LAUNCHER_APPIMAGE = "/opt/dofus/Retro-Setup-x86_64.AppImage"
LAUNCHER_WINDOW_NAME = "Ankama Launcher"
GAME_WINDOW_NAME = "Dofus Retro"

LAUNCHER_WAIT_SEC = 60.0
GAME_WAIT_SEC = 90.0
POST_KILL_SETTLE_SEC = 2.0
POST_MAXIMIZE_SETTLE_SEC = 0.5
# Time for the freshly-relaunched Ankama Launcher to load its UI
# content (hits Ankama's auth/news endpoints before the PLAY button
# becomes interactable). Clicking earlier than this lands on a
# not-yet-rendered launcher and is silently absorbed.
LAUNCHER_CONTENT_LOAD_SEC = 3.0
# After raising/activating any window, give the X server a beat to
# settle focus before clicking.
POST_FOCUS_SETTLE_SEC = 0.5
POST_CLICK_SETTLE_SEC = 1.0
# Time for the in-game "Choose a server" screen to render after the
# Dofus Retro window appears.
POST_GAME_WINDOW_WAIT_SEC = 10.0
# Time after the server double-click before either the "Choose your
# character" screen or the in-world map is up.
POST_SERVER_CLICK_WAIT_SEC = 5.0
# Time for the world to finish loading after the character double-click.
POST_CHARACTER_CLICK_WAIT_SEC = 8.0


def log(msg):
    print(f"[restart-dofus] {msg}", flush=True)


def resolve_screen(cfg, override):
    if override:
        return override
    env = os.environ.get("FIGHTER_SCREEN")
    if env:
        return env
    default = cfg.get("default_screen")
    if default:
        return default
    log("ERROR: no screen specified -- pass --screen, set "
        "$FIGHTER_SCREEN, or set config.json[default_screen].")
    sys.exit(2)


def load_clicks(cfg, screen):
    """Return (play, server, character) tuples. play_button is
    required (hard-error if missing); server_first and character_first
    are optional and returned as None if not calibrated (the script
    just skips those steps and warns)."""
    entry = (cfg.get("restart_clicks") or {}).get(screen)
    if not entry or "play_button" not in entry:
        log(f"ERROR: no restart_clicks[{screen!r}][play_button] in "
            f"{CONFIG_PATH.name}. Run "
            f"`python3 calibrate_play_button.py {screen}` first.")
        sys.exit(2)

    def _xy(key):
        if key not in entry:
            return None
        x, y = entry[key]
        return int(x), int(y)

    return _xy("play_button"), _xy("server_first"), _xy("character_first")


def xdotool_search(name):
    try:
        out = subprocess.check_output(
            ["xdotool", "search", "--name", name],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except subprocess.CalledProcessError:
        return None
    if not out:
        return None
    return out.split("\n")[0]


def wait_for_window(name, timeout_sec):
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        wid = xdotool_search(name)
        if wid:
            return wid
        time.sleep(1.0)
    return None


def kill_window_by_name(window_name, label):
    """xdotool search -> getwindowpid -> SIGKILL. Reliable even when
    the target is unresponsive (xdotool queries the X server, not the
    app). Returns True if a kill was issued."""
    wid = xdotool_search(window_name)
    if not wid:
        log(f"no {window_name!r} window -- skipping {label} kill")
        return False
    try:
        pid_out = subprocess.check_output(
            ["xdotool", "getwindowpid", wid],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except subprocess.CalledProcessError:
        log(f"could not get PID for {label} window {wid} -- skipping")
        return False
    try:
        pid = int(pid_out)
    except ValueError:
        log(f"got non-numeric PID {pid_out!r} for {label} window -- skipping")
        return False
    log(f"killing {label} window pid={pid} (SIGKILL)")
    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        log(f"pid {pid} already gone")
        return False


def cleanup_survivors():
    """Belt-and-suspenders sweep after the window-PID kills:
      - /tmp/.mount_Retro-* is the AppImage's FUSE mount; the
        launcher binary (named `zaap`, NOT `Retro-Setup...`) and its
        renderer/GPU/utility subprocesses all run from here.
      - dofus1electron catches any orphan game binaries.
      - Retro-Setup-x86_64.AppImage catches the shell wrapper if
        it's still alive (usually exec'd into already, but cheap).
    Critical: without killing the launcher process, Electron's
    single-instance lock makes any relaunch a no-op (just signals
    the existing instance via "second-instance" event)."""
    for pattern in ("/tmp/.mount_Retro",
                    "dofus1electron",
                    LAUNCHER_APPIMAGE):
        log(f"pkill -9 -f {pattern}")
        subprocess.run(
            ["pkill", "-9", "-f", pattern],
            check=False,
        )


def relaunch_launcher():
    if not Path(LAUNCHER_APPIMAGE).exists():
        log(f"ERROR: {LAUNCHER_APPIMAGE} not found")
        sys.exit(2)
    log(f"relaunching {LAUNCHER_APPIMAGE}")
    env = {**os.environ}
    env.setdefault("DISPLAY", ":1")
    subprocess.Popen(
        [LAUNCHER_APPIMAGE],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def maximize_window(wid, label):
    log(f"maximizing {label} window {wid}")
    subprocess.run(
        ["xdotool", "windowmove", wid, "0", "0"], check=False,
    )
    subprocess.run(
        ["xdotool", "windowsize", wid, "100%", "100%"], check=False,
    )
    time.sleep(POST_MAXIMIZE_SETTLE_SEC)


def click_in_window(wid, x, y, label, repeat=2, delay_ms=300):
    """Raise + activate the target window, then click `repeat` times
    at (x, y) with `delay_ms` between clicks. The click is sent
    WITHOUT --window so it propagates through the X server like a
    real mouse click (Electron renderers are nested X windows;
    --window <main_wid> clicks often don't reach the inner renderer
    and get silently dropped). Used for launcher PLAY (double @300ms),
    server card (double @300ms), and character card (quadruple @200ms
    -- single/double/triple all stop at "selected"; only ~4 fast
    clicks reliably enter the world)."""
    log(f"raising + activating {label} wid={wid}")
    subprocess.run(["xdotool", "windowraise", wid], check=False)
    subprocess.run(
        ["xdotool", "windowactivate", "--sync", wid], check=False,
    )
    time.sleep(POST_FOCUS_SETTLE_SEC)
    log(f"{repeat}x-clicking {label} at ({x},{y}) (delay={delay_ms}ms)")
    subprocess.run(
        ["xdotool", "mousemove", str(x), str(y)], check=True,
    )
    subprocess.run(
        ["xdotool", "click", "--repeat", str(repeat),
         "--delay", str(delay_ms), "1"],
        check=True,
    )
    time.sleep(POST_CLICK_SETTLE_SEC)


def restart_dofus_client(screen_name=None,
                         no_game_kill=False,
                         no_maximize_game=False,
                         no_character_click=False):
    """Kill + relaunch the Dofus client. Programmatic entry point.

    Args mirror the CLI flags so the in-bot caller and the manual user
    have identical knobs. `screen_name` follows the same precedence as
    the CLI: arg > $FIGHTER_SCREEN > config.json[default_screen].

    Returns True on success, False if a non-fatal step (window never
    appeared) didn't complete -- in both cases the caller should
    follow up with whatever recovery it has (e.g. re-execing main.py
    via --auto-reuse). Raises SystemExit on fatal misconfiguration
    (missing config, missing calibration, AppImage gone) so a manual
    CLI run still exits non-zero.
    """
    if not CONFIG_PATH.exists():
        log(f"ERROR: {CONFIG_PATH} not found")
        sys.exit(2)
    cfg = json.loads(CONFIG_PATH.read_text())
    screen = resolve_screen(cfg, screen_name)
    log(f"screen = {screen!r}")
    play_xy, server_xy, character_xy = load_clicks(cfg, screen)
    log(f"play_button = {play_xy}")
    log(f"server_first = {server_xy}")
    log(f"character_first = {character_xy}")

    if no_game_kill:
        log("--no-game-kill: skipping game-window kill")
    else:
        kill_window_by_name(GAME_WINDOW_NAME, "game")
    kill_window_by_name(LAUNCHER_WINDOW_NAME, "launcher")
    cleanup_survivors()

    log(f"sleeping {POST_KILL_SETTLE_SEC}s for X server to settle")
    time.sleep(POST_KILL_SETTLE_SEC)

    relaunch_launcher()

    log(f"waiting up to {LAUNCHER_WAIT_SEC:.0f}s for {LAUNCHER_WINDOW_NAME!r}")
    launcher_wid = wait_for_window(LAUNCHER_WINDOW_NAME, LAUNCHER_WAIT_SEC)
    if not launcher_wid:
        log(f"ERROR: {LAUNCHER_WINDOW_NAME!r} did not appear within "
            f"{LAUNCHER_WAIT_SEC:.0f}s")
        return False
    log(f"launcher window = {launcher_wid}")

    maximize_window(launcher_wid, "launcher")
    log(f"sleeping {LAUNCHER_CONTENT_LOAD_SEC}s for launcher UI to load")
    time.sleep(LAUNCHER_CONTENT_LOAD_SEC)
    click_in_window(launcher_wid, *play_xy, label="launcher PLAY")

    log(f"waiting up to {GAME_WAIT_SEC:.0f}s for {GAME_WINDOW_NAME!r}")
    game_wid = wait_for_window(GAME_WINDOW_NAME, GAME_WAIT_SEC)
    if not game_wid:
        log(f"ERROR: {GAME_WINDOW_NAME!r} did not appear within "
            f"{GAME_WAIT_SEC:.0f}s")
        return False
    log(f"game window = {game_wid}")

    if no_maximize_game:
        log("--no-maximize-game: skipping game maximize")
    else:
        maximize_window(game_wid, "game")

    if server_xy is None:
        log("server_first not calibrated -- stopping here. Run "
            "`python3 calibrate_post_launcher_clicks.py <screen>` to "
            "enable server + character auto-clicks.")
    else:
        log(f"sleeping {POST_GAME_WINDOW_WAIT_SEC}s for 'Choose a "
            f"server' screen to render")
        time.sleep(POST_GAME_WINDOW_WAIT_SEC)
        click_in_window(game_wid, *server_xy, label="server card")

        if no_character_click:
            log("--no-character-click: skipping character double-click")
        elif character_xy is None:
            log("character_first not calibrated -- stopping after "
                "server click.")
        else:
            log(f"sleeping {POST_SERVER_CLICK_WAIT_SEC}s for character "
                f"screen (or in-world auto-enter)")
            time.sleep(POST_SERVER_CLICK_WAIT_SEC)
            click_in_window(game_wid, *character_xy,
                            label="character card",
                            repeat=4, delay_ms=200)
            log(f"sleeping {POST_CHARACTER_CLICK_WAIT_SEC}s for world "
                f"to finish loading")
            time.sleep(POST_CHARACTER_CLICK_WAIT_SEC)

    log("restart complete")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--screen", help="screen name override")
    parser.add_argument("--no-game-kill", action="store_true",
                        help="skip the game-window kill step")
    parser.add_argument("--no-maximize-game", action="store_true",
                        help="skip the final game-window maximize")
    parser.add_argument("--no-character-click", action="store_true",
                        help="skip the in-game character double-click "
                             "(use when the character was mid-fight; "
                             "the server auto-selects in that case "
                             "and our extra click would land on the "
                             "map)")
    args = parser.parse_args()
    ok = restart_dofus_client(
        screen_name=args.screen,
        no_game_kill=args.no_game_kill,
        no_maximize_game=args.no_maximize_game,
        no_character_click=args.no_character_click,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
