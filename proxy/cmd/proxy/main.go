// Entry point for the Dofus Retro MITM proxy.
//
// Bind both Ankama hostnames to loopback IPs via /etc/hosts, then run this
// with sudo (port 443). See proxy/README.md for the one-time Linux setup.
package main

import (
	"flag"
	"log"

	"github.com/carlosegat/simple-miner-dofus/proxy/internal/proxy"
)

func main() {
	eventAddr := flag.String("events", "127.0.0.1:9999",
		"TCP address to publish JSON event stream on (empty = disabled)")
	flag.Parse()

	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.Println("[proxy] starting (needs sudo to bind 127.0.0.1:443)")
	log.Println("[proxy] required /etc/hosts entries:")
	log.Println("        127.0.0.1  dofusretro-co-production.ankama-games.com")
	log.Println("        127.0.0.2  dofusretro-ga-allisteria.ankama-games.com")

	p := &proxy.Proxy{EventAddr: *eventAddr}
	if err := p.Run(); err != nil {
		log.Fatalf("proxy: %v", err)
	}
}
