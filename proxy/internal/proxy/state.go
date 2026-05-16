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

// stateTracker consumes server->client packets and emits structured events
// (state snapshots, fight transitions) to the eventHub.
type stateTracker struct {
	hub *eventHub

	mu             sync.Mutex
	mapID          int
	myID           int
	myCell         int
	inFight        bool
	mobs           map[int]MobGroup    // keyed by cell
	players        map[int]Player      // keyed by player id
	fightEntities  map[int]FightEntity // keyed by actor id (in-fight only)
}

// MobGroup is one aggressive monster pack sitting on a cell.
type MobGroup struct {
	Cell    int   `json:"cell"`
	GroupID int   `json:"group_id"`
	Members []int `json:"members"`
}

type Player struct {
	ID   int    `json:"id"`
	Name string `json:"name"`
	Cell int    `json:"cell"`
}

// FightEntity is one combatant in an active fight, derived from GTM packets.
// All numeric fields are 0 if unparseable. Alive=false means the entity
// collapsed to "<id>;1" in the GTM list (dead this turn).
type FightEntity struct {
	ID    int  `json:"id"`
	Cell  int  `json:"cell"`
	HP    int  `json:"hp"`
	AP    int  `json:"ap"`
	MP    int  `json:"mp"`
	HPMax int  `json:"hp_max"`
	Alive bool `json:"alive"`
}

func newStateTracker(hub *eventHub) *stateTracker {
	return &stateTracker{
		hub:           hub,
		mobs:          make(map[int]MobGroup),
		players:       make(map[int]Player),
		fightEntities: make(map[int]FightEntity),
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
	case pkt == "GS" || strings.HasPrefix(pkt, "GS|"):
		// Real fight-start packet is bare "GS" (or "GS|<args>" in some
		// flavors). Don't match "GSf", "GSU", etc.
		log.Printf("[state] fight_start trigger pkt=%q", pkt)
		s.setFight(true)
	case strings.HasPrefix(pkt, "GA;905;"):
		log.Printf("[state] fight_end trigger pkt=%q", pkt)
		s.setFight(false)
	case len(pkt) >= 3 && pkt[0] == 'G' && pkt[1] == 'E' && pkt[2] >= '0' && pkt[2] <= '9':
		// GE<xp>;<level>;<count>|... is the post-fight XP summary. It
		// always fires when a fight ends with rewards, so use it as a
		// backup trigger if GA;905; gets lost (some flavors of retro
		// frame it differently). setFight is idempotent.
		log.Printf("[state] fight_end trigger (GE xp summary) pkt=%q", pkt)
		s.setFight(false)
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
	case strings.HasPrefix(pkt, "GTM|"):
		// In-fight roster + state snapshot. Format:
		//   GTM|<entity>|<entity>|...
		// where each entity is "<id>;<status>;<hp>;<ap>;<mp>;<cell>;<?>;<hp_max>"
		// or just "<id>;1" if the entity died this turn.
		s.applyGTM(pkt[4:])
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
	} else if actorID < 0 {
		// Mob group movement. Find current cell by group_id and re-key.
		for cell, mob := range s.mobs {
			if mob.GroupID == actorID {
				if cell != dest {
					delete(s.mobs, cell)
					mob.Cell = dest
					s.mobs[dest] = mob
					log.Printf("[state] mob group=%d cell %d -> %d (path %q)", actorID, cell, dest, parts[1])
					changed = true
				}
				break
			}
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

func (s *stateTracker) setFight(active bool) {
	s.mu.Lock()
	changed := s.inFight != active
	s.inFight = active
	if !active {
		// Leaving a fight clears the in-fight roster.
		s.fightEntities = make(map[int]FightEntity)
	}
	s.mu.Unlock()
	if changed {
		ev := "fight_end"
		if active {
			ev = "fight_start"
		}
		s.hub.Publish(map[string]interface{}{
			"type": ev,
			"ts":   time.Now().UnixMilli(),
		})
		s.emitSnapshot()
	}
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
	s.fightEntities = entities
	s.mu.Unlock()
	s.emitSnapshot()
}

func atoiSafe(s string) int {
	n, _ := strconv.Atoi(s)
	return n
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
	if changed {
		s.mapID = id
		s.mobs = make(map[int]MobGroup)
		s.players = make(map[int]Player)
		s.myCell = 0
		// A map change always means we're outside any fight. Clear
		// stale in_fight so a missed GA;905; (e.g. fight ended via
		// teleport, or the proxy started mid-fight and only saw GS)
		// doesn't keep the bot wedged thinking it's still fighting.
		if s.inFight {
			log.Printf("[state] map change clearing stale in_fight=true")
			s.inFight = false
		}
		s.fightEntities = make(map[int]FightEntity)
	}
	s.mu.Unlock()
	if changed {
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
		"type":           "state",
		"ts":             time.Now().UnixMilli(),
		"map_id":         s.mapID,
		"my_id":          s.myID,
		"my_cell":        s.myCell,
		"in_fight":       s.inFight,
		"mobs":           mobs,
		"players":        players,
		"fight_entities": entities,
	}
	s.mu.Unlock()
	s.hub.Publish(snap)
}
