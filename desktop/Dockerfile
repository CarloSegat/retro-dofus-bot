# Ubuntu 22.04 XFCE desktop accessible over VNC (5901) and noVNC (6080).
#
# Why this base: matches the host's Ubuntu 22.04 jammy so anything that
# works here (Wine, Dofus Retro client, xdotool clicks, the auto-fighter
# proxy + python scripts) will work the same way in the container.
#
# Build:  docker compose build desktop
# Run:    docker compose up -d desktop
# View:   browser -> http://localhost:6080/vnc.html
#         or VNC viewer -> localhost:5901  (password from $VNC_PASSWORD)

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
        xfce4 \
        xfce4-terminal \
        xfce4-goodies \
        tigervnc-standalone-server \
        tigervnc-common \
        tightvncpasswd \
        novnc \
        websockify \
        dbus-x11 \
        xdotool \
        xdg-utils \
        x11-utils \
        x11-xserver-utils \
        libfuse2 \
        fuse \
        # Electron runtime deps for the Zaap launcher (Dofus Retro Setup).
        # The slim ubuntu base doesn't ship the Chromium shared libs and
        # Zaap dlopens them at startup -- without these it dies with
        # "error while loading shared libraries: libnss3.so" and friends.
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libgbm1 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2 \
        libxshmfence1 \
        libgtk-3-0 \
        libsecret-1-0 \
        # Mesa software OpenGL (llvmpipe). The Dofus Retro Linux client
        # (dofus1electron) is an Electron app -- Chromium's compositor
        # asks for GL on startup. Without a real GPU we render entirely
        # on CPU via llvmpipe; that needs libgl1 + the DRI swrast driver.
        # Pair this with LIBGL_ALWAYS_SOFTWARE=1 (set below) so Mesa
        # doesn't waste startup time probing for hardware that isn't there.
        libgl1 \
        libgl1-mesa-dri \
        libglx-mesa0 \
        sudo \
        curl \
        wget \
        ca-certificates \
        gnupg \
        fonts-dejavu \
        fonts-liberation \
        locales \
        nano \
        less \
        net-tools \
        procps \
        git \
        # Python runtime + pip so the bot's requirements.txt can be
        # installed inside the container (psycopg, pyautogui, etc.).
        python3 \
        python3-pip \
        # Go toolchain to build the MITM proxy at container start.
        # Ubuntu 22.04 ships Go 1.18, which matches proxy/go.mod.
        golang-go \
        # `ip` from iproute2 -- used by proxy/setup-hosts.sh to add the
        # 127.0.0.2/8 loopback alias (best-effort: needs NET_ADMIN).
        iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Firefox from the official Mozilla apt repo. The default Ubuntu 22.04
# `firefox` package is a snap shim that does not work in a container,
# so we pin to packages.mozilla.org instead.
RUN install -d -m 0755 /etc/apt/keyrings && \
    wget -q https://packages.mozilla.org/apt/repo-signing-key.gpg \
         -O /etc/apt/keyrings/packages.mozilla.org.asc && \
    echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" \
         > /etc/apt/sources.list.d/mozilla.list && \
    printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' \
         > /etc/apt/preferences.d/mozilla && \
    apt-get update && apt-get install -y --no-install-recommends firefox && \
    rm -rf /var/lib/apt/lists/*

# Non-root user matching host uid/gid (1000:1000) so files on the
# bind-mounted /workspace stay owned by the host user.
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd -g ${USER_GID} bot && \
    useradd  -m -u ${USER_UID} -g ${USER_GID} -s /bin/bash bot && \
    echo "bot ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/bot

COPY --chown=bot:bot start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

# Bake the Dofus Retro installer (Zaap launcher) into the image.
# FUSE is wired up via /dev/fuse + SYS_ADMIN in docker-compose.yml,
# so the AppImage self-mounts and runs the same way it does on a
# regular desktop Ubuntu host -- no extract step needed.
COPY --chown=bot:bot Retro-Setup-x86_64.AppImage /opt/dofus/Retro-Setup-x86_64.AppImage
RUN chmod +x /opt/dofus/Retro-Setup-x86_64.AppImage

USER bot
WORKDIR /home/bot

ENV DISPLAY=:1 \
    VNC_GEOMETRY=1280x800 \
    VNC_DEPTH=24 \
    VNC_PASSWORD=secret \
    # Force Mesa's software rasterizer (llvmpipe). No GPU in the container,
    # and Chromium / dofus1electron will hang probing for one otherwise.
    LIBGL_ALWAYS_SOFTWARE=1

EXPOSE 5901 6080

CMD ["/usr/local/bin/start.sh"]
