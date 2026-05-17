# Phase 1 — Dofus Retro in a container, manual play only

**Goal:** Build a Docker image that runs the native Linux Dofus Retro
client headless inside the container, exposed via VNC so the user can
connect from a VNC viewer on the host and **play the game manually**.

No proxy. No bot. No calibration. No multi-instance. Just: can I open a
VNC viewer, log in to Dofus, walk around, fight a mob with my own
keyboard and mouse?

This phase is purely about removing variables. If manual play works,
every later phase (proxy, bot, multi-instance) only has to add things
on top of a known-good base.

---

## Deliverables

- `docker/Dockerfile` — single image
- `docker/entrypoint.sh` — starts Xvfb, openbox, x11vnc, Dofus
- `docker/README.md` — one-shot build + run commands
- A working `docker run` that exposes VNC on `localhost:5900` and shows
  the Dofus login screen when connected

---

## Steps

### 1. Settle the Dofus packaging question
Decide before touching the Dockerfile:
- **AppImage**: self-contained, but needs FUSE in the container
  (`--cap-add SYS_ADMIN --device /dev/fuse`) OR `--appimage-extract`
  at build time so we run the unpacked tree.
- **`.deb`**: cleaner, no FUSE concerns, but tied to whatever apt deps
  Ankama ships.

Default: extract the AppImage at build time. No runtime caps required,
image stays portable.

Action: download the official Dofus Retro Linux artifact, note its
exact filename, and decide based on what's actually offered.

### 2. Pick a fixed display geometry
Hard-code Xvfb to `1280x800x24`. The `x24` (24-bit color) part is
mandatory — Xvfb defaults to 8-bit pseudocolor and the game will look
broken or fail to start. Phase 1 doesn't care about the exact
resolution, but later phases (calibration) will, so commit to one
number now and never change it.

### 3. Dockerfile skeleton
Base: `ubuntu:22.04`.

Install:
- GUI runtime libs: `libgtk-3-0`, `libnss3`, `libasound2`, `libgbm1`,
  `libxss1`, `libnotify4`, `libxtst6`, `libsecret-1-0`
- X stack: `xvfb`, `openbox`, `x11vnc`
- Utilities for debugging: `xdotool`, `wmctrl`, `procps`, `ca-certificates`

Copy:
- Extracted Dofus AppImage (or installed `.deb` contents) to `/opt/dofus`
- `entrypoint.sh` to `/entrypoint.sh`

Expose: `5900` (VNC).

`CMD ["/entrypoint.sh"]`.

### 4. entrypoint.sh
Sequential, with small sleeps where needed:

```sh
#!/bin/sh
set -e

# 1. virtual display
Xvfb :0 -screen 0 1280x800x24 &
export DISPLAY=:0
sleep 1

# 2. window manager (required so Dofus gets focus + decorations behave)
openbox &
sleep 1

# 3. VNC server (no password for phase 1 — bind to container loopback
#    only via docker port mapping)
x11vnc -display :0 -forever -shared -nopw -rfbport 5900 &
sleep 1

# 4. launch Dofus
exec /opt/dofus/Dofus --no-sandbox
```

The `--no-sandbox` flag is required for Electron-based clients running
as root inside Docker. Confirm Dofus Retro is Electron-based first; if
it's a native Qt/SDL binary, drop the flag.

### 5. Build + run
```sh
docker build -t auto-fighter-phase1 docker/
docker run --rm -it -p 5900:5900 --name dofus1 auto-fighter-phase1
```

Connect with any VNC viewer to `localhost:5900`. No password.

### 6. Verify
- Dofus login screen renders without graphical corruption
- Keyboard input from VNC reaches the login fields
- Mouse clicks register
- Can log in, pick a character, walk one map
- Can engage a mob and complete a fight manually

If all six work, Phase 1 is done.

---

## What can go wrong (Phase 1 only)

**1. AppImage refuses to run without FUSE.** Symptom: "AppImages
require FUSE to run." Fix: extract at build time with
`./Dofus.AppImage --appimage-extract` and point the entrypoint at
`squashfs-root/AppRun`.

**2. Dofus exits immediately with no error.** Almost always a missing
shared library. Run `ldd /opt/dofus/Dofus 2>&1 | grep "not found"` in
the container to see what's missing. Add to the apt install list.

**3. VNC connects but shows a black screen.** Either Xvfb didn't start
(check `pgrep Xvfb`), or Dofus crashed before drawing. Run the
entrypoint commands one at a time with `docker run -it ... bash` to
isolate.

**4. VNC connects, Dofus renders, but keyboard input does nothing.**
openbox isn't running, or Dofus didn't get focus. Test with
`xdotool getactivewindow getwindowname` inside the container. If
empty, openbox isn't there. If it shows something other than Dofus,
add a `wmctrl -a Dofus` to the entrypoint after launch.

**5. Mouse clicks register on wrong cells.** Not a Phase 1 concern (no
bot, user is clicking manually via VNC), but if the *cursor* appears
offset from where it clicks, the VNC viewer is doing its own scaling.
Disable scaling in the VNC client.

**6. Performance is unusable.** Software rendering (llvmpipe) for a 2D
game should be fine, but if it stutters badly check container CPU
limits and that the host isn't already loaded. Phase 1 doesn't need
GPU passthrough.

**7. `--no-sandbox` wrong for the binary type.** Some clients reject
the flag and refuse to start. Drop it and retry. Only Electron/Chromium
clients need it.

**8. Color looks wrong / "8-bit dithered" appearance.** Xvfb is at
8-bit. Recheck the `-screen 0 1280x800x24` argument — easy to typo as
`x8` or omit entirely.

**9. Audio errors spamming stderr.** Dofus tries to open PulseAudio,
fails, logs noise. Harmless for Phase 1. Suppress later with
`PULSE_SERVER=none` or by installing `libpulse0` + a dummy sink.

**10. Container has no `/dev/shm` or too small.** Some Electron clients
need `>=256M` of shared memory or they crash on startup. Fix:
`docker run --shm-size=512m ...`.

---

## Out of scope for Phase 1 (do NOT do these here)

- Installing Go, the proxy, Python, or any project code
- Running `setup-hosts.sh` or touching `/etc/hosts`
- Adding the `127.0.0.2` loopback alias
- Calibration tooling
- docker-compose, multi-instance, account isolation
- VNC password, TLS, or any hardening
- Persistent volumes for Dofus state (re-login per run is fine)

Each of those is a separate phase. Keep Phase 1 boring.
