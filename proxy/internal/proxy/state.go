package proxy

import (
	"log"
	"strconv"
	"strings"
	"sync"
	"time"
)

// dofus64 is the custom base64 alphabet Dofus Retro uses for cell ids in
// movement paths: a-z (0-25), A-Z (26-51), 0-9 (52-61), '-' (62), '_' (63).
const dofus64 = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"

// Iso-grid geometry mirrors cell_grid.py: 29 cells per sub-row pair, even
// sub-rows hold 14 cells (no offset), odd hold 15 (half-cell shift).
const (
	cellsPerPair = 29
	evenRowLen   = 14
)

// cellToUV converts a cell id to (u, v) iso-axis coordinates so we can
// compute Dofus "Po" distance. See cell_grid.py for the derivation.
func cellToUV(cell int) (int, int) {
	pair := cell / cellsPerPair
	rem := cell % cellsPerPair
	var subRow, pos int
	if rem < evenRowLen {
		subRow = 2 * pair
		pos = rem
	} else {
		subRow = 2*pair + 1
		pos = rem - evenRowLen
	}
	odd := subRow & 1
	u := (subRow + 2*pos - odd) / 2
	v := (subRow - 2*pos + odd) / 2
	return u, v
}

func absInt(x int) int {
	if x < 0 {
		return -x
	}
	return x
}

// cellDistance is the L1 distance in (u, v), matching cell_grid.cell_distance.
func cellDistance(a, b int) int {
	ua, va := cellToUV(a)
	ub, vb := cellToUV(b)
	return absInt(ua-ub) + absInt(va-vb)
}

func decodeDofus64Cell(s string) (int, bool) {
	if len(s) < 2 {
		return 0, false
	}
	hi := strings.IndexByte(dofus64, s[0])
	lo := strings.IndexByte(dofus64, s[1])
	if hi < 0 || lo < 0 {
		return 0, false
	}
	c := hi*64 + lo
	if c < 0 || c >= 560 {
		return 0, false
	}
	return c, true
}

// pathDestination returns the last cell in a Dofus movement path. The
// path is groups of 3 chars (direction + 2-char cell); destination is
// the cell of the final group, i.e. the trailing 2 chars.
func pathDestination(path string) (int, bool) {
	if len(path) < 2 {
		return 0, false
	}
	return decodeDofus64Cell(path[len(path)-2:])
}

// FightPhase is the tri-state machine reflecting the current fight context.
//
//	idle      -- not in a fight. Mobs visible on the map, can engage.
//	placement -- engage signal received (GA;905;<myId>;). Placement screen
//	             is up, the 30s placement timer is counting. Ready-up press
//	             advances to combat; otherwise the timer expires and combat
//	             starts automatically.
//	combat    -- bare GS packet seen. Turns flow (GTF/GTR/GTM), spells cast.
//
// Transitions and the signals that drive them:
//
//	idle      -> placement : GA;905;<myId>;  (the only signal worth trusting
//	                          for "I just engaged" -- other actors' GA;905;
//	                          packets are ignored).
//	placement -> combat    : bare GS (or "GS|...").
//	idle      -> combat    : GTM seen while not in combat. Fallback for the
//	                          case where the proxy attached mid-fight and
//	                          missed both GA;905 and GS.
//	combat    -> idle      : GE<xp>;... XP summary at end of fight.
//	*         -> idle      : GDM|<mapId> map change. Any teleport-out kills
//	                          a stale fight state.
type FightPhase string

const (
	PhaseIdle      FightPhase = "idle"
	PhasePlacement FightPhase = "placement"
	PhaseCombat    FightPhase = "combat"
)

// stateTracker consumes server->client packets and emits structured events
// (state snapshots, fight transitions) to the eventHub.
type stateTracker struct {
	hub *eventHub

	mu            sync.Mutex
	mapID         int
	myID          int
	myCell        int
	myLife          int   // out-of-fight current HP, anchor from server "As" packet
	myLifeMax       int   // out-of-fight max HP, from server "As" packet
	myLifeAnchorMs  int64 // unix ms when myLife was set (basis for regen estimate)
	myLifeRegenMs   int   // server-stated regen rate (ms per HP) from "ILS<n>" packet
	pods              int // current inventory weight (Ow packet field 0)
	podsMax           int // soft cap, no penalty below (Ow field 2)
	podsMaxOverweight int // hard cap, movement blocked at/above (Ow field 3)
	phase         FightPhase
	mobs          map[int]MobGroup    // keyed by cell
	players       map[int]Player      // keyed by player id
	fightEntities map[int]FightEntity // keyed by actor id (in-fight only)
	summons       map[int]int         // summonId -> summonerId (in-fight only)

	// Turn state, driven by GTS<actorId>|<dur_ms>|<turn_n> packets.
	// Reset on phase transitions back to idle.
	turnActor       int   // actorId whose turn just started (0 = none)
	turnNumber      int   // monotonically increasing turn counter within a fight
	turnStartedAtMs int64 // proxy-side wall clock (UnixMilli) at GTS receipt
	turnDurMs       int   // turn time allowance as quoted in the GTS packet
}

// MobGroup is one aggressive monster pack sitting on a cell.
//
// MoveEndsAtMs is the unix-ms deadline by which the client-side walk
// animation for the most recent GA0;1; / GA;1; movement should have
// finished. The wire packet announces the *destination* immediately, so
// our Cell field jumps ahead by ~steps*mobStepWalkMs of where the
// sprite actually is. Clicking the destination during that window
// registers as a walk, not an engage -- consumers should skip mobs
// whose MoveEndsAtMs is in the future.
type MobGroup struct {
	Cell          int   `json:"cell"`
	GroupID       int   `json:"group_id"`
	Members       []int `json:"members"`
	MoveEndsAtMs  int64 `json:"move_ends_at_ms"`
}

// Per-cell walk duration used to estimate when a mob's GA0;1; animation
// finishes. 400ms is a deliberate over-estimate: shorter than the run
// animation would let the bot click during the tail of the animation
// (where engage still fails), longer would just delay one tick.
const mobStepWalkMs = 400

type Player struct {
	ID   int    `json:"id"`
	Name string `json:"name"`
	Cell int    `json:"cell"`
}

// FightEntity is one combatant in an active fight, derived from GTM packets.
// All numeric fields are 0 if unparseable. Alive=false means the entity
// collapsed to "<id>;1" in the GTM list (dead this turn).
//
// IsSummon / SummonerID are stamped from the running summons map (built
// from GA;181 packets, see applySummonSpawn). GTM itself has no in-band
// summon flag -- only the GA;181 history distinguishes.
type FightEntity struct {
	ID         int  `json:"id"`
	Cell       int  `json:"cell"`
	HP         int  `json:"hp"`
	AP         int  `json:"ap"`
	MP         int  `json:"mp"`
	HPMax      int  `json:"hp_max"`
	Alive      bool `json:"alive"`
	IsSummon   bool `json:"is_summon"`
	SummonerID int  `json:"summoner_id"`
}

func newStateTracker(hub *eventHub) *stateTracker {
	return &stateTracker{
		hub:           hub,
		phase:         PhaseIdle,
		mobs:          make(map[int]MobGroup),
		players:       make(map[int]Player),
		fightEntities: make(map[int]FightEntity),
		summons:       make(map[int]int),
	}
}

// Apply consumes one server->client packet. Emits events when relevant
// state changes.
func (s *stateTracker) Apply(pkt string) {
	switch {
	case strings.HasPrefix(pkt, "GDM|"):
		s.applyGDM(pkt[4:])
	case strings.HasPrefix(pkt, "GM|"):
		s.applyGM(pkt[3:])
	case strings.HasPrefix(pkt, "GA;905;"):
		// GA;905;<actorId>; -- the actor has entered a fight challenge.
		// First packet of the placement-phase burst (immediately followed
		// on the wire by GJK2 placement timer, GP positions, ILF, and
		// the in-fight GM|+ spawns). For our own myID this is the
		// idle->placement transition; we ignore other actors' GA;905;.
		s.handleEngage(pkt[len("GA;905;"):])
	case pkt == "GS" || strings.HasPrefix(pkt, "GS|"):
		// Bare GS = placement timer expired (or everyone hit Ready),
		// combat actually begins. Don't match "GSf", "GSU", etc.
		s.setPhase(PhaseCombat, "GS combat-start")
	case len(pkt) >= 3 && pkt[0] == 'G' && pkt[1] == 'E' && pkt[2] >= '0' && pkt[2] <= '9':
		// GE<xp>;<level>;<count>|... post-fight XP summary. The
		// authoritative fight-end signal in this server flavor -- there
		// is no GA;<id>; for fight end.
		s.setPhase(PhaseIdle, "GE xp summary")
	case strings.HasPrefix(pkt, "ASK|"):
		// ASK|<id>|<name>|<level>|... is pushed right after the player
		// picks a character; it's our reliable source for myID.
		s.captureMyID(pkt[4:])
	case strings.HasPrefix(pkt, "GA0;1;"):
		// GA0;1;<actorId>;<encodedPath> -- outside-fight movement start.
		// The destination is encoded in the last 2 chars of the path
		// (dofus64). For intra-map walking the server doesn't push a
		// new GM|+ for the player, so this is the only signal we get.
		s.applyMovement(pkt[6:])
	case strings.HasPrefix(pkt, "GA;1;"):
		// Same format but seen for in-fight moves and mob walks. Mob
		// movement is ignored by applyMovement (only myID matters here).
		s.applyMovement(pkt[5:])
	case strings.HasPrefix(pkt, "GA;181;"):
		// GA;181;<casterId>;+<cell>;<kind>;<flag>;<summonedId>;<lvls>;<subkind>;<gfx^lvl>;...
		// Summon spawn during combat. The body after the casterId mirrors
		// a GM|+ entity payload (note the leading "+" on the cell). Subkind
		// is -1 for true summons (vs -2 for placement-burst in-fight mobs
		// and -3 for idle aggressive groups). We stash summonedId in
		// s.summons so the following GTM can stamp IsSummon/SummonerID on
		// the entity row -- GTM itself has no summon flag.
		s.applySummonSpawn(pkt[len("GA;181;"):])
	case strings.HasPrefix(pkt, "GA;4;"):
		// GA;4;<casterId>;<actorId>,<newCell> -- Transposition (Sacrieur
		// "Swap") position update. Fires twice per cast inside the
		// caster's GAS|...|GAF block, once per swapper (target first,
		// then caster). Teleport, not a path, so applyMovement's GA;1;
		// path decoder can't be reused.
		s.applySwapMove(pkt[5:])
	case strings.HasPrefix(pkt, "GTM|"):
		// In-fight roster + state snapshot. Format:
		//   GTM|<entity>|<entity>|...
		// where each entity is "<id>;<status>;<hp>;<ap>;<mp>;<cell>;<?>;<hp_max>"
		// or just "<id>;1" if the entity died this turn.
		s.applyGTM(pkt[4:])
	case strings.HasPrefix(pkt, "GTS"):
		// GTS<actorId>|<dur_ms>|<turn_n> -- turn-start for <actorId>.
		// Fires once per actor per round. GTM arrives just *before* GTS
		// at each turn boundary (carrying refreshed AP/MP), so by the
		// time we see GTS the snapshot is already current. When
		// <actorId> == myID the bot may begin acting.
		s.applyGTS(pkt[3:])
	case strings.HasPrefix(pkt, "As") && len(pkt) > 2 && pkt[2] >= '0' && pkt[2] <= '9':
		// Player stats packet. Format:
		//   As<xp>,<xpLow>,<xpNext>|<kamas>|<statsPts>|<spellPts>|<align>|<life,maxLife>|<energy,maxEnergy>|...
		// Server pushes one whenever a stat changes (post-fight, level
		// up, pickup). Field index 5 (0-based, pipe-split) is the
		// "<life>,<maxLife>" pair -- our out-of-fight HP source.
		s.applyAs(pkt[2:])
	case strings.HasPrefix(pkt, "Ow") && len(pkt) > 2 && pkt[2] >= '0' && pkt[2] <= '9':
		// Inventory weight packet. Format:
		//   Ow<current>|<?>|<soft_cap>|<hard_cap>
		// e.g. Ow1426|0|1043|3900 = 1426 current pods, 1043 normal max,
		// 3900 absolute max. Between soft_cap and hard_cap the character
		// walks slowly (overweight); at hard_cap movement is blocked.
		// Field 1 is consistently 0 in observed packets -- best guess is
		// trade/exchange weight; not used by the bot. Server pushes Ow
		// after every inventory delta (loot, drop, sell) so it fires in
		// bursts during fight wrap-up.
		s.applyOw(pkt[2:])
	case strings.HasPrefix(pkt, "ILS"):
		// Out-of-fight HP regen rate, in ms per +1 HP. Server fires it
		// once right after the post-fight "As" anchor; no further HP
		// packets arrive while we sit. Without parsing this we'd be
		// stuck forever at the anchor HP. Client/proxy must time-extrapolate.
		s.applyILS(pkt[3:])
	case len(pkt) >= 4 && strings.HasPrefix(pkt, "ECK") && pkt[3] >= '0' && pkt[3] <= '9':
		// ECK<kind>|<target_id> -- Exchange Create OK. Server confirms an
		// exchange window opened (kind 4 = player merchant shop, others
		// for NPC shops / trades / etc). The bot occasionally clicks a
		// player in merchant mode while engaging and gets stuck on the
		// shop; consumers listen for this event to dismiss it.
		// Digit-guard so we don't match hypothetical future "ECKE" etc.
		s.applyExchangeOpen(pkt[3:])
	}
}

// applyMovement consumes "<actorId>;<encodedPath>" from GA0;1;... or
// GA;1;... and updates the actor's cell. Handles myID (my character)
// and negative IDs (mob groups). Player-other movement is ignored.
func (s *stateTracker) applyMovement(body string) {
	parts := strings.SplitN(body, ";", 2)
	if len(parts) < 2 {
		return
	}
	actorID, err := strconv.Atoi(parts[0])
	if err != nil {
		return
	}
	dest, ok := pathDestination(parts[1])
	if !ok {
		return
	}
	s.mu.Lock()
	changed := false
	if actorID == s.myID && s.myID != 0 {
		if s.myCell != dest {
			log.Printf("[state] my_cell %d -> %d (path %q)", s.myCell, dest, parts[1])
			s.myCell = dest
			changed = true
		}
		// GTM only fires at turn boundaries, so fightEntities[myID].Cell
		// goes stale the moment we walk. Patch it here so consumers using
		// fight_entities (which is preferred over my_cell in-fight) see
		// the new position immediately.
		if s.phase == PhaseCombat {
			if me, ok := s.fightEntities[s.myID]; ok && me.Cell != dest {
				me.Cell = dest
				s.fightEntities[s.myID] = me
				changed = true
			}
		}
	} else if actorID < 0 {
		// Mob group movement. Find current cell by group_id and re-key,
		// and stamp the estimated animation-end time so consumers don't
		// click the destination cell before the sprite arrives.
		for cell, mob := range s.mobs {
			if mob.GroupID == actorID {
				steps := cellDistance(cell, dest)
				if steps < 1 {
					steps = 1
				}
				mob.MoveEndsAtMs = time.Now().UnixMilli() + int64(steps)*mobStepWalkMs
				if cell != dest {
					delete(s.mobs, cell)
					mob.Cell = dest
					s.mobs[dest] = mob
					log.Printf("[state] mob group=%d cell %d -> %d steps=%d (path %q)", actorID, cell, dest, steps, parts[1])
				} else {
					s.mobs[cell] = mob
				}
				changed = true
				break
			}
		}
	}
	s.mu.Unlock()
	if changed {
		s.emitSnapshot()
	}
}

// applySwapMove consumes "<casterId>;<actorId>,<newCell>" from a GA;4;
// (Transposition) packet and teleports actorID to newCell. Updates
// myCell + fightEntities[myID] when actorID==myID, and
// fightEntities[actorID] for mob targets (in-fight only -- swap is a
// fight-only spell). Two GA;4 fire per swap (target first, then
// caster), so this gets called twice with both sides of the swap.
func (s *stateTracker) applySwapMove(body string) {
	parts := strings.SplitN(body, ";", 2)
	if len(parts) < 2 {
		return
	}
	sub := strings.SplitN(parts[1], ",", 2)
	if len(sub) < 2 {
		return
	}
	actorID, err := strconv.Atoi(sub[0])
	if err != nil {
		return
	}
	newCell, err := strconv.Atoi(sub[1])
	if err != nil {
		return
	}
	s.mu.Lock()
	changed := false
	if actorID == s.myID && s.myID != 0 {
		if s.myCell != newCell {
			log.Printf("[state] my_cell %d -> %d (swap GA;4)", s.myCell, newCell)
			s.myCell = newCell
			changed = true
		}
		if s.phase == PhaseCombat {
			if me, ok := s.fightEntities[s.myID]; ok && me.Cell != newCell {
				me.Cell = newCell
				s.fightEntities[s.myID] = me
				changed = true
			}
		}
	} else if s.phase == PhaseCombat {
		if ent, ok := s.fightEntities[actorID]; ok && ent.Cell != newCell {
			log.Printf("[state] swap actor=%d cell %d -> %d", actorID, ent.Cell, newCell)
			ent.Cell = newCell
			s.fightEntities[actorID] = ent
			changed = true
		}
	}
	s.mu.Unlock()
	if changed {
		s.emitSnapshot()
	}
}

func (s *stateTracker) captureMyID(body string) {
	parts := strings.SplitN(body, "|", 3)
	if len(parts) < 1 {
		return
	}
	id, err := strconv.Atoi(parts[0])
	if err != nil {
		return
	}
	s.mu.Lock()
	changed := s.myID != id
	s.myID = id
	if changed && len(parts) >= 2 {
		// Demote ourselves from the players map if we'd been bucketed
		// there before we knew our id.
		delete(s.players, id)
	}
	s.mu.Unlock()
	if changed {
		s.emitSnapshot()
	}
}

// handleEngage processes the body of "GA;905;<actorId>;[<args>]". Only
// promotes us to placement when the actor is myID -- other players'
// engage packets are just chatter.
func (s *stateTracker) handleEngage(body string) {
	parts := strings.SplitN(body, ";", 2)
	if len(parts) < 1 {
		return
	}
	actorID, err := strconv.Atoi(parts[0])
	if err != nil {
		return
	}
	s.mu.Lock()
	isMe := s.myID != 0 && actorID == s.myID
	s.mu.Unlock()
	if !isMe {
		return
	}
	// Preemptively remove the engaged group from s.mobs. The server doesn't
	// reliably send GM|-<groupId> at engage time (or the packet boundary
	// can swallow it), and s.mobs has no other way to clear engaged groups
	// until the next GDM map change -- leaving "ghost" cells that the bot
	// will keep clicking. The engaged group is whichever mob group sits
	// closest (in Po distance) to s.myCell at engage time.
	s.removeClosestMobToMe("GA;905; engage")
	s.setPhase(PhasePlacement, "GA;905; engage")
}

// removeClosestMobToMe deletes the mob group nearest s.myCell, intended to
// be called at engagement to drop the group we just walked into. No-op if
// myCell is unknown or s.mobs is empty.
func (s *stateTracker) removeClosestMobToMe(reason string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.myCell == 0 || len(s.mobs) == 0 {
		return
	}
	bestCell := -1
	bestDist := 1<<31 - 1
	var bestID int
	for cell, mob := range s.mobs {
		d := cellDistance(s.myCell, cell)
		if d < bestDist {
			bestDist = d
			bestCell = cell
			bestID = mob.GroupID
		}
	}
	if bestCell < 0 {
		return
	}
	delete(s.mobs, bestCell)
	log.Printf("[state] removed engaged mob group=%d at cell=%d (po=%d, %s)",
		bestID, bestCell, bestDist, reason)
}

// setPhase transitions to the given fight phase, publishing one of three
// hub events on every real change:
//
//	idle      -> placement  ==> "fight_engage"
//	*         -> combat     ==> "fight_start"
//	*         -> idle       ==> "fight_end"
//
// Leaving a fight (any phase -> idle) clears the fight_entities roster.
func (s *stateTracker) setPhase(phase FightPhase, reason string) {
	s.mu.Lock()
	if s.phase == phase {
		s.mu.Unlock()
		return
	}
	prev := s.phase
	s.phase = phase
	if phase == PhaseIdle {
		s.fightEntities = make(map[int]FightEntity)
		s.summons = make(map[int]int)
		s.turnActor = 0
		s.turnNumber = 0
		s.turnStartedAtMs = 0
		s.turnDurMs = 0
	}
	s.mu.Unlock()
	log.Printf("[state] phase %s -> %s (%s)", prev, phase, reason)

	var ev string
	switch {
	case prev == PhaseIdle && phase == PhasePlacement:
		ev = "fight_engage"
	case phase == PhaseCombat:
		ev = "fight_start"
	case phase == PhaseIdle:
		ev = "fight_end"
	}
	if ev != "" {
		s.hub.Publish(map[string]interface{}{
			"type":  ev,
			"phase": string(phase),
			"ts":    time.Now().UnixMilli(),
		})
	}
	s.emitSnapshot()
}

// applyGTM parses one GTM packet body and replaces fightEntities with the
// fresh roster. Each entity is "<id>;<status>;<hp>;<ap>;<mp>;<cell>;<?>;<hp_max>".
// A 2-field entity "<id>;1" means dead this turn (status=1 collapsed form).
func (s *stateTracker) applyGTM(body string) {
	entities := map[int]FightEntity{}
	for _, raw := range strings.Split(body, "|") {
		if raw == "" {
			continue
		}
		f := strings.Split(raw, ";")
		id, err := strconv.Atoi(f[0])
		if err != nil {
			continue
		}
		e := FightEntity{ID: id, Alive: true}
		// Short form "<id>;1" = dead.
		if len(f) <= 2 {
			e.Alive = false
			entities[id] = e
			continue
		}
		e.HP = atoiSafe(f[2])
		e.AP = atoiSafe(f[3])
		e.MP = atoiSafe(f[4])
		e.Cell = atoiSafe(f[5])
		if len(f) >= 8 {
			e.HPMax = atoiSafe(f[7])
		}
		entities[id] = e
	}
	s.mu.Lock()
	for id, e := range entities {
		if summonerID, ok := s.summons[id]; ok {
			e.IsSummon = true
			e.SummonerID = summonerID
			entities[id] = e
		}
	}
	s.fightEntities = entities
	needPromote := s.phase != PhaseCombat
	s.mu.Unlock()
	if needPromote {
		// GTM only fires inside combat. Seeing one while idle/placement
		// means we missed GS (proxy attached mid-fight, or the packet
		// boundary swallowed it). Promote ourselves now.
		s.setPhase(PhaseCombat, "GTM seen outside combat")
		return
	}
	s.emitSnapshot()
}

// applySummonSpawn parses "<casterId>;+<cell>;<kind>;<flag>;<summonedId>;
// <lvls>;<subkind>;..." from a GA;181 body (no leading "GA;181;"). When
// subkind == -1 (true summon, distinct from -2 in-fight mob and -3 idle
// mob group) we remember (summonedId -> casterId) so the next GTM stamps
// IsSummon on the entity row. Wire example, mob -2 summoning -6:
//
//	GA;181;-2;+288;1;0;-6;111;-1;1072^95;2;-1;-1;-1;0,0,0,0;40;7;9;1;1;40
func (s *stateTracker) applySummonSpawn(body string) {
	fields := strings.Split(body, ";")
	if len(fields) < 7 {
		return
	}
	casterID, err := strconv.Atoi(fields[0])
	if err != nil {
		return
	}
	summonedID, err := strconv.Atoi(fields[4])
	if err != nil {
		return
	}
	if fields[6] != "-1" {
		return
	}
	s.mu.Lock()
	s.summons[summonedID] = casterID
	// If the entity row is already in fightEntities (rare -- GA;181 always
	// fires before the GTM in practice, but cheap to handle), stamp it now.
	if e, ok := s.fightEntities[summonedID]; ok {
		e.IsSummon = true
		e.SummonerID = casterID
		s.fightEntities[summonedID] = e
	}
	s.mu.Unlock()
	log.Printf("[state] summon: caster=%d summoned=%d", casterID, summonedID)
	s.emitSnapshot()
}

func atoiSafe(s string) int {
	n, _ := strconv.Atoi(s)
	return n
}

// applyAs parses the body of an "As..." stats packet and extracts the
// player's current and max life (field index 5, 0-based, split by '|').
// Only re-emits a snapshot when life actually changed -- the server
// fires As for unrelated changes (xp, kamas, etc.) and we don't want
// to spam JSON events.
func (s *stateTracker) applyAs(body string) {
	fields := strings.Split(body, "|")
	if len(fields) <= 5 {
		return
	}
	life := strings.SplitN(fields[5], ",", 2)
	if len(life) != 2 {
		return
	}
	cur, err1 := strconv.Atoi(life[0])
	max, err2 := strconv.Atoi(life[1])
	if err1 != nil || err2 != nil {
		return
	}
	nowMs := time.Now().UnixMilli()
	s.mu.Lock()
	changed := s.myLife != cur || s.myLifeMax != max
	s.myLife = cur
	s.myLifeMax = max
	// Stamp the anchor unconditionally: even if HP didn't change, the
	// server is asserting "this is your HP right now" so the regen
	// extrapolation must restart from this moment.
	s.myLifeAnchorMs = nowMs
	s.mu.Unlock()
	if changed {
		s.emitSnapshot()
	}
}

// applyOw parses the body of an "Ow<cur>|<?>|<soft>|<hard>" packet (no
// leading "Ow"). Only re-emits a snapshot when any value actually
// changed -- the server bursts several Ow packets in a row when loot
// is added one item at a time, and we don't want a state event per
// stack tick.
func (s *stateTracker) applyOw(body string) {
	fields := strings.Split(body, "|")
	if len(fields) < 4 {
		return
	}
	cur, err1 := strconv.Atoi(fields[0])
	soft, err2 := strconv.Atoi(fields[2])
	hard, err3 := strconv.Atoi(fields[3])
	if err1 != nil || err2 != nil || err3 != nil {
		return
	}
	s.mu.Lock()
	changed := s.pods != cur || s.podsMax != soft || s.podsMaxOverweight != hard
	s.pods = cur
	s.podsMax = soft
	s.podsMaxOverweight = hard
	s.mu.Unlock()
	if changed {
		s.emitSnapshot()
	}
}

// applyExchangeOpen parses "ECK<kind>|<target_id>" body (no leading "ECK")
// and publishes an "exchange_open" event. Pure notification -- no state
// change. The Python side schedules a delayed Esc to dismiss the window.
func (s *stateTracker) applyExchangeOpen(body string) {
	parts := strings.SplitN(body, "|", 2)
	if len(parts) < 1 || parts[0] == "" {
		return
	}
	kind, err := strconv.Atoi(parts[0])
	if err != nil {
		return
	}
	target := 0
	if len(parts) >= 2 {
		target = atoiSafe(parts[1])
	}
	log.Printf("[state] exchange_open kind=%d target=%d", kind, target)
	s.hub.Publish(map[string]interface{}{
		"type":   "exchange_open",
		"kind":   kind,
		"target": target,
		"ts":     time.Now().UnixMilli(),
	})
}

// applyILS consumes the body of an "ILS<ms>" packet -- the server-stated
// out-of-fight HP regen rate in milliseconds per +1 HP. The server is
// authoritative and sends the correct rate for the current state:
// ILS1000 on sit, ILS2000 on stand (Marx-Rockfeller). Python must NOT
// re-derive or scale this -- trust it verbatim.
//
// When the rate changes (sit<->stand) while we have an active anchor,
// rebase: compute current HP using the OLD rate over the elapsed window,
// then write that as the new anchor at now-ms with the new rate. Without
// rebasing, the next extrapolation would apply the new rate to the
// entire elapsed period since the anchor, skewing the estimate.
//
// Always emits a snapshot so the rate change propagates.
func (s *stateTracker) applyILS(body string) {
	ms, err := strconv.Atoi(strings.TrimSpace(body))
	if err != nil || ms <= 0 {
		return
	}
	nowMs := time.Now().UnixMilli()
	s.mu.Lock()
	if s.myLifeRegenMs > 0 && s.myLifeRegenMs != ms && s.myLifeAnchorMs > 0 && s.myLifeMax > 0 {
		elapsed := nowMs - s.myLifeAnchorMs
		if elapsed > 0 {
			gained := int(elapsed / int64(s.myLifeRegenMs))
			if gained > 0 {
				s.myLife += gained
				if s.myLife > s.myLifeMax {
					s.myLife = s.myLifeMax
				}
			}
		}
		s.myLifeAnchorMs = nowMs
	}
	s.myLifeRegenMs = ms
	s.mu.Unlock()
	s.emitSnapshot()
}

// applyGTS consumes the body of a "GTS<actorId>|<dur_ms>|<turn_n>" packet
// (no leading "GTS"). Updates turn state and emits a lightweight
// "turn_start" event with the receive-time wall clock so the bot can
// schedule actions a fixed delay after server send.
func (s *stateTracker) applyGTS(body string) {
	parts := strings.Split(body, "|")
	if len(parts) < 1 || parts[0] == "" {
		return
	}
	actor, err := strconv.Atoi(parts[0])
	if err != nil {
		return
	}
	dur := 0
	turn := 0
	if len(parts) >= 2 {
		dur = atoiSafe(parts[1])
	}
	if len(parts) >= 3 {
		turn = atoiSafe(parts[2])
	}
	nowMs := time.Now().UnixMilli()
	s.mu.Lock()
	myID := s.myID
	s.turnActor = actor
	s.turnNumber = turn
	s.turnDurMs = dur
	s.turnStartedAtMs = nowMs
	s.mu.Unlock()
	tag := "other"
	if actor == myID && myID != 0 {
		tag = "ME"
	}
	log.Printf("[state] turn_start actor=%d (%s) turn=%d dur_ms=%d", actor, tag, turn, dur)
	s.hub.Publish(map[string]interface{}{
		"type":   "turn_start",
		"actor":  actor,
		"turn":   turn,
		"dur_ms": dur,
		"ts":     nowMs,
	})
}

// GDM|<mapId>|<date>|<encodedCells>
func (s *stateTracker) applyGDM(body string) {
	parts := strings.SplitN(body, "|", 2)
	if len(parts) < 1 {
		return
	}
	id, err := strconv.Atoi(parts[0])
	if err != nil {
		return
	}
	s.mu.Lock()
	changed := s.mapID != id
	stalePhase := s.phase
	if changed {
		s.mapID = id
		s.mobs = make(map[int]MobGroup)
		s.players = make(map[int]Player)
		s.myCell = 0
		s.fightEntities = make(map[int]FightEntity)
		s.summons = make(map[int]int)
	}
	s.mu.Unlock()
	if changed {
		// A map change always means we're outside any fight. Force back
		// to idle so a missed GE (fight ended via teleport, or proxy
		// started mid-fight and never saw the XP summary) doesn't keep
		// the bot wedged. setPhase is a no-op when already idle.
		if stalePhase != PhaseIdle {
			s.setPhase(PhaseIdle, "GDM map change")
			return
		}
		s.emitSnapshot()
	}
}

// GM|<entry>|<entry>|... -- each entry is `+<spawn>` or `-<id>`.
func (s *stateTracker) applyGM(body string) {
	changed := false
	for _, entry := range strings.Split(body, "|") {
		if entry == "" {
			continue
		}
		switch entry[0] {
		case '+':
			if s.applyGMSpawn(entry[1:]) {
				changed = true
			}
		case '-':
			if s.applyGMRemove(entry[1:]) {
				changed = true
			}
		}
	}
	if changed {
		s.emitSnapshot()
	}
}

func (s *stateTracker) applyGMSpawn(entry string) bool {
	fields := strings.Split(entry, ";")
	if len(fields) < 4 {
		return false
	}
	cell, err := strconv.Atoi(fields[0])
	if err != nil {
		return false
	}
	id, err := strconv.Atoi(fields[3])
	if err != nil {
		return false
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	if id == s.myID {
		if s.myCell != cell {
			s.myCell = cell
			return true
		}
		return false
	}

	if id > 0 {
		name := ""
		if len(fields) >= 5 {
			name = fields[4]
		}
		s.players[id] = Player{ID: id, Name: name, Cell: cell}
		return true
	}

	// id < 0 -> mob group or NPC. Wire format:
	//   +<cell>;<kind>;<flag>;<id>;<lvls>;<subkind>;<gfx^lvl,...>;...
	// We only want subkind=-3 (aggressive mob group). Other negative
	// subkinds (-4 solo monster NPC, -10 NPC mount, ...) get skipped.
	if len(fields) < 7 || fields[5] != "-3" {
		return false
	}
	members := []int{}
	for _, m := range strings.Split(fields[6], ",") {
		gfx, _, _ := strings.Cut(m, "^") // strip "^<level>" suffix
		if n, err := strconv.Atoi(gfx); err == nil {
			members = append(members, n)
		}
	}
	s.mobs[cell] = MobGroup{Cell: cell, GroupID: id, Members: members}
	return true
}

func (s *stateTracker) applyGMRemove(body string) bool {
	id, err := strconv.Atoi(body)
	if err != nil {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.players[id]; ok {
		delete(s.players, id)
		return true
	}
	for cell, g := range s.mobs {
		if g.GroupID == id {
			delete(s.mobs, cell)
			return true
		}
	}
	return false
}

func (s *stateTracker) emitSnapshot() {
	s.mu.Lock()
	mobs := make([]MobGroup, 0, len(s.mobs))
	for _, m := range s.mobs {
		mobs = append(mobs, m)
	}
	players := make([]Player, 0, len(s.players))
	for _, p := range s.players {
		players = append(players, p)
	}
	entities := make([]FightEntity, 0, len(s.fightEntities))
	for _, e := range s.fightEntities {
		entities = append(entities, e)
	}
	snap := map[string]interface{}{
		"type":               "state",
		"ts":                 time.Now().UnixMilli(),
		"map_id":             s.mapID,
		"my_id":              s.myID,
		"my_cell":            s.myCell,
		"my_life":            s.myLife,
		"my_life_max":        s.myLifeMax,
		"my_life_anchor_ms":  s.myLifeAnchorMs,
		"my_life_regen_ms":   s.myLifeRegenMs,
		"pods":                s.pods,
		"pods_max":            s.podsMax,
		"pods_max_overweight": s.podsMaxOverweight,
		"fight_phase":        string(s.phase),
		"mobs":               mobs,
		"players":            players,
		"fight_entities":     entities,
		"turn_actor":         s.turnActor,
		"turn_number":        s.turnNumber,
		"turn_started_at_ms": s.turnStartedAtMs,
		"turn_dur_ms":        s.turnDurMs,
	}
	s.mu.Unlock()
	s.hub.Publish(snap)
}
