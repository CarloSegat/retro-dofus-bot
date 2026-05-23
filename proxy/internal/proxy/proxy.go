// Package proxy is a MITM TCP proxy that sits between the real Dofus Retro
// client and the Ankama login + game servers, allowing us to read the
// plaintext server-to-client packet stream.
//
// Client-to-server traffic is encrypted by the BC anti-cheat after the early
// handshake; we forward those bytes verbatim and don't try to decrypt them.
// We only consume the S->C direction, which stays plaintext for the whole
// session.
//
// See proxy/README.md for the one-time Linux setup (/etc/hosts hijack +
// 127.0.0.2 loopback alias).
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
	// EventAddr is the TCP address to publish JSON event lines on. Empty
	// disables the publisher (proxy runs in observe-only logging mode).
	EventAddr string

	// OnPacket receives every packet seen on either stream. Set by Run().
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

	events *eventHub
	state  *stateTracker
}

type gameSession struct {
	clientToServer net.Conn // writing to this side reaches the real game server
}

// Direction: which way the packet was travelling.
type Direction int

const (
	C2S Direction = iota // client -> server
	S2C                  // server -> client
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

// Run starts the event publisher (if EventAddr is set) and both proxy
// listeners. Blocks until a listener dies.
func (p *Proxy) Run() error {
	if p.EventAddr != "" {
		p.events = newEventHub()
		if err := p.events.Listen(p.EventAddr); err != nil {
			return fmt.Errorf("event listener: %w", err)
		}
		log.Printf("[proxy] event publisher on %s", p.EventAddr)
	}
	p.state = newStateTracker(p.events)

	// Wire packet observer to the state tracker so map/fight events are
	// emitted to subscribers.
	p.OnPacket = func(dir Direction, ch Channel, pkt string) {
		if dir == S2C && ch == Game {
			p.state.Apply(pkt)
		}
	}

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
		log.Printf("[proxy] game client arrived but no AYK seen yet -- dropping")
		return
	}

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

	p.sessMu.Lock()
	p.gameSession = &gameSession{clientToServer: upstream}
	p.sessMu.Unlock()
	defer func() {
		p.sessMu.Lock()
		p.gameSession = nil
		p.sessMu.Unlock()
	}()

	// Tell Python subscribers the upstream link is live. When either
	// pipe goroutine returns the game session is over -- the Dofus
	// client crashed, was kicked, or closed cleanly. Publish that
	// loudly so the bot's log shows a clean "logged out at <time>"
	// marker instead of just trailing silence.
	p.events.Publish(map[string]interface{}{
		"type":   "client_connected",
		"remote": client.RemoteAddr().String(),
		"ts":     time.Now().UnixMilli(),
	})

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
	log.Printf("[proxy] game client disconnected: %s", client.RemoteAddr())
	p.events.Publish(map[string]interface{}{
		"type":   "client_disconnected",
		"remote": client.RemoteAddr().String(),
		"ts":     time.Now().UnixMilli(),
	})
}

// Inject sends one packet to the game upstream as if the client had sent it.
// Plaintext only -- after BC kicks in, the server expects encrypted packets,
// so injection is mostly useful for early-handshake testing.
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
// Bytes are forwarded VERBATIM -- we do not strip or re-add the terminator.
// Server->client frames use "<payload>\x00" while client->server frames use
// "<payload>\n\x00"; re-framing would inject an extra "\n" into S->C traffic
// and the Flash client rejects the mismatched packets.
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
			toWrite = raw
		} else {
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
// unchanged. We stash the host:port so the game proxy knows what to dial.
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

func trunc(s string) string {
	if len(s) > 220 {
		return s[:220] + "..."
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
// Bypasses /etc/hosts and the OS resolver so the proxy never loops itself.
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
		if a.Type == 1 {
			return a.Data, nil
		}
	}
	return "", fmt.Errorf("no A record for %s", host)
}
