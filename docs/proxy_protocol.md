# Proxy protocol

What the Go MITM proxy parses out of the plaintext server→client stream
and exposes as JSON events on `127.0.0.1:9999`. Source of truth is
`proxy/internal/proxy/state.go`.

## Fight state machine

`fight_phase` is carried in every snapshot, with explicit
`fight_engage` / `fight_start` / `fight_end` event types at transitions.

| Phase | Meaning |
|-------|---------|
| `idle`      | Not in a fight. Mobs visible on the map, can engage. |
| `placement` | Just engaged. Placement screen up, 30s placement timer running. We can ready up. |
| `combat`    | Combat actually started — turns flowing (`GTM` roster + `GTS<actor>` turn-start), spells cast. |

Convenience properties on `Snapshot`: `in_combat`, `in_placement`,
`in_fight` (= `phase != "idle"`).

## Key packets

| Packet | Meaning | Effect |
|--------|---------|--------|
| `ASK\|<id>\|<name>\|...` | Character chosen | `my_id` |
| `GDM\|<mapId>\|...` | Map change | `map_id`; clears mobs/players/fight; force `phase=idle` |
| `GM\|+<cell>;...;<-id>;...;-3;<gfx^lvl,...>` | Mob group spawn | `mobs[cell]` (subkind `-3` only) |
| `GM\|+<cell>;...;<+id>;<name>;...` | Player spawn | `players[id]` |
| `GA0;1;<actor>;<path>` | Out-of-fight move | last 2 chars of path = dofus64-encoded dest cell |
| `GA;1;<actor>;<path>` | In-fight / mob move | same; updates `my_cell` or mob's cell |
| `GA;905;<myId>;` | **Fight engage** (placement starts) | `phase: idle → placement`, emits `fight_engage`. Other actors' `GA;905;` ignored. |
| `GS` or `GS\|...` | **Combat start** (placement timer expired / everyone ready) | `phase: * → combat`, emits `fight_start` |
| `GE<xp>;<level>;...` | **Fight end** (post-fight XP summary) | `phase: * → idle`, emits `fight_end` |
| `GTM\|<id>;<status>;<hp>;<ap>;<mp>;<cell>;;<hp_max>\|...` | In-fight roster | `fight_entities[id]` (collapsed form `<id>;1` = dead) |
| `GTS<actor>\|<dur_ms>\|<turn_n>` | Turn-start for `<actor>` | `turn_actor`/`turn_number`/`turn_started_at_ms`; emits `turn_start`. Main fighter waits for `actor==my_id`, then `turn_start_settle_sec` (1.5s) before acting. |

`GTF<actor>` / `GTR<actor>` (turn-finish/ready) are NOT parsed — we
pass-turn ourselves and don't care about mob turn boundaries.

## Placement-start burst

The wire sequence on engage is:

```
GA;905;<myId>;
  → GM|--<groupId>
  → GJK2|0|1|0|<placement_ms>|<n>
  → GP<teamA>|<teamB>|<flag>
  → GM|+<cell>;...;-1;973;-2;<gfx^lvl>;...   (in-fight mob spawn)
  → ILF<n>
  → GA;950;...                                (initial fight actions)
  → GM|+<cell>;...;<myId>;<myName>;...        (me placed)
```

The proxy only keys off the first packet (`GA;905;<myId>;`); the rest
is sequencing detail.

## Fallbacks

For when the proxy attaches mid-fight or misses a packet:

- `GTM|...` seen while phase != combat → promote to `combat`
  (`GTM` only fires inside an active fight).
- `GDM|<mapId>` map change while phase != idle → force back to `idle`.

## HP regen

Server emits a single `ILS<ms>` after each post-fight `As` anchor; that
value is the **seated** rate (1 HP / 1000 ms on Marx-Rockfeller).
Standing regen is `raw * 2`. Sit-state is inferred Python-side
(`ProxyState.sitting`) — server doesn't broadcast it. See
`Snapshot.effective_regen_ms`.

## Gotchas

- `GS` prefix matches multiple Dofus packets if too greedy. Match
  exactly `pkt == "GS"` or `strings.HasPrefix(pkt, "GS|")`.
- `GA;905;<actorId>;` is engage, **NOT** fight-end. Earlier versions of
  the proxy treated it as fight-end, which made `in_fight` only flip
  true ~30s after a click (when `GS` arrived). The 30s gap is the
  placement timer (`GJK2|...|30000|...`), not network latency.
- Stale `phase`: if the proxy connects mid-fight and misses
  `GA;905;` / `GS` / `GE`, a `GTM` in flight will still promote phase
  to `combat`, and the `GDM`-clears-phase fallback handles teleport-out.
