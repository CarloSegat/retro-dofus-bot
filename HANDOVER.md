# Handover — retro-bot-dofus

Self-contained handover. Everything needed to rebuild the proxy is pasted in
this file: source code, go.mod, scripts, run instructions.

Status: **the proxy works end-to-end and gives you live server→client packets
(map, fight, mob, player state). Client→server packets are encrypted by the
SWF and we never broke that cipher.** Hand off to a UI-driven (click + key)
bot project, using this proxy as the eyes.

---

## TL;DR run

```bash
# one-time setup (sudo)
sudo ifconfig lo0 alias 127.0.0.2 up
echo '127.0.0.1  dofusretro-co-production.ankama-games.com  # retro-bot proxy' \
    | sudo tee -a /etc/hosts
echo '127.0.0.2  dofusretro-ga-allisteria.ankama-games.com  # retro-bot proxy' \
    | sudo tee -a /etc/hosts
sudo killall -HUP mDNSResponder

# build + run (needs sudo to bind 443)
sudo go run ./cmd/proxy

# launch Dofus Retro from the Ankama Launcher as normal.
# Watch the proxy stdout — every plaintext packet gets logged.
```

Cleanup:

```bash
sudo sed -i '' '/retro-bot proxy/d' /etc/hosts
sudo ifconfig lo0 -alias 127.0.0.2
sudo killall -HUP mDNSResponder
```

Two loopback IPs are needed because both upstreams use port 443 and we need
separate listeners. The client must see the **original** hostname in `AYK`;
do **not** rewrite it (the client's char list is filtered by serverId matching
the connection hostname).

Forwarding rule: bytes are passed **verbatim**. Server→client packets end in
`\x00`; client→server packets end in `\n\x00`. Re-framing breaks the Flash
client.

---

## Repo layout

```
cmd/proxy/main.go                # entry point
internal/proxy/proxy.go          # the MITM
internal/client/proto.go         # NUL-terminated TCP wire helper (Conn)
internal/client/mapinfo.go       # packet → MapState, prints snapshots
internal/client/game.go          # direct-Go game-server client (legacy)
internal/client/login.go         # direct-Go login client (legacy)
docs/combat-protocol.md          # fight protocol reference
scripts/sniff_login.sh           # tcpdump login server
scripts/sniff_game.sh            # tcpdump game server
config.yaml                      # login + character config
go.mod                           # module deps
```

---

## What we tried for client→server (the BC anti-cheat)

The wall we hit. All client→server packets after the early auth handshake are
wrapped in an AES envelope:

```
ù<iv_b64>==<ciphertext_b64>
```

(`ù` is one byte 0xF9 prefix. `==` is base64 padding of the 16-byte IV.)

We could **not** decrypt those without the key + cipher mode, and could not
inject our own packets without producing valid envelopes. Steps we walked:

1. **Direct-Go login client** (`cmd/login`) — reaches IN_WORLD but the server
   sends a `HT<envelope>` BC challenge ~30 s after spawn, kicks us with `TT32`
   when unanswered. Cannot drive gameplay without solving BC.

2. **JPEXS FFDec on `loader.swf`** — decompiled to ActionScript. Found a stub
   for `getHash`/`onHashCallback` that proxies the BC challenge to the
   Electron layer via `ExternalInterface.call("getHash", …)`. SWF itself has
   no inline crypto for BC, but the heavy obfuscation made deeper RE
   impractical in the time available.

3. **View8 on `main.jsc`** — main.jsc is V8 8.7 bytecode (~12 MB, compiled
   with `bytenode`). View8 ships disassemblers for V8 9.4 / 10.2 / 11.3 only.
   Building a v8.7 fork is multi-day. Stopped.

4. **Pivot to MITM proxy** — successful for **observation** of server →
   client. Confirmed live map + fight state visible. But cannot inject
   commands because we'd need to produce valid encrypted packets.

5. **Patched preloader.js + D1ElectronLauncher.html in the app bundle** to
   monkey-patch `crypto.createCipheriv` / `createDecipheriv` /
   `net.Socket.prototype.write` / `ipcRenderer` in both the Electron main
   process **and** the renderer. Hook fired (confirmed by 100+ IPC events
   captured to `/tmp/bc-dump.log`) but **zero `createCipheriv` calls during a
   full login**. Conclusion: BC encryption does **not** go through Node's
   `crypto` module in any JS context.

6. **`strings main.jsc`** revealed `_keySchedule`, `_invKeySchedule`,
   `_keystream`, `createEncryptor`, `createDecryptor` — distinctive
   **CryptoJS** internals. So BC hashing is done by CryptoJS inside main.jsc,
   pure-JS AES that bypasses Node crypto entirely.

7. **`lsof` on the running game** — the game TCP socket lives in the
   Electron **NetworkService utility process**
   (`Dofus Retro Helper --type=utility --utility-sub-type=network.mojom.NetworkService`),
   not in Main, not in the Renderer, not in the Flash plugin helper. That
   process just forwards bytes from Mojo IPC to the kernel; the encryption
   happens upstream in whichever process generates the plaintext.

8. **Per-packet encryption hypothesis** — the BC challenge response goes
   through main.jsc + CryptoJS. But the per-packet `ù<iv>==<ct>` encryption
   for gameplay traffic does **not** round-trip through Node JS at all (no
   createCipheriv, no `net.Socket.write` to the game IP from main or
   renderer). Most likely: the SWF does per-packet encryption in
   ActionScript with its own AES, and ships the encrypted bytes via
   Chromium's PPAPI socket → Mojo → NetworkService.

9. **Tested zero-key AES-128-CBC** on the first encrypted packet from a live
   capture (hypothesis: the trailing `aaaaaaaa…` field of the
   `ts<charId>|<name>|<…>` message is a Dofus-base64 (a=0) all-zeros key).
   Decrypted to garbage. So the real key is delivered some other way and we
   never isolated it.

### Files we touched on the live game install (all reverted)

- `/Applications/Ankama/Retro/Dofus Retro.app/Contents/Resources/app/preloader.js`
- `/Applications/Ankama/Retro/Dofus Retro.app/Contents/Resources/app/retroclient/D1ElectronLauncher.html`
- `/Applications/Ankama/Retro/Dofus Retro.app/Contents/Resources/app/retroclient/js/bc-hook.js` (created then removed)

### What would unblock BC RE if anyone returns to it

- Patch loader.swf with JPEXS to log AES key/IV/plaintext (re-sign and inject
  — the SWF is the most likely actual encryption site).
- Or build a V8 8.7 disassembler for View8.
- Or hook the Flash plugin's PPAPI socket calls with frida and stack-walk
  back into the SWF AS3 frames.
- Or build a fake server, issue known `ts` + BC challenge, capture many
  (plaintext, ciphertext) pairs from a known-key scenario, statistical break.

---

## Reading the live stream

`proxy.Proxy` exposes `OnPacket(direction, channel, payload)`. Hook it:

```go
p := &proxy.Proxy{}
p.OnPacket = func(dir proxy.Direction, ch proxy.Channel, pkt string) {
    if dir == proxy.S2C && ch == proxy.Game {
        // feed pkt into your MapState / fight state machine
    }
}
p.Run()
```

Useful S→C packets to consume (all plaintext in the stream):

| code | meaning | format |
|---|---|---|
| `GDM` | map definition | `GDM\|<mapId>\|<date>\|<encodedCells>` |
| `GM\|+…` | spawn | `+<cell>;<kind>;<flag>;<id>;<name|mobIds>;…` |
| `GM\|-<id>` | despawn | |
| `GJK` | fight start | `GJK<...>` |
| `GIC` | placement cell | `GIC\|<id>;<cell>;<team>` |
| `GTS<id>\|<ms>\|<turn>` | turn start | |
| `GA;300;<casterId>;<spellId>,<cell>,…` | cast | |
| `GA;100;<casterId>;<targetId>,<-dmg>,…` | damage | |
| `GA;103;<id>` | death | |
| `GE…` | fight end / xp summary | |
| `As…` | self stat update | |

`internal/client/mapinfo.go` (pasted below) already parses the
entity-tracking ones. The fight protocol is in `docs/combat-protocol.md`.

---

## Open follow-ups in the repo

- `internal/client/fight.go` — was a draft fight loop targeting the
  direct-Go path; deleted after the pivot. If you ever rebuild it, do it on
  top of `MapState` + a UI driver, not the direct socket.
- `cmd/login` — still works up to IN_WORLD but dies to BC after ~30 s.
  Useful for non-fight tasks (server selection, character list dumps).
  Don't trust it for anything that lasts longer than a map load.
- `XNICOX541` account has a stray level-1 `Young-Fox` character from a debug
  session. Delete from the char screen when convenient.
- `berlinthree` (segatcarlo98@gmail.com) is the testbed account.

---

# CODE DUMP

Everything below is the verbatim repo contents needed to run the proxy. Drop
into a fresh module, `go mod tidy`, and run.

## go.mod

```go
module github.com/carlosegat/retro-bot-dofus

go 1.21.3

require (
	github.com/alexedwards/argon2id v0.0.0-20230305115115-4b3c3280a736 // indirect
	github.com/asaskevich/govalidator v0.0.0-20230301143203-a9d515a09cc2 // indirect
	github.com/go-ozzo/ozzo-validation/v4 v4.3.0 // indirect
	github.com/happybydefault/logging v0.0.0-20210507180050-0f3842239c0e // indirect
	github.com/kralamoure/dofus v0.0.0-20220428011622-33766786c1b4 // indirect
	github.com/kralamoure/retro v0.0.0-20210524205513-a4b1f4842c56 // indirect
	github.com/kralamoure/retrologin v0.25.0 // indirect
	github.com/kralamoure/retroproto v0.0.0-20220514025851-4074f9025d30 // indirect
	github.com/kralamoure/retroproxy v1.5.1 // indirect
	github.com/kralamoure/retroutil v0.0.0-20210518132922-a957c67f4004 // indirect
	go.uber.org/atomic v1.10.0 // indirect
	go.uber.org/multierr v1.9.0 // indirect
	go.uber.org/zap v1.24.0 // indirect
	golang.org/x/crypto v0.7.0 // indirect
	golang.org/x/sys v0.6.0 // indirect
	golang.org/x/time v0.0.0-20211116232009-f0f3c7e86c11 // indirect
)
```

The proxy itself only depends on the standard library — the kralamoure
dependencies are pulled by the legacy direct-Go client. You can strip them
if you only want the proxy.

## config.yaml

```yaml
login: segatcarlo98@gmail.com
character: Marx-Rockfeller
```

## cmd/proxy/main.go

```go
package main

import (
	"log"

	"github.com/carlosegat/retro-bot-dofus/internal/proxy"
)

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.Println("[proxy] starting (needs sudo to bind 127.0.0.1:443)")
	log.Println("[proxy] required /etc/hosts entry:")
	log.Println("        127.0.0.1  dofusretro-co-production.ankama-games.com")
	log.Println("[proxy] then launch Dofus Retro normally.")
	p := &proxy.Proxy{}
	if err := p.Run(); err != nil {
		log.Fatalf("proxy: %v", err)
	}
}
```

## internal/proxy/proxy.go

```go
// Package proxy is a MITM TCP proxy that sits between the real Dofus Retro
// client and the Ankama login + game servers.
//
// Why a proxy: the post-1.29 client has a BasicsFileCheck (BC) anti-cheat
// step wrapped in AES-encrypted "HT<iv>==<ct>" envelopes. The hash logic
// lives in the Electron-side V8 bytecode (main.jsc) and is hard to RE.
// Instead of re-implementing BC, we let the real client perform it and we
// observe / inject around it.
//
// Wiring (one-time, requires sudo):
//
//  1. Make sure loopback alias 127.0.0.2 exists, then hijack both
//     hostnames to localhost addresses:
//
//         sudo ifconfig lo0 alias 127.0.0.2 up
//
//         echo '127.0.0.1  dofusretro-co-production.ankama-games.com' \
//             | sudo tee -a /etc/hosts
//         echo '127.0.0.2  dofusretro-ga-allisteria.ankama-games.com' \
//             | sudo tee -a /etc/hosts
//         sudo killall -HUP mDNSResponder
//
//     Two distinct loopback IPs are needed because both upstreams use port
//     443 and we want one TCP listener per service. The client sees the
//     ORIGINAL hostname in the AYK response so its server-id filter does
//     not hide our characters.
//
//  2. sudo go run ./cmd/proxy
//
//  3. Launch Dofus Retro normally.
//
// The proxy resolves real upstream IPs via Cloudflare DNS-over-HTTPS so
// /etc/hosts entries can't loop us back to ourselves.
package proxy

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

const (
	defaultLoginHost = "dofusretro-co-production.ankama-games.com"
	defaultLoginPort = "443"

	// Two loopback IPs so we can bind both upstreams on port 443.
	// /etc/hosts redirects each Ankama hostname to the matching IP.
	listenLogin = "127.0.0.1:443"
	listenGame  = "127.0.0.2:443"
)

// Proxy holds shared state between the login and game listeners.
type Proxy struct {
	// Hooks receive every packet seen on either stream. May be nil.
	OnPacket func(direction Direction, channel Channel, payload string)

	// Set after the login server's AYK response is parsed, used by the
	// game listener to know where to dial upstream.
	mu          sync.Mutex
	gameUpstrm  string // "<host>:<port>" of the real game server
	loginUpstrm string // resolved IP:port for upstream login

	// gameSessions tracks each connected client so injection can target
	// the right upstream.
	sessMu      sync.Mutex
	gameSession *gameSession
}

type gameSession struct {
	clientToServer net.Conn // writing to this side reaches the real game server
}

// Direction: which way the packet was travelling.
type Direction int

const (
	C2S Direction = iota // client → server
	S2C                  // server → client
)

func (d Direction) String() string {
	if d == C2S {
		return "C>S"
	}
	return "S>C"
}

// Channel: which TCP session the packet belongs to.
type Channel int

const (
	Login Channel = iota
	Game
)

func (c Channel) String() string {
	if c == Login {
		return "login"
	}
	return "game"
}

// Run starts both listeners and blocks until one of them dies.
func (p *Proxy) Run() error {
	// Pre-resolve the login upstream IP once so subsequent dials don't
	// touch the system resolver (which would honour our /etc/hosts entry).
	ip, err := resolveDOH(defaultLoginHost)
	if err != nil {
		return fmt.Errorf("resolve login upstream: %w", err)
	}
	p.loginUpstrm = net.JoinHostPort(ip, defaultLoginPort)
	log.Printf("[proxy] login upstream resolved: %s -> %s", defaultLoginHost, ip)

	errCh := make(chan error, 2)
	go func() { errCh <- p.serveLogin() }()
	go func() { errCh <- p.serveGame() }()
	return <-errCh
}

func (p *Proxy) serveLogin() error {
	ln, err := net.Listen("tcp", listenLogin)
	if err != nil {
		return fmt.Errorf("listen login %s: %w", listenLogin, err)
	}
	log.Printf("[proxy] login listener ready on %s", listenLogin)
	for {
		c, err := ln.Accept()
		if err != nil {
			return err
		}
		go p.handleLogin(c)
	}
}

func (p *Proxy) serveGame() error {
	ln, err := net.Listen("tcp", listenGame)
	if err != nil {
		return fmt.Errorf("listen game %s: %w", listenGame, err)
	}
	log.Printf("[proxy] game listener ready on %s", listenGame)
	for {
		c, err := ln.Accept()
		if err != nil {
			return err
		}
		go p.handleGame(c)
	}
}

func (p *Proxy) handleLogin(client net.Conn) {
	defer client.Close()
	log.Printf("[proxy] login client connected from %s", client.RemoteAddr())

	upstream, err := dialDirect(p.loginUpstrm)
	if err != nil {
		log.Printf("[proxy] dial login upstream: %v", err)
		return
	}
	defer upstream.Close()

	// AYK is no longer rewritten — we keep the original hostname so the
	// client's server-id filter accepts our characters. The S→C pipe
	// only inspects AYK to learn the game upstream target.
	done := make(chan struct{}, 2)
	go func() {
		p.pipe(upstream, client, C2S, Login, nil)
		done <- struct{}{}
	}()
	go func() {
		p.pipe(client, upstream, S2C, Login, p.captureAYK)
		done <- struct{}{}
	}()
	<-done
}

func (p *Proxy) handleGame(client net.Conn) {
	defer client.Close()
	log.Printf("[proxy] game client connected from %s", client.RemoteAddr())

	p.mu.Lock()
	target := p.gameUpstrm
	p.mu.Unlock()
	if target == "" {
		log.Printf("[proxy] game client arrived but no AYK seen yet — dropping")
		return
	}

	// Resolve host portion via DoH to bypass /etc/hosts (the game host
	// isn't hijacked today but be future-proof).
	host, port, _ := net.SplitHostPort(target)
	ip, err := resolveDOH(host)
	if err != nil {
		log.Printf("[proxy] resolve game upstream %s: %v", host, err)
		return
	}
	upstreamAddr := net.JoinHostPort(ip, port)
	log.Printf("[proxy] dialing game upstream %s (host=%s)", upstreamAddr, host)
	upstream, err := dialDirect(upstreamAddr)
	if err != nil {
		log.Printf("[proxy] dial game upstream: %v", err)
		return
	}
	defer upstream.Close()

	// Stash the upstream so external code can inject packets.
	p.sessMu.Lock()
	p.gameSession = &gameSession{clientToServer: upstream}
	p.sessMu.Unlock()
	defer func() {
		p.sessMu.Lock()
		p.gameSession = nil
		p.sessMu.Unlock()
	}()

	done := make(chan struct{}, 2)
	go func() {
		p.pipe(upstream, client, C2S, Game, nil)
		done <- struct{}{}
	}()
	go func() {
		p.pipe(client, upstream, S2C, Game, nil)
		done <- struct{}{}
	}()
	<-done
}

// Inject sends one packet to the game upstream as if the client had sent it.
// Returns an error if no game session is active.
//
// NOTE: After BC kicks in the server expects encrypted packets, so injecting
// plaintext mid-session will fail. Only useful for early-handshake testing.
func (p *Proxy) Inject(payload string) error {
	p.sessMu.Lock()
	s := p.gameSession
	p.sessMu.Unlock()
	if s == nil {
		return errors.New("no active game session")
	}
	frame := payload + "\n\x00"
	if _, err := s.clientToServer.Write([]byte(frame)); err != nil {
		return err
	}
	log.Printf("[proxy] [INJECT] %s", trunc(payload))
	return nil
}

// pipe reads NUL-terminated packets from src and writes them to dst.
//
// Bytes are forwarded VERBATIM — we do not strip or re-add the terminator.
// Server→client frames use "<payload>\x00" while client→server frames use
// "<payload>\n\x00"; re-framing would inject an extra "\n" into S→C traffic
// and the Flash client rejects the (mismatched) packets, silently falling
// through to first-time-character setup.
//
// If transform is non-nil, each packet's payload (without terminator) is
// passed through it. If the function returns the original unchanged we
// still forward the raw bytes; otherwise we emit "<new>\n\x00" or "<new>\x00"
// matching the direction.
func (p *Proxy) pipe(dst, src net.Conn, dir Direction, ch Channel, transform func(string) (string, bool)) {
	br := bufio.NewReader(src)
	for {
		raw, err := br.ReadBytes(0)
		if err != nil {
			if err != io.EOF {
				log.Printf("[proxy] %s/%s read: %v", ch, dir, err)
			}
			return
		}
		payload := strings.TrimRight(string(raw), "\n\x00")

		out := payload
		drop := false
		if transform != nil {
			out, drop = transform(payload)
		}

		if p.OnPacket != nil {
			p.OnPacket(dir, ch, out)
		}
		log.Printf("[%s/%s] %s", ch, dir, trunc(out))

		if drop {
			continue
		}

		var toWrite []byte
		if out == payload {
			// Unchanged — pass the original framing through unaltered.
			toWrite = raw
		} else {
			// Modified — match the original terminator style.
			suffix := "\x00"
			if dir == C2S {
				suffix = "\n\x00"
			}
			toWrite = []byte(out + suffix)
		}
		if _, err := dst.Write(toWrite); err != nil {
			log.Printf("[proxy] %s/%s write: %v", ch, dir, err)
			return
		}
	}
}

// captureAYK reads `AYK<host>:<port>;<ticket>` but lets it pass through
// unchanged. The hostname stays so the client believes it's connecting to
// the legitimate game server (which it is — via /etc/hosts -> 127.0.0.2).
// We stash the host:port so the game proxy knows what to dial.
func (p *Proxy) captureAYK(pkt string) (string, bool) {
	if !strings.HasPrefix(pkt, "AYK") {
		return pkt, false
	}
	rest := strings.TrimPrefix(pkt, "AYK")
	addr, _, ok := strings.Cut(rest, ";")
	if !ok {
		return pkt, false
	}
	p.mu.Lock()
	p.gameUpstrm = addr
	p.mu.Unlock()
	log.Printf("[proxy] captured game upstream from AYK: %s (unchanged on wire)", addr)
	return pkt, false
}

// trunc keeps logs readable.
func trunc(s string) string {
	if len(s) > 220 {
		return s[:220] + "…"
	}
	return s
}

// dialDirect is the same as net.Dial("tcp", addr) but with a short timeout.
// `addr` must be a literal "IP:port" pair so no DNS happens here.
func dialDirect(addr string) (net.Conn, error) {
	d := net.Dialer{Timeout: 10 * time.Second}
	return d.Dial("tcp", addr)
}

// resolveDOH performs an A-record lookup over Cloudflare's DNS-over-HTTPS API.
// This deliberately bypasses /etc/hosts and the OS resolver so our own
// hosts hijack can't loop the proxy back into itself.
func resolveDOH(host string) (string, error) {
	url := "https://cloudflare-dns.com/dns-query?name=" + host + "&type=A"
	req, _ := http.NewRequest("GET", url, nil)
	req.Header.Set("Accept", "application/dns-json")
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	var body struct {
		Answer []struct {
			Type int    `json:"type"`
			Data string `json:"data"`
		} `json:"Answer"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return "", err
	}
	for _, a := range body.Answer {
		if a.Type == 1 { // A record
			return a.Data, nil
		}
	}
	return "", fmt.Errorf("no A record for %s", host)
}
```

## internal/client/proto.go

Tiny TCP framing helper used by the legacy direct-Go client. Not strictly
needed for the proxy but kept here because `mapinfo.go` references its
types when used standalone. Drop it in `internal/client/`.

```go
// Package client implements TCP clients for the Dofus Retro login and game
// servers using the plaintext message-oriented framing documented by
// kralamoure/retroproto.
package client

import (
	"bufio"
	"fmt"
	"net"
	"strings"
	"time"
)

// Conn wraps a TCP conn with the protocol's null-terminated string framing.
type Conn struct {
	net.Conn
	rd *bufio.Reader
}

func Dial(addr string, timeout time.Duration) (*Conn, error) {
	c, err := net.DialTimeout("tcp", addr, timeout)
	if err != nil {
		return nil, err
	}
	if tcp, ok := c.(*net.TCPConn); ok {
		_ = tcp.SetLinger(0)
		_ = tcp.SetKeepAlive(true)
	}
	return &Conn{Conn: c, rd: bufio.NewReader(c)}, nil
}

// Recv reads one packet (everything up to the next NUL byte).
func (c *Conn) Recv() (string, error) {
	s, err := c.rd.ReadString('\x00')
	if err != nil {
		return "", err
	}
	return strings.TrimRight(s, "\n\x00"), nil
}

// Send writes a single packet. Trailing NUL is appended automatically.
func (c *Conn) Send(pkt string) error {
	_, err := fmt.Fprint(c.Conn, pkt, "\n\x00")
	return err
}
```

## internal/client/mapinfo.go

State machine that turns the plaintext server→client stream into a live
view of the map. Plug into `proxy.OnPacket`.

```go
package client

import (
	"context"
	"fmt"
	"log"
	"sort"
	"strconv"
	"strings"
	"time"
)

// MapState is everything we know about the current map.
//
// MapID comes from the server's GDM push (`GDM|<mapId>|<date>|<encodedCells>`).
// Real-world coordinates (x,y) are NOT sent on the wire — the official client
// looks them up from its embedded map database. We only expose MapID here;
// coord lookup is a separate concern.
type MapState struct {
	MapID    int
	Mobs     map[int]MobGroup // keyed by cell
	Players  map[int]Player   // keyed by player id
	NPCs     map[int]NPC      // keyed by npc id (negative)
	MyID     int
	MyCell   int
	MyMapPos string // raw "x,y" if we ever learn it from another msg
}

// MobGroup is one aggressive monster pack standing on a cell.
//
// GroupID is the negative id the server assigns to the leader sprite
// (e.g. -2, -3, ...). Members are the gfx ids of each mob in the pack
// (`491,492` style from the 5th field of the GM line).
type MobGroup struct {
	Cell    int
	GroupID int
	Members []int
}

type Player struct {
	ID    int
	Name  string
	Cell  int
	Level int
}

type NPC struct {
	ID   int
	Cell int
	Name string
}

func NewMapState(myID int) *MapState {
	return &MapState{
		Mobs:    make(map[int]MobGroup),
		Players: make(map[int]Player),
		NPCs:    make(map[int]NPC),
		MyID:    myID,
	}
}

// Summary returns a printable snapshot of the current map.
func (s *MapState) Summary() string {
	var sb strings.Builder
	fmt.Fprintf(&sb, "map=%d", s.MapID)
	if s.MyCell != 0 {
		fmt.Fprintf(&sb, " me@cell=%d", s.MyCell)
	}
	fmt.Fprintf(&sb, " mobs=%d players=%d npcs=%d", len(s.Mobs), len(s.Players), len(s.NPCs))
	if len(s.Mobs) > 0 {
		sb.WriteString("\n  mob groups:")
		cells := make([]int, 0, len(s.Mobs))
		for c := range s.Mobs {
			cells = append(cells, c)
		}
		sort.Ints(cells)
		for _, c := range cells {
			g := s.Mobs[c]
			fmt.Fprintf(&sb, "\n    cell=%d groupID=%d members=%v", g.Cell, g.GroupID, g.Members)
		}
	}
	if len(s.Players) > 0 {
		sb.WriteString("\n  players:")
		ids := make([]int, 0, len(s.Players))
		for id := range s.Players {
			ids = append(ids, id)
		}
		sort.Ints(ids)
		for _, id := range ids {
			p := s.Players[id]
			fmt.Fprintf(&sb, "\n    id=%d name=%s cell=%d", p.ID, p.Name, p.Cell)
		}
	}
	return sb.String()
}

// WatchMap consumes packets from conn, maintains a MapState, and prints
// snapshots on changes. It runs until ctx is done or the connection drops.
// Used by the legacy direct-Go client. For the proxy path call
// ApplyPacket directly from your OnPacket hook.
func WatchMap(ctx context.Context, conn *Conn, myID int) error {
	state := NewMapState(myID)

	recvCh := make(chan string, 64)
	errCh := make(chan error, 1)
	go func() {
		for {
			pkt, err := conn.Recv()
			if err != nil {
				errCh <- err
				return
			}
			recvCh <- pkt
		}
	}()

	ping := time.NewTicker(15 * time.Second)
	defer ping.Stop()

	var dirty bool
	flush := time.NewTicker(500 * time.Millisecond)
	defer flush.Stop()

	for {
		select {
		case <-ctx.Done():
			return nil
		case err := <-errCh:
			return err
		case pkt := <-recvCh:
			if ApplyPacket(state, pkt) {
				dirty = true
			}
		case <-flush.C:
			if dirty {
				log.Printf("[map]\n%s", state.Summary())
				dirty = false
			}
		case <-ping.C:
			if err := conn.Send("Bp"); err != nil {
				return err
			}
		}
	}
}

// ApplyPacket updates state from one server packet; returns true if anything
// observable changed. Unknown packets are silently ignored.
func ApplyPacket(s *MapState, pkt string) bool {
	switch {
	case strings.HasPrefix(pkt, "GDM|"):
		return applyGDM(s, pkt[4:])
	case strings.HasPrefix(pkt, "GM|"):
		return applyGM(s, pkt[3:])
	}
	return false
}

// GDM|<mapId>|<date>|<encodedCells>
func applyGDM(s *MapState, body string) bool {
	parts := strings.SplitN(body, "|", 2)
	if len(parts) < 1 {
		return false
	}
	id, err := strconv.Atoi(parts[0])
	if err != nil {
		return false
	}
	if s.MapID == id {
		return false
	}
	// New map: wipe entity state since they're rebuilt by subsequent GM pushes.
	s.MapID = id
	s.Mobs = make(map[int]MobGroup)
	s.Players = make(map[int]Player)
	s.NPCs = make(map[int]NPC)
	s.MyCell = 0
	return true
}

// GM|<entry>|<entry>|... — each entry is `+<spawn>` or `-<id>` (removal).
//
// Spawn entry layout (semicolon-separated):
//
//	0: cell
//	1: kind (1=character/mob, 3=other player, 7=mob group leader, ...)
//	2: flag
//	3: id (positive = player/character, negative = mob/NPC, our own playerId
//	      when this entry is the character we control)
//	4: payload — for players: name; for mobs: comma-separated gfx ids
//	5: subkind (-3=mob group, -4=NPC monster solo, -5=other player,
//	            -6=NPC mount, -145..-174=titled player, etc.)
//	6...: visuals
func applyGM(s *MapState, body string) bool {
	changed := false
	for _, entry := range strings.Split(body, "|") {
		if entry == "" {
			continue
		}
		switch entry[0] {
		case '+':
			if applyGMSpawn(s, entry[1:]) {
				changed = true
			}
		case '-':
			if applyGMRemove(s, entry[1:]) {
				changed = true
			}
		}
	}
	return changed
}

func applyGMSpawn(s *MapState, entry string) bool {
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

	if id == s.MyID {
		if s.MyCell != cell {
			s.MyCell = cell
			return true
		}
		return false
	}

	if id > 0 {
		// Other player.
		name := ""
		if len(fields) >= 5 {
			name = fields[4]
		}
		s.Players[id] = Player{ID: id, Name: name, Cell: cell}
		return true
	}

	// id < 0 → mob group or NPC.
	if id == -166 {
		// Royalty NPC (mounts / decorative). Skip from mobs list, file under NPCs.
		s.NPCs[id] = NPC{ID: id, Cell: cell, Name: "Royalty"}
		return true
	}

	// Mob group: 5th field is comma-separated mob gfx ids.
	members := []int{}
	if len(fields) >= 5 {
		for _, m := range strings.Split(fields[4], ",") {
			if n, err := strconv.Atoi(m); err == nil {
				members = append(members, n)
			}
		}
	}
	s.Mobs[cell] = MobGroup{Cell: cell, GroupID: id, Members: members}
	return true
}

// `-<id>` removes an entity by id. Mobs are indexed by cell so we sweep.
func applyGMRemove(s *MapState, body string) bool {
	id, err := strconv.Atoi(body)
	if err != nil {
		return false
	}
	if _, ok := s.Players[id]; ok {
		delete(s.Players, id)
		return true
	}
	if _, ok := s.NPCs[id]; ok {
		delete(s.NPCs, id)
		return true
	}
	for cell, g := range s.Mobs {
		if g.GroupID == id {
			delete(s.Mobs, cell)
			return true
		}
	}
	return false
}
```

## scripts/sniff_login.sh

```bash
#!/usr/bin/env bash
# Capture the plaintext TCP traffic between the official Dofus Retro client
# and the Ankama login server, then extract ASCII packets in send order.
set -euo pipefail

PCAP=${1:-/tmp/retro_login.pcap}

LOGIN_HOSTS=$(host dofusretro-co-production.ankama-games.com \
  | awk '/has address/ {print "host "$4}' \
  | paste -sd '|' - | sed 's/|/ or /g')

if [[ -z "$LOGIN_HOSTS" ]]; then
  echo "could not resolve login hosts" >&2; exit 1
fi

FILTER="($LOGIN_HOSTS) and port 443"
echo "capture filter: $FILTER"
echo "writing to:     $PCAP"
echo "1) leave this running"
echo "2) in another terminal, hit Play on Dofus Retro"
echo "3) wait until the character list appears, then Ctrl-C this script"

sudo tcpdump -i en0 -w "$PCAP" "$FILTER"
```

## scripts/sniff_game.sh

```bash
#!/usr/bin/env bash
# Capture plaintext TCP traffic to the game server (Allisteria).
set -euo pipefail
PCAP=${1:-/tmp/retro_game.pcap}
FILTER="host dofusretro-ga-allisteria.ankama-games.com and port 443"
echo "filter: $FILTER"
echo "writing: $PCAP"
echo "1) keep this running"
echo "2) launch Dofus Retro, select your account, log in"
echo "3) stay in-world 15 seconds, then Ctrl-C"
sudo tcpdump -i en0 -w "$PCAP" "$FILTER"
```

---

# docs/combat-protocol.md

Fight protocol reverse-engineered from live captures of `Sartogioielliscarper`
(Ecaflip). Spell IDs are Ecaflip-specific; recapture for other classes.

## Engage → placement → fight start

```
C → Gp<targetCellOfMobGroup>          # tap monster group
S → GIC|<myId>;<myCell>;<dir>         # initial placement (me alone)
C → GR1                                # I'm ready
S → GR1<myId>                          # placement-ready ack
S → GIC|<mob1>;<cell>;<dir>|<mob2>;<cell>;<dir>|<me>;<cell>;<dir>|...  # full roster
S → GS                                 # game start
S → GTL|<id>|<id>|<id>|...             # turn order list
S → Gd<mapId>;0;;0;0;<n>;<m>;<n>;<m>   # fight default state
S → GTM|<id>;<dead>;<life>;<team>;<?>;<cell>;;<maxLife>|...
S → GTS<turnHolderId>|29000|<turnNum>  # 29s turn timer, turn 1
```

Mob ids during fight are **negative** (`-1`, `-2`, ...). Player id stays
positive. `GTM` per-fighter: `id;dead;life;team;?;cell;;maxLife`.

## Per-turn flow

```
S → GTS<id>|29000|<n>     # turn n starts for <id>
... my actions if it's my id ...
C → Gt                    # end-of-turn
S → GTF<id>               # turn finished
S → As<full stats refresh>
S → GTR<id>               # next turn can begin
```

`Bp` heartbeat (15s) should be skipped while a fight is active; server sends
its own turn ticks.

## Casting

**Client send:** `GA300<spellId>;<targetCell>`

**Server broadcast on success:**
```
S → As<stats>                                     # full stats refresh
S → GAS<casterId>                                 # action start
S → GA;300;<casterId>;<spellId>,<casterCell>,<X>,<lvl>,<crit>,1,1
S → SC<spellId>;<cooldownTurns>                   # (only if spell has CD)
S → GIE<effectType>;<targetIds>;<p1>;<p2>;;;<a>;<spellId>;<casterId>;<crit>
... (one GIE per effect) ...
S → GA;100;<casterId>;<targetId>,<-hp>,<elem>     # per-target damage
S → GA;103;<deadId>;<deadId>                      # per kill
S → GA;102;<casterId>;<casterId>,-<paCost>        # PA consumed
S → GAF0|<casterId>                               # action finished
```

### Verified spells (Ecaflip, Sartogioielli lvl 20)

| Spell           | ID  | PA | CD (turns) | Targeting     |
|-----------------|-----|----|------------|---------------|
| Heads or Tails  | 102 | 3  | none       | self cell     |
| Perception      | 113 | 2  | 4          | self cell     |
| All or Nothing  | 119 | 5  | 4          | self cell AoE |

Cooldown packet: `SC<spellId>;<turnsRemaining>` — set after cast. Spell is
re-castable when packet stops being sent / cooldown counter reaches 0.

## Fight end

```
S → GA;905;<myId>;                                # fight terminated
S → GM|--<mobId>                                  # mob entity removed (one per dead mob)
S → GJK2|0|1|0|30000|4                            # fight-join cleanup state
S → GPd<spawnCellsTeam1>|<spawnCellsTeam2>|0      # spawn positions reset
S → GM|+<id>;... (mobs respawn list)
S → ILF0
S → GA;950;<winnerId>;<winnerId>,<rank>,0
S → GM|+<myId>;... (me restored on map)
S → GE<xp>;<level>;<count>|<winnerId>|0|<rewardsPerFighter>...
```

## Map scan (idle mob discovery)

While in-world (not fighting), mobs are pushed via `GM|...` lines. Filter
rule for "monster groups to attack":
- 4th field starts with `-` and is **not** `-166` (Royalty/NPC).
- 5th field contains the monster ids (`491,492` = two mobs).

The 1st field after `+` is the **cell** where they stand — the value to
send in `Gp<cell>` to engage. The proxy's `MapState` already does this
filtering.
