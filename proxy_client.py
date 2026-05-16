"""Client for the Go MITM proxy's JSON event stream.

The proxy (proxy/cmd/proxy) publishes one JSON object per line on a TCP
socket (default 127.0.0.1:9999). This module connects, parses the stream
in a background thread, and maintains a thread-safe `ProxyState` snapshot
the rest of the bot can read.

Usage:
    state = ProxyState("127.0.0.1:9999")
    state.start()
    snap = state.snapshot()
    if snap.in_fight:
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
    """Immutable snapshot of proxy state. Returned by ProxyState.snapshot()."""
    connected: bool = False
    map_id: int = 0
    my_id: int = 0
    my_cell: int = 0
    in_fight: bool = False
    mobs: dict = field(default_factory=dict)            # {cell: MobGroup}
    players: dict = field(default_factory=dict)         # {id: Player}
    fight_entities: dict = field(default_factory=dict)  # {id: FightEntity}
    last_event_ts: float = 0.0                          # local time.time()
    last_fight_start_ts: float = 0.0
    last_fight_end_ts: float = 0.0


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

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(
                connected=self._snap.connected,
                map_id=self._snap.map_id,
                my_id=self._snap.my_id,
                my_cell=self._snap.my_cell,
                in_fight=self._snap.in_fight,
                mobs=dict(self._snap.mobs),
                players=dict(self._snap.players),
                fight_entities=dict(self._snap.fight_entities),
                last_event_ts=self._snap.last_event_ts,
                last_fight_start_ts=self._snap.last_fight_start_ts,
                last_fight_end_ts=self._snap.last_fight_end_ts,
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
                self._snap.in_fight = ev.get("in_fight", False)
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
            elif t == "fight_start":
                self._snap.in_fight = True
                self._snap.last_fight_start_ts = now
            elif t == "fight_end":
                self._snap.in_fight = False
                self._snap.last_fight_end_ts = now
