"""Client for the Go MITM proxy's JSON event stream.

The proxy (proxy/cmd/proxy) publishes one JSON object per line on a TCP
socket (default 127.0.0.1:9999). This module connects, parses the stream
in a background thread, and maintains a thread-safe `ProxyState` snapshot
the rest of the bot can read.

Fight state is a tri-state machine (`fight_phase`):
  "idle"      -- not in a fight (mobs visible on the map, can engage).
  "placement" -- engaged: placement screen up, 30s placement timer running.
  "combat"    -- combat actually started, turns flowing.

The transitions are driven by these S->C signals (matched by the proxy):
  idle      -> placement  GA;905;<myId>;     (we walked into a mob)
  placement -> combat     bare GS / GS|...   (placement timer expired)
  combat    -> idle       GE<xp>;...         (post-fight XP summary)
  *         -> idle       GDM|<mapId>        (teleport/map change fallback)

Convenience properties on Snapshot:
  in_combat    -- phase == "combat"
  in_placement -- phase == "placement"
  in_fight     -- phase != "idle"  (either placement or combat)

Usage:
    state = ProxyState("127.0.0.1:9999")
    state.start()
    snap = state.snapshot()
    if snap.in_combat:
        ...
"""
import json
import socket
import threading
import time
from dataclasses import dataclass, field


@dataclass
class MobGroup:
    cell: int
    group_id: int
    members: list


@dataclass
class Player:
    id: int
    name: str
    cell: int


@dataclass
class FightEntity:
    """One combatant in an active fight, derived from GTM packets."""
    id: int
    cell: int = 0
    hp: int = 0
    ap: int = 0
    mp: int = 0
    hp_max: int = 0
    alive: bool = True


@dataclass
class Snapshot:
    """Immutable snapshot of proxy state. Returned by ProxyState.snapshot().

    fight_phase is one of "idle", "placement", "combat" -- see module
    docstring for the protocol signals that drive each transition.

    Turn fields are driven by GTS<actorId>|<dur_ms>|<turn_n> packets. The
    proxy fires a `turn_start` event on every GTS; turn_actor/turn_number
    reflect the most recent one (i.e. whose turn it is *right now*).
    turn_started_local_ts is the wall clock when the bot received the
    event -- use it to schedule actions a fixed delay after server send."""
    connected: bool = False
    map_id: int = 0
    my_id: int = 0
    my_cell: int = 0
    my_life: int = 0           # out-of-fight HP anchor (from server "As" stats packet)
    my_life_max: int = 0       # out-of-fight max HP (from server "As" stats packet)
    my_life_anchor_ms: int = 0 # unix ms when my_life was last set (regen basis)
    my_life_regen_ms: int = 0  # server-stated regen rate from "ILS" packet
    sitting: bool = False      # true while /sit is active; halves effective regen
    fight_phase: str = "idle"
    mobs: dict = field(default_factory=dict)            # {cell: MobGroup}
    players: dict = field(default_factory=dict)         # {id: Player}
    fight_entities: dict = field(default_factory=dict)  # {id: FightEntity}
    last_event_ts: float = 0.0                          # local time.time()
    last_fight_engage_ts: float = 0.0                   # idle -> placement
    last_fight_start_ts: float = 0.0                    # * -> combat
    last_fight_end_ts: float = 0.0                      # * -> idle
    turn_actor: int = 0                                 # actorId currently playing
    turn_number: int = 0                                # monotonic within a fight
    turn_started_at_ms: int = 0                         # proxy-side ms epoch
    turn_dur_ms: int = 0                                # turn allowance from GTS
    turn_started_local_ts: float = 0.0                  # local time.time() at event

    def effective_regen_ms(self) -> int:
        """Regen rate after applying sit-state.

        Empirically (Marx-Rockfeller, Dofus retro):
          - seated:   1 HP / 1000 ms
          - standing: 1 HP / 2000 ms
        The server's ILS packet reports the **seated** baseline value,
        not standing. So while sitting we use the raw rate as-is; while
        standing we double it. Server doesn't tell us the sit state on
        the wire -- we infer it from the fact that the bot itself sent
        /sit (ProxyState.sitting)."""
        if self.my_life_regen_ms <= 0:
            return self.my_life_regen_ms
        if self.sitting:
            return self.my_life_regen_ms
        return self.my_life_regen_ms * 2

    def estimated_life(self) -> int:
        """Anchor HP + extrapolated regen since the anchor.

        Server emits `As` once at fight end (the anchor) and `ILS<ms>`
        once stating regen rate; no further HP packets arrive while we
        sit. Computed here as `min(my_life + elapsed_ms/regen_ms, my_life_max)`.
        Falls back to literal my_life when we have no rate or no anchor
        (e.g. brand-new proxy session that hasn't seen an As yet) -- the
        caller's "my_life_max > 0" guard still refuses to engage blind.

        Uses effective_regen_ms so a seated bot gets its 2x regen reflected
        in the estimate (server only quotes the standing rate)."""
        if self.my_life_max <= 0:
            return self.my_life
        regen = self.effective_regen_ms()
        if regen <= 0 or self.my_life_anchor_ms <= 0:
            return self.my_life
        now_ms = int(time.time() * 1000)
        elapsed = now_ms - self.my_life_anchor_ms
        if elapsed <= 0:
            return self.my_life
        gained = elapsed // regen
        return min(self.my_life + int(gained), self.my_life_max)

    @property
    def in_combat(self) -> bool:
        return self.fight_phase == "combat"

    @property
    def in_placement(self) -> bool:
        return self.fight_phase == "placement"

    @property
    def in_fight(self) -> bool:
        """True iff phase is placement or combat (i.e. not idle)."""
        return self.fight_phase != "idle"


class ProxyState:
    """Background TCP client that maintains a Snapshot of proxy events.

    Reconnects automatically if the proxy goes down. Thread-safe."""

    def __init__(self, addr="127.0.0.1:9999", reconnect_sec=2.0):
        host, port = addr.split(":")
        self._host = host
        self._port = int(port)
        self._reconnect_sec = reconnect_sec
        self._lock = threading.Lock()
        self._snap = Snapshot()
        self._thread = None
        self._stop = threading.Event()
        self._on_event_callbacks = []

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="proxy-eyes")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def set_sitting(self, sitting: bool):
        """Local-only sit-state flag. Set True right after the bot sends
        /sit; cleared automatically on fight_engage (entering combat
        forces the character to stand). Halves the effective HP regen
        rate inside Snapshot.estimated_life()."""
        with self._lock:
            self._snap.sitting = sitting

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(
                connected=self._snap.connected,
                map_id=self._snap.map_id,
                my_id=self._snap.my_id,
                my_cell=self._snap.my_cell,
                my_life=self._snap.my_life,
                my_life_max=self._snap.my_life_max,
                my_life_anchor_ms=self._snap.my_life_anchor_ms,
                my_life_regen_ms=self._snap.my_life_regen_ms,
                sitting=self._snap.sitting,
                fight_phase=self._snap.fight_phase,
                mobs=dict(self._snap.mobs),
                players=dict(self._snap.players),
                fight_entities=dict(self._snap.fight_entities),
                last_event_ts=self._snap.last_event_ts,
                last_fight_engage_ts=self._snap.last_fight_engage_ts,
                last_fight_start_ts=self._snap.last_fight_start_ts,
                last_fight_end_ts=self._snap.last_fight_end_ts,
                turn_actor=self._snap.turn_actor,
                turn_number=self._snap.turn_number,
                turn_started_at_ms=self._snap.turn_started_at_ms,
                turn_dur_ms=self._snap.turn_dur_ms,
                turn_started_local_ts=self._snap.turn_started_local_ts,
            )

    def on_event(self, cb):
        """Register cb(event_dict) called for every event from the proxy."""
        self._on_event_callbacks.append(cb)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._connect_and_read()
            except (ConnectionRefusedError, ConnectionResetError, OSError) as e:
                with self._lock:
                    self._snap.connected = False
                print(f"[proxy-eyes] connection lost: {e}; retrying in {self._reconnect_sec}s")
            if self._stop.is_set():
                break
            time.sleep(self._reconnect_sec)

    def _connect_and_read(self):
        with socket.create_connection((self._host, self._port), timeout=5) as s:
            # create_connection's timeout becomes the socket's read timeout
            # too; clear it so blocking reads wait indefinitely for events.
            s.settimeout(None)
            with self._lock:
                self._snap.connected = True
            print(f"[proxy-eyes] connected to {self._host}:{self._port}")
            f = s.makefile("r", encoding="utf-8")
            for line in f:
                if self._stop.is_set():
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._apply(ev)
                for cb in self._on_event_callbacks:
                    try:
                        cb(ev)
                    except Exception as e:
                        print(f"[proxy-eyes] callback error: {e}")

    def _apply(self, ev):
        t = ev.get("type")
        now = time.time()
        with self._lock:
            self._snap.last_event_ts = now
            if t == "state":
                self._snap.map_id = ev.get("map_id", 0)
                self._snap.my_id = ev.get("my_id", 0)
                self._snap.my_cell = ev.get("my_cell", 0)
                self._snap.my_life = ev.get("my_life", 0)
                self._snap.my_life_max = ev.get("my_life_max", 0)
                self._snap.my_life_anchor_ms = ev.get("my_life_anchor_ms", 0)
                self._snap.my_life_regen_ms = ev.get("my_life_regen_ms", 0)
                self._snap.fight_phase = ev.get("fight_phase", "idle")
                self._snap.mobs = {
                    m["cell"]: MobGroup(m["cell"], m["group_id"], m.get("members", []))
                    for m in ev.get("mobs") or []
                }
                self._snap.players = {
                    p["id"]: Player(p["id"], p.get("name", ""), p["cell"])
                    for p in ev.get("players") or []
                }
                self._snap.fight_entities = {
                    e["id"]: FightEntity(
                        id=e["id"],
                        cell=e.get("cell", 0),
                        hp=e.get("hp", 0),
                        ap=e.get("ap", 0),
                        mp=e.get("mp", 0),
                        hp_max=e.get("hp_max", 0),
                        alive=e.get("alive", True),
                    )
                    for e in ev.get("fight_entities") or []
                }
                self._snap.turn_actor = ev.get("turn_actor", 0)
                self._snap.turn_number = ev.get("turn_number", 0)
                self._snap.turn_started_at_ms = ev.get("turn_started_at_ms", 0)
                self._snap.turn_dur_ms = ev.get("turn_dur_ms", 0)
            elif t == "fight_engage":
                self._snap.fight_phase = ev.get("phase", "placement")
                self._snap.last_fight_engage_ts = now
                # Entering combat forces the character to stand up.
                self._snap.sitting = False
            elif t == "fight_start":
                self._snap.fight_phase = ev.get("phase", "combat")
                self._snap.last_fight_start_ts = now
            elif t == "fight_end":
                self._snap.fight_phase = ev.get("phase", "idle")
                self._snap.last_fight_end_ts = now
                self._snap.turn_actor = 0
                self._snap.turn_number = 0
                self._snap.turn_started_at_ms = 0
                self._snap.turn_dur_ms = 0
                self._snap.turn_started_local_ts = 0.0
            elif t == "turn_start":
                self._snap.turn_actor = ev.get("actor", 0)
                self._snap.turn_number = ev.get("turn", 0)
                self._snap.turn_started_at_ms = ev.get("ts", 0)
                self._snap.turn_dur_ms = ev.get("dur_ms", 0)
                self._snap.turn_started_local_ts = now
