"""Auto-fighter entry point.

Constructs the Orchestrator (which prompts for runtime settings,
connects to the proxy, and wires the fighter classes) and runs it.

All bot logic lives under fighter/. The layers below it are:
  dofus/           game-aware verbs and protocol parsing
  mouse_keyboard/  input simulation (xdotool)
  vision/          OCR-based popup detection
"""
from fighter.orchestrator import Orchestrator


def main():
    Orchestrator().run()


if __name__ == "__main__":
    main()
