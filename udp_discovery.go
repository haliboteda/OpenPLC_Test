// UDP discovery cases. The bootloader answers a small set of keywords on the
// same port the TCP server listens on, and the whole ether upgrade flow starts
// with one of those replies -- so when discovery goes quiet the board looks
// absent even though it is running fine.
package main

import (
	"fmt"
	"net"
	"strings"
	"time"

	"IAPTool/iapproto"
)

// dialBoard opens a UDP socket to the board, pinned to the physical NIC the
// way IAPTool pins it (iapproto.DialUDP).
func dialBoard(cfg config) (net.Conn, error) {
	conn, err := iapproto.DialUDP(cfg.ip, cfg.port)
	if err != nil {
		return nil, err
	}
	return conn, nil
}

// The board rate-limits discovery replies per source address.
const discoveryRateWindow = 2 * time.Second

// What IAPTool allows a discovery reply. A reply that arrives later is not
// merely slow: IAPTool has closed its socket by then and binds a fresh
// ephemeral port for the next try, so the late answer is dropped.
const toolReplyBudget = iapproto.CommandTimeout

func init() {
	register(testCase{id: "T1-01", title: "the board answers UDP discovery", run: runN1})
	register(testCase{id: "T1-02", title: "discovery survives repeated queries", run: runN2})
	register(testCase{id: "T1-03", title: "discovery replies arrive inside the tool's timeout", run: runN3})
	register(testCase{id: "T1-04", title: "discovery holds up over a long soak", run: runN4})
	register(testCase{id: "T1-05", title: "a flood is capped without killing discovery", run: runN5})
}

// The device-wide ceiling on discovery replies, mirrored from
// DISCOVERY_MAX_REPLIES_PER_SEC in both udp_server.c files.
const discoveryRepliesPerSec = 50

// runN5 checks the cap from both sides: it must actually hold under a flood,
// and the device must still be answering normally right afterwards. A cap that
// bricks discovery for the next caller is worse than no cap.
func runN5(cfg config) result {
	const floodFor = 3 * time.Second

	conn, err := dialBoard(cfg)
	if err != nil {
		return fail("could not open a socket: %v", err)
	}
	defer conn.Close()

	replies := 0
	done := make(chan struct{})
	go func() {
		buf := make([]byte, 256)
		for {
			select {
			case <-done:
				return
			default:
			}
			_ = conn.SetReadDeadline(time.Now().Add(200 * time.Millisecond))
			if _, err := conn.Read(buf); err == nil {
				replies++
			}
		}
	}()

	sent := 0
	deadline := time.Now().Add(floodFor)
	for time.Now().Before(deadline) {
		if _, err := conn.Write([]byte("openplc_server_where_r_y")); err != nil {
			break
		}
		sent++
		time.Sleep(2 * time.Millisecond) // ~500/s, ten times the cap
	}
	close(done)
	time.Sleep(500 * time.Millisecond)

	seconds := floodFor.Seconds()
	rate := float64(replies) / seconds
	allowed := float64(discoveryRepliesPerSec) * 1.5 // fixed windows let a burst straddle a boundary
	fmt.Printf("    sent %d queries in %.0fs, got %d replies (%.0f/s, cap %d/s)\n",
		sent, seconds, replies, rate, discoveryRepliesPerSec)

	if rate > allowed {
		return fail("the board answered %.0f replies/s under a flood; the %d/s cap is not holding",
			rate, discoveryRepliesPerSec)
	}

	// The cap must be a speed limit, not a fuse.
	time.Sleep(discoveryRateWindow)
	if _, err := udpAsk(cfg, "openplc_server_where_r_y", 3*time.Second); err != nil {
		return fail("capped correctly, but the board stopped answering afterwards: %v", err)
	}
	return pass("flood of %d queries drew %d replies (%.0f/s, cap %d/s); normal discovery still works",
		sent, replies, rate, discoveryRepliesPerSec)
}

// runN4 is the instrument for a fault that short runs keep missing: it keeps
// asking for --minutes and reports every failure with the time it happened and
// how long the board had been answering fine before it. Short bursts have
// repeatedly come back clean while the real tool still failed minutes later.
func runN4(cfg config) result {
	deadline := time.Now().Add(cfg.soak)

	// Varying this is how you tell a device-side periodic event from something
	// locked to the query count: a real 30s period keeps failures 30s apart no
	// matter how often you ask, a count-based one keeps them N rounds apart.
	interval := cfg.interval
	if interval == 0 {
		interval = discoveryRateWindow + 500*time.Millisecond
	}

	start := time.Now()
	rounds, failures := 0, 0
	lastFailure := time.Time{}
	var worst time.Duration

	fmt.Printf("    soaking for %s, one query every %s...\n", cfg.soak, interval)

	for time.Now().Before(deadline) {
		rounds++
		queryStart := time.Now()
		_, err := udpAsk(cfg, "openplc_server_where_r_y", 10*time.Second)
		elapsed := time.Since(queryStart)

		if err != nil {
			failures++
			since := "start"
			if !lastFailure.IsZero() {
				since = time.Since(lastFailure).Round(time.Second).String() + " since the previous one"
			}
			lastFailure = time.Now()
			fmt.Printf("    [%s] round %d SILENT after %s (%s)\n",
				time.Since(start).Round(time.Second), rounds, elapsed.Round(time.Millisecond), since)
		} else if elapsed > worst {
			worst = elapsed
		}
		time.Sleep(interval)
	}

	if failures > 0 {
		return fail("%d of %d queries went unanswered over %s (slowest good reply %s)",
			failures, rounds, cfg.soak, worst.Round(time.Millisecond))
	}
	return pass("%d queries over %s, all answered, slowest %s", rounds, cfg.soak, worst.Round(time.Millisecond))
}

// udpAsk sends one datagram and waits for the reply.
func udpAsk(cfg config, msg string, timeout time.Duration) (string, error) {
	conn, err := dialBoard(cfg)
	if err != nil {
		return "", err
	}
	defer conn.Close()

	if err := conn.SetDeadline(time.Now().Add(timeout)); err != nil {
		return "", err
	}
	if _, err := conn.Write([]byte(msg)); err != nil {
		return "", fmt.Errorf("send %q: %w", msg, err)
	}
	buf := make([]byte, 256)
	n, err := conn.Read(buf)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(buf[:n])), nil
}

func runN1(cfg config) result {
	// Every keyword the board accepts, so a reply to one and silence on another
	// points straight at the keyword matching rather than at the socket.
	keywords := []string{"openplc_server_where_r_y", "DISCOVER", "openplc_discover", "ping"}

	answered := map[string]string{}
	for i, kw := range keywords {
		if i > 0 {
			// Stay outside the per-source rate limit, otherwise a silent reply
			// would just mean "asked too soon".
			time.Sleep(discoveryRateWindow + 500*time.Millisecond)
		}
		reply, err := udpAsk(cfg, kw, 3*time.Second)
		if err != nil {
			fmt.Printf("    %-26s no reply (%v)\n", kw, err)
			continue
		}
		fmt.Printf("    %-26s %s\n", kw, reply)
		answered[kw] = reply
	}

	if len(answered) == 0 {
		// Until 2026-09-18 this said "yet its TCP server is up" without ever
		// probing TCP. The claim was false on a board that was simply off, and
		// it sent the reader after the UDP path instead. Ask before telling.
		conn, err := dial(cfg)
		if err != nil {
			return fail("the board answered none of the %d discovery keywords, and its TCP port is "+
				"not reachable either (%v) -- the board is absent, not a UDP fault", len(keywords), err)
		}
		conn.Close()
		return fail("the board answered none of the %d discovery keywords, yet its TCP server accepts "+
			"connections -- the UDP path is down on its own", len(keywords))
	}
	if len(answered) < len(keywords) {
		missing := []string{}
		for _, kw := range keywords {
			if _, ok := answered[kw]; !ok {
				missing = append(missing, kw)
			}
		}
		return fail("answered %d of %d keywords; silent on: %s", len(answered), len(keywords), strings.Join(missing, ", "))
	}
	return pass("answered all %d discovery keywords", len(keywords))
}

// runN3 separates "the board never answered" from "the board answered too late
// for the tool to still be listening". Both look identical from IAPTool, but
// only one of them is a firmware problem.
func runN3(cfg config) result {
	const rounds = 20
	const patience = 10 * time.Second

	var latencies []time.Duration
	slow, silent := 0, 0
	peer := ""

	for i := 0; i < rounds; i++ {
		if i > 0 {
			time.Sleep(discoveryRateWindow + 500*time.Millisecond)
		}
		start := time.Now()
		reply, err := udpAsk(cfg, "openplc_server_where_r_y", patience)
		elapsed := time.Since(start)

		// The bootloader and the application run separate UDP servers; a result
		// says nothing about the other one.
		if err == nil && peer == "" {
			peer = reply
			fmt.Printf("    answering side: %s\n", peer)
		}

		if err != nil {
			silent++
			fmt.Printf("    round %2d: SILENT after %s (%v)\n", i+1, elapsed.Round(time.Millisecond), err)
			continue
		}
		latencies = append(latencies, elapsed)
		if elapsed > toolReplyBudget {
			slow++
			fmt.Printf("    round %2d: %s  <-- LATE, past the %s the tool waits\n",
				i+1, elapsed.Round(time.Millisecond), toolReplyBudget)
		}
	}

	if len(latencies) > 0 {
		min, max, total := latencies[0], latencies[0], time.Duration(0)
		for _, d := range latencies {
			if d < min {
				min = d
			}
			if d > max {
				max = d
			}
			total += d
		}
		fmt.Printf("    %d replies: min %s, mean %s, max %s\n", len(latencies),
			min.Round(time.Millisecond), (total / time.Duration(len(latencies))).Round(time.Millisecond),
			max.Round(time.Millisecond))
	}

	switch {
	case silent > 0 && slow > 0:
		return fail("%d/%d silent and %d late (over %s) -- the board both drops and delays replies",
			silent, rounds, slow, toolReplyBudget)
	case silent > 0:
		return fail("%d/%d queries got no reply within %s -- the board really is dropping them, "+
			"not just answering slowly", silent, rounds, patience)
	case slow > 0:
		return fail("%d/%d replies arrived after %s. IAPTool would have given up and closed its "+
			"socket, so these look like a missing board even though the firmware answered",
			slow, rounds, toolReplyBudget)
	}
	return pass("all %d replies arrived inside the %s the tool waits", rounds, toolReplyBudget)
}

func runN2(cfg config) result {
	const rounds = 6

	replies, refused := 0, 0
	for i := 0; i < rounds; i++ {
		if i > 0 {
			time.Sleep(discoveryRateWindow + 500*time.Millisecond)
		}
		reply, err := udpAsk(cfg, "openplc_server_where_r_y", 3*time.Second)
		if err != nil {
			refused++
			fmt.Printf("    round %d: no reply (%v)\n", i+1, err)
			continue
		}
		replies++
		fmt.Printf("    round %d: %s\n", i+1, reply)
	}

	if refused > 0 {
		return fail("%d of %d spaced-out queries went unanswered -- discovery is not reliable, "+
			"and every ether upgrade starts with one of these", refused, rounds)
	}
	return pass("all %d spaced-out queries answered", replies)
}
