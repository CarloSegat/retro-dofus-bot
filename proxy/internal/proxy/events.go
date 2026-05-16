package proxy

import (
	"encoding/json"
	"log"
	"net"
	"sync"
)

// eventHub is a fan-out TCP server: every subscriber connection gets a copy
// of every Publish call as a single JSON line. Slow subscribers are dropped.
//
// The hub caches the most recent "state" message and replays it to every
// new subscriber so late-attaching consumers (e.g. the Python bot starting
// after Dofus is already in-world) immediately see the current MapState
// instead of waiting for the next change.
type eventHub struct {
	mu        sync.Mutex
	subs      map[net.Conn]chan []byte
	lastState []byte
}

func newEventHub() *eventHub {
	return &eventHub{subs: make(map[net.Conn]chan []byte)}
}

func (h *eventHub) Listen(addr string) error {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return err
	}
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				log.Printf("[events] accept: %v", err)
				return
			}
			h.addSub(c)
		}
	}()
	return nil
}

func (h *eventHub) addSub(c net.Conn) {
	ch := make(chan []byte, 256)
	h.mu.Lock()
	h.subs[c] = ch
	// Replay the most recent state snapshot so the new subscriber starts
	// from a known-good baseline instead of waiting for the next change.
	if h.lastState != nil {
		select {
		case ch <- h.lastState:
		default:
		}
	}
	h.mu.Unlock()
	log.Printf("[events] subscriber connected from %s", c.RemoteAddr())
	go func() {
		defer func() {
			h.mu.Lock()
			delete(h.subs, c)
			h.mu.Unlock()
			c.Close()
			log.Printf("[events] subscriber disconnected: %s", c.RemoteAddr())
		}()
		for msg := range ch {
			if _, err := c.Write(msg); err != nil {
				return
			}
		}
	}()
}

// Publish encodes v as JSON and fans it out to all subscribers.
// Subscribers whose buffers are full are dropped (this keeps the proxy
// from stalling on a slow Python consumer).
//
// "state" messages are cached so late-attaching subscribers can be
// brought up to the current MapState on connect.
func (h *eventHub) Publish(v interface{}) {
	if h == nil {
		return
	}
	data, err := json.Marshal(v)
	if err != nil {
		log.Printf("[events] marshal: %v", err)
		return
	}
	data = append(data, '\n')
	isState := false
	if m, ok := v.(map[string]interface{}); ok {
		if t, _ := m["type"].(string); t == "state" {
			isState = true
		}
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if isState {
		h.lastState = data
	}
	for c, ch := range h.subs {
		select {
		case ch <- data:
		default:
			log.Printf("[events] subscriber %s slow, dropping", c.RemoteAddr())
			close(ch)
			delete(h.subs, c)
			c.Close()
		}
	}
}
