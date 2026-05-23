#!/usr/bin/env bash
# debug_docker.sh -- diagnose the docker stack from the host.
#
# Runs a battery of read-only checks against the running containers:
# status, env, logs, connectivity, persistence, calibration. Each
# section prints PASS / WARN / FAIL so the failing layer is obvious
# at a glance.
#
# Run from the repo root (or anywhere -- it cd's to itself):
#     bash scripts/debug_docker.sh
#
# No side effects. Safe to run any time.
set -u

cd "$(dirname "$0")/.." || exit 2

DESKTOP=auto-fighter-desktop
DB=auto-fighter-db

# ----- output helpers -----
if [[ -t 1 ]]; then
    BOLD=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; GRN=$'\e[32m'
    YEL=$'\e[33m'; BLU=$'\e[34m'; NC=$'\e[0m'
else
    BOLD=""; DIM=""; RED=""; GRN=""; YEL=""; BLU=""; NC=""
fi

section() { printf "\n${BOLD}${BLU}== %s ==${NC}\n" "$*"; }
pass()    { printf "  ${GRN}PASS${NC}  %s\n" "$*"; }
warn()    { printf "  ${YEL}WARN${NC}  %s\n" "$*"; }
fail()    { printf "  ${RED}FAIL${NC}  %s\n" "$*"; }
info()    { printf "  ${DIM}info${NC}  %s\n" "$*"; }

# Run a command in the desktop container (as bot user). Returns its
# exit code; output goes to caller's stdout.
in_desktop() {
    docker compose exec -T desktop "$@"
}

# Like in_desktop but as root.
in_desktop_root() {
    docker compose exec -T -u 0 desktop "$@"
}

# ----- 1. container status -----
section "container status"
for c in "$DESKTOP" "$DB"; do
    if ! docker inspect "$c" >/dev/null 2>&1; then
        fail "$c does not exist (run: docker compose up -d)"
        continue
    fi
    status=$(docker inspect -f '{{.State.Status}}' "$c")
    restarts=$(docker inspect -f '{{.RestartCount}}' "$c")
    started=$(docker inspect -f '{{.State.StartedAt}}' "$c")
    if [[ "$status" == "running" ]]; then
        pass "$c: $status (restarts=$restarts, since $started)"
    else
        fail "$c: $status (restarts=$restarts) -- see logs section below"
    fi
done

# Bail early if desktop isn't running -- most subsequent checks need exec.
desktop_running=$(docker inspect -f '{{.State.Running}}' "$DESKTOP" 2>/dev/null || echo false)
if [[ "$desktop_running" != "true" ]]; then
    section "desktop logs (last 50 lines)"
    docker logs --tail 50 "$DESKTOP" 2>&1 | sed 's/^/  /'
    printf "\n${RED}desktop is not running -- skipping interactive checks${NC}\n"
    exit 1
fi

# ----- 2. environment in desktop -----
section "desktop environment"
for var in DISPLAY VNC_GEOMETRY MSS_BACKEND MAP_DB_URL FIGHTER_SCREEN; do
    val=$(in_desktop printenv "$var" 2>/dev/null)
    if [[ -n "$val" ]]; then
        info "$var=$val"
    else
        warn "$var is unset"
    fi
done

# ----- 3. /etc/hosts hijack -----
section "/etc/hosts hijack (for proxy MITM)"
hits=$(in_desktop grep -c 'miner-proxy' /etc/hosts 2>/dev/null || echo 0)
if [[ "$hits" -ge 2 ]]; then
    pass "$hits hijack line(s) present"
    in_desktop grep 'miner-proxy' /etc/hosts | sed 's/^/    /'
else
    fail "expected >=2 'miner-proxy' lines in /etc/hosts, found $hits"
    fail "proxy can't MITM -- check start.sh logs for 'setup-hosts.sh' errors"
fi
# 127.0.0.2 loopback alias (needed for the GA endpoint)
if in_desktop ip addr show lo 2>/dev/null | grep -q '127\.0\.0\.2'; then
    pass "127.0.0.2/8 loopback alias present"
else
    fail "127.0.0.2 missing on lo -- proxy can't bind the GA address"
fi

# ----- 4. proxy process + socket -----
section "auto-fighter proxy"
if in_desktop pgrep -f auto-fighter-proxy >/dev/null 2>&1; then
    pids=$(in_desktop pgrep -f auto-fighter-proxy | tr '\n' ',' | sed 's/,$//')
    pass "proxy process running (pid=$pids)"
else
    fail "proxy process NOT running -- check /tmp/proxy.log inside container"
fi
# Event socket on 127.0.0.1:9999
if in_desktop bash -c '</dev/tcp/127.0.0.1/9999' 2>/dev/null; then
    pass "127.0.0.1:9999 accepting connections (event stream)"
else
    fail "127.0.0.1:9999 closed -- bot can't subscribe to proxy events"
fi

# ----- 5. DB reachability from desktop -----
section "DB connectivity (desktop -> db)"
if in_desktop bash -c 'getent hosts db' >/dev/null 2>&1; then
    pass "'db' service hostname resolves on compose network"
else
    fail "'db' hostname doesn't resolve -- compose network broken?"
fi
if in_desktop bash -c '</dev/tcp/db/5432' 2>/dev/null; then
    pass "db:5432 reachable"
else
    fail "db:5432 unreachable from desktop"
fi
# Actual query
url=$(in_desktop printenv MAP_DB_URL 2>/dev/null)
if [[ -n "$url" ]]; then
    rows=$(in_desktop python3 -c "
import os, psycopg
try:
    with psycopg.connect(os.environ['MAP_DB_URL']) as c, c.cursor() as cur:
        cur.execute('select count(*) from maps')
        print(cur.fetchone()[0])
except Exception as e:
    print(f'ERR: {e}')
" 2>/dev/null)
    if [[ "$rows" =~ ^[0-9]+$ ]]; then
        pass "psycopg query OK -- maps table has $rows row(s)"
    else
        fail "psycopg query failed: $rows"
    fi
fi

# ----- 6. X display + mss -----
section "X display + screen capture"
display=$(in_desktop printenv DISPLAY)
if in_desktop xdpyinfo >/dev/null 2>&1; then
    geom=$(in_desktop bash -c "xdpyinfo | awk '/dimensions:/ {print \$2}'")
    pass "X server $display reachable (geometry $geom)"
else
    fail "xdpyinfo failed -- TigerVNC not up?"
fi
mss_out=$(in_desktop python3 -c "
import os, mss
backend = os.environ.get('MSS_BACKEND', 'default')
with mss.mss(backend=backend) as sct:
    img = sct.grab(sct.monitors[0])
    print(f'OK backend={backend} size={img.size}')
" 2>&1)
if [[ "$mss_out" == OK* ]]; then
    pass "mss screen capture works ($mss_out)"
else
    fail "mss capture failed: $(printf '%s' "$mss_out" | tr '\n' ' ' | cut -c1-160)"
fi

# ----- 7. persisted volumes -----
section "persisted volumes"
for vol in auto_fighter_pgdata dofus_retro_config ankama_launcher_config ankama_config zaap_config; do
    full="auto-fighter_${vol}"
    if docker volume inspect "$full" >/dev/null 2>&1; then
        mount=$(docker volume inspect -f '{{.Mountpoint}}' "$full")
        pass "$full present (host path: $mount)"
    else
        warn "$full not found -- will be created on next 'compose up'"
    fi
done
# Dofus config files persisted?
dofus_files=$(in_desktop bash -c "ls -1 ~/.config/'Dofus Retro' 2>/dev/null | wc -l" || echo 0)
if [[ "$dofus_files" -gt 0 ]]; then
    pass "~/.config/Dofus Retro has $dofus_files entry/ies (saved creds + UI prefs)"
else
    warn "~/.config/Dofus Retro empty -- launch Dofus + tick 'remember account'"
fi
# /workspace bind-mount sees the host repo?
if in_desktop test -f /workspace/main.py; then
    pass "/workspace bind-mount serving the repo (main.py visible)"
else
    fail "/workspace/main.py missing -- bind-mount broken"
fi

# ----- 8. calibration / FIGHTER_SCREEN sanity -----
section "calibration vs FIGHTER_SCREEN"
cal_check=$(in_desktop python3 -c "
import json, os
cfg = json.load(open('/workspace/config.json'))
cals = cfg.get('cell_calibrations') or {}
want = os.environ.get('FIGHTER_SCREEN') or cfg.get('default_screen')
print('known:', sorted(cals))
print('want:', want)
print('match:', want in cals)
" 2>&1)
echo "$cal_check" | sed 's/^/    /'
if echo "$cal_check" | grep -q 'match: True'; then
    pass "active screen has a calibration entry"
else
    warn "no calibration for active screen -- run: python3 recalibrate_screen.py \$FIGHTER_SCREEN"
fi

# ----- 9. recent logs -----
section "desktop logs (last 30 lines)"
docker logs --tail 30 "$DESKTOP" 2>&1 | sed 's/^/  /'

section "proxy log (/tmp/proxy.log, last 30 lines)"
if in_desktop test -f /tmp/proxy.log; then
    in_desktop tail -n 30 /tmp/proxy.log 2>&1 | sed 's/^/  /'
else
    warn "/tmp/proxy.log missing"
fi

section "db logs (last 15 lines)"
docker logs --tail 15 "$DB" 2>&1 | sed 's/^/  /'

printf "\n${BOLD}done.${NC} re-run after fixing each FAIL to confirm.\n"
