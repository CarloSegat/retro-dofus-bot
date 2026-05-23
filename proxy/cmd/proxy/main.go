// Entry point for the Dofus Retro MITM proxy.
//
// Bind both Ankama hostnames to loopback IPs via /etc/hosts, then run this
// with sudo (port 443). See proxy/README.md for the one-time Linux setup.
package main

import (
	"flag"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/carlosegat/simple-miner-dofus/proxy/internal/proxy"
)

// resolveInstance mirrors fighter/logging_setup.py:_resolve_instance so
// both halves of the bot land under logs/<instance>/. Convention: one
// container per Dofus character, so <instance> is a 1:1 stand-in for the
// character running there.
func resolveInstance(flagVal string) string {
	raw := flagVal
	if raw == "" {
		raw = os.Getenv("FIGHTER_INSTANCE")
	}
	if raw == "" {
		if h, err := os.Hostname(); err == nil {
			raw = h
		}
	}
	if raw == "" {
		raw = "host"
	}
	var b strings.Builder
	for _, r := range raw {
		switch {
		case (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') ||
			(r >= '0' && r <= '9') || r == '-' || r == '_' || r == '.':
			b.WriteRune(r)
		default:
			b.WriteRune('_')
		}
	}
	return b.String()
}

func main() {
	eventAddr := flag.String("events", "127.0.0.1:9999",
		"TCP address to publish JSON event stream on (empty = disabled)")
	logDir := flag.String("log-dir", "",
		"directory for rotating proxy.log files (empty = stderr only). "+
			"Default: <repo>/logs when launched from the repo root.")
	logInterval := flag.Duration("log-interval", 10*time.Minute,
		"rotate proxy.log after this much wall time")
	logBackups := flag.Int("log-backups", 144,
		"keep this many rotated proxy.log files (0 = unlimited)")
	instanceFlag := flag.String("instance", "",
		"per-bot tag for the log subdirectory. Falls back to "+
			"$FIGHTER_INSTANCE, then hostname. Used to keep multi-container "+
			"logs separate when they share a bind-mounted logs dir.")
	flag.Parse()

	log.SetFlags(log.LstdFlags | log.Lmicroseconds)

	instance := resolveInstance(*instanceFlag)

	// Default --log-dir to <repo>/logs when the binary is run from the
	// project root (which is what proxy/README.md and CLAUDE.md tell
	// users to do). Falls through to stderr-only when the dir can't
	// be created -- proxy must never refuse to start over logging.
	dir := *logDir
	if dir == "" {
		if cwd, err := os.Getwd(); err == nil {
			dir = filepath.Join(cwd, "logs")
		}
	}
	if dir != "" {
		instDir := filepath.Join(dir, instance)
		rw, err := proxy.NewRotatingWriter(instDir, "proxy.log",
			*logInterval, *logBackups, os.Stderr)
		if err != nil {
			log.Printf("[proxy] log rotation disabled: %v", err)
		} else {
			log.SetOutput(rw)
			log.Printf("[proxy] rotating log: %s/proxy.log "+
				"(every %s, keep %d, instance=%s)",
				instDir, *logInterval, *logBackups, instance)
		}
	}

	log.Println("[proxy] starting (needs sudo to bind 127.0.0.1:443)")
	log.Println("[proxy] required /etc/hosts entries:")
	log.Println("        127.0.0.1  dofusretro-co-production.ankama-games.com")
	log.Println("        127.0.0.2  dofusretro-ga-allisteria.ankama-games.com")

	p := &proxy.Proxy{EventAddr: *eventAddr}
	if err := p.Run(); err != nil {
		log.Fatalf("proxy: %v", err)
	}
}
