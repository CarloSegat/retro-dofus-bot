#!/usr/bin/env bash
# Entrypoint for the desktop container:
#   1. write the VNC password from $VNC_PASSWORD
#   2. start TigerVNC on :1 with an XFCE session
#   3. bridge :1 to a noVNC websocket on :6080
#   4. tail logs in the foreground so `docker logs` shows them
set -euo pipefail

VNC_DISPLAY="${DISPLAY:-:1}"
VNC_NUM="${VNC_DISPLAY#:}"
VNC_PORT="$((5900 + VNC_NUM))"

mkdir -p "$HOME/.vnc"

# Docker creates named-volume mountpoints as root. The four Dofus/Ankama
# config volumes mount under ~/.config, which also leaves ~/.config itself
# root-owned -- XFCE then can't write its session state and crashes with
# "Unable to load a failsafe session". Reclaim ownership before VNC starts.
sudo chown -R bot:bot "$HOME/.config" || true

# Password file. VNC truncates to 8 chars silently; that is fine
# for a localhost-only bind, but worth knowing.
# Ubuntu 22.04's `tigervnc-common` does NOT ship a standalone passwd
# binary (tigervnc 1.12 dropped it). We use `tightvncpasswd` from the
# package of the same name -- the on-disk format is plain RFB
# obfuscated-password and tigervnc accepts it via -PasswordFile.
echo "${VNC_PASSWORD}" | tightvncpasswd -f > "$HOME/.vnc/passwd"
chmod 600 "$HOME/.vnc/passwd"

# Session script that VNC launches. dbus-launch is required so XFCE
# panels / notifications / file manager get a session bus.
cat > "$HOME/.vnc/xstartup" <<'EOF'
#!/bin/bash
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
export XDG_SESSION_TYPE=x11
export XDG_CURRENT_DESKTOP=XFCE
exec dbus-launch --exit-with-session startxfce4
EOF
chmod +x "$HOME/.vnc/xstartup"

# ---------------------------------------------------------------------------
# Auto-fighter MITM proxy.
#
# Runs BEFORE the VNC session comes up so the /etc/hosts hijack is already
# in place by the time the user opens Zaap inside the desktop. The proxy
# itself binds 127.0.0.1:443 + 127.0.0.2:443 (login/game) and publishes
# events on 127.0.0.1:9999 for the python bot.
#
# Failure here is non-fatal -- if /workspace isn't mounted or the build
# fails, we still bring up the desktop so the user can debug interactively.
# ---------------------------------------------------------------------------
PROXY_SRC=/workspace/proxy
PROXY_BIN=/home/bot/auto-fighter-proxy
# Per-instance log directory under the bind-mounted /workspace/logs.
# The proxy itself creates the subdir; we just need to point its
# --log-dir at /workspace/logs and pass --instance through. The Go
# proxy will produce /workspace/logs/<instance>/proxy.log; stderr is
# captured to a small bootstrap-only file so `docker logs` and post-
# mortems still see the early "starting" / "build failed" lines.
PROXY_BOOTSTRAP_LOG=/tmp/proxy.bootstrap.log

if [[ -d "${PROXY_SRC}" ]]; then
    echo "[start] building proxy from ${PROXY_SRC}"
    # -buildvcs=false: Go 1.18 stamps the binary with git info by default and
    # fails the build if `git` is not on PATH. We don't care about that stamp,
    # and adding git to the image just for this would bloat the layer.
    if ( cd "${PROXY_SRC}" && go build -buildvcs=false -o "${PROXY_BIN}" ./cmd/proxy ); then
        echo "[start] applying /etc/hosts hijack (sudo)"
        sudo bash "${PROXY_SRC}/setup-hosts.sh" || \
            echo "[start] WARN: setup-hosts.sh failed -- proxy may not be reached by Zaap"

        echo "[start] launching proxy on 127.0.0.1:443 + 127.0.0.2:443 (sudo, bg)"
        # `setsid` detaches from this script's process group so docker stop
        # signals don't take the proxy with the VNC bridge.
        # --log-dir /workspace/logs + --instance "$FIGHTER_INSTANCE" lands
        # the rotating proxy.log at /workspace/logs/<instance>/proxy.log,
        # next to fighter.log for the same instance. -E preserves the env
        # vars across sudo so the proxy can also read FIGHTER_INSTANCE
        # from the environment if --instance is empty.
        sudo -bE setsid "${PROXY_BIN}" \
            --events 127.0.0.1:9999 \
            --log-dir /workspace/logs \
            --instance "${FIGHTER_INSTANCE:-}" \
            > "${PROXY_BOOTSTRAP_LOG}" 2>&1 || \
            echo "[start] WARN: proxy launch failed -- see ${PROXY_BOOTSTRAP_LOG}"
    else
        echo "[start] WARN: proxy build failed -- skipping hosts hijack + proxy launch"
        echo "[start]       Zaap will talk to Ankama directly; bot won't see traffic."
    fi
else
    echo "[start] WARN: ${PROXY_SRC} not found -- is the repo bind-mounted at /workspace?"
fi

# Clean up any stale X locks from a previous unclean shutdown so
# `vncserver` does not refuse to start on the same display.
rm -f "/tmp/.X${VNC_NUM}-lock" "/tmp/.X11-unix/X${VNC_NUM}" || true

echo "[start] launching TigerVNC on ${VNC_DISPLAY} (${VNC_GEOMETRY}, depth ${VNC_DEPTH})"
tigervncserver "${VNC_DISPLAY}" \
    -geometry "${VNC_GEOMETRY}" \
    -depth "${VNC_DEPTH}" \
    -localhost no \
    -SecurityTypes VncAuth \
    -PasswordFile "$HOME/.vnc/passwd"

echo "[start] launching noVNC bridge on :6080 -> localhost:${VNC_PORT}"
websockify --web=/usr/share/novnc 6080 "localhost:${VNC_PORT}" &
NOVNC_PID=$!

# Surface VNC server logs through `docker logs`.
VNC_LOG="$(ls -1t "$HOME/.vnc/"*.log 2>/dev/null | head -n1 || true)"
if [[ -n "${VNC_LOG}" ]]; then
    tail -F "${VNC_LOG}" &
fi

# Clean shutdown on SIGTERM (docker stop).
trap 'echo "[start] shutting down"; vncserver -kill "${VNC_DISPLAY}" || true; kill ${NOVNC_PID} 2>/dev/null || true; exit 0' TERM INT

wait "${NOVNC_PID}"
