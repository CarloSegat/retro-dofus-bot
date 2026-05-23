"""Auto-fighter entry point.

Constructs the Orchestrator (which prompts for runtime settings,
connects to the proxy, and wires the fighter classes) and runs it.

All bot logic lives under fighter/. The layers below it are:
  dofus/           game-aware verbs and protocol parsing
  mouse_keyboard/  input simulation (xdotool)
  vision/          OCR-based popup detection

Pass --screen <name> to pick which calibration in
config.json[cell_calibrations] to use (e.g. host_ubuntu vs
docker_ubuntu). Falls back to $FIGHTER_SCREEN then
config[default_screen].
"""
import argparse

from fighter.logging_setup import setup_logging
from fighter.orchestrator import Orchestrator


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--screen", default=None,
                        help="calibration key in config.json[cell_calibrations] "
                             "(e.g. host_ubuntu, docker_ubuntu)")
    parser.add_argument("--auto-reuse", action="store_true",
                        help="skip every interactive prompt and reuse the "
                             "settings saved by the last run "
                             "(~/.config/auto-fighter/last_run.json). Used by "
                             "the orchestrator's in-bot auto-restart path "
                             "after restart_dofus_client + os.execv -- a "
                             "fresh process must come up without anyone at "
                             "the terminal. Falls back to the normal "
                             "interactive flow + a warning if no saved run "
                             "exists.")
    args = parser.parse_args()
    setup_logging()
    Orchestrator(screen_name=args.screen, auto_reuse=args.auto_reuse).run()


if __name__ == "__main__":
    main()
