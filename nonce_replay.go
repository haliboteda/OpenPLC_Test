// T1-17: the device must never issue the same authentication nonce twice, and in
// particular must not repeat itself after losing power.
//
// Each nonce is 16 bytes from the hardware RNG (decision 66; IAPServer/iap_auth.c
// rng_words). The case collects nonces before and after a real power cut and
// requires every one of them to be distinct. The power cut has to be real --
// a reset does not restart the RNG the way power-up does. Since the network
// drops with the power, the case runs in two phases with the nonces from phase 1
// kept in a state file. tools/run_au1.py drives both.
package main

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"strings"
	"time"
)

func init() {
	register(testCase{
		id:     "T1-17",
		title:  "nonces do not repeat, and do not restart after a power cycle",
		manual: true, // needs somebody to pull the plug between the two phases
		run:    runAU1,
	})
}

// nonceHexLen is IAP_AUTH_NONCE_SIZE (16) as hex.
const nonceHexLen = 32

type nonceSample struct {
	Hex string `json:"hex"`
}

type au1State struct {
	Role    string        `json:"role"`
	Taken   string        `json:"taken"`
	Samples []nonceSample `json:"samples"`
}

func parseNonce(s string) (nonceSample, error) {
	s = strings.TrimSpace(s)
	if len(s) != nonceHexLen {
		return nonceSample{}, fmt.Errorf("expected %d hex chars, got %d (%q)", nonceHexLen, len(s), s)
	}
	raw, err := hex.DecodeString(s)
	if err != nil {
		return nonceSample{}, fmt.Errorf("not hex: %w", err)
	}
	_ = raw
	return nonceSample{Hex: strings.ToLower(s)}, nil
}

// askOnce writes one command and reads the reply, accumulating until want
// bytes have arrived rather than trusting a single Read to return them all.
func askOnce(conn net.Conn, cmd string, want int) (string, error) {
	if err := conn.SetDeadline(time.Now().Add(dialTimeout)); err != nil {
		return "", err
	}
	if _, err := conn.Write([]byte(cmd + "\n")); err != nil {
		return "", fmt.Errorf("write %q: %w", cmd, err)
	}
	var got []byte
	buf := make([]byte, 256)
	for len(strings.TrimSpace(string(got))) < want {
		n, err := conn.Read(buf)
		if n > 0 {
			got = append(got, buf[:n]...)
		}
		if err != nil {
			if len(got) == 0 {
				return "", fmt.Errorf("read after %q: %w", cmd, err)
			}
			break
		}
	}
	return strings.TrimSpace(string(got)), nil
}

// collectNonces asks for count challenges on one session.
func collectNonces(cfg config, count int) ([]nonceSample, error) {
	conn, err := dial(cfg)
	if err != nil {
		return nil, fmt.Errorf("could not connect: %w", err)
	}
	defer conn.Close()

	samples := make([]nonceSample, 0, count)
	for i := 0; i < count; i++ {
		reply, err := askOnce(conn, "authchallenge", nonceHexLen)
		if err != nil {
			return nil, fmt.Errorf("challenge %d: %w", i+1, err)
		}
		s, err := parseNonce(reply)
		if err != nil {
			return nil, fmt.Errorf("challenge %d: %w", i+1, err)
		}
		samples = append(samples, s)
	}
	return samples, nil
}

func runAU1(cfg config) result {
	if cfg.stateFile == "" {
		return fail("needs --state=<file> to carry phase 1 across the power cycle")
	}
	count := cfg.count
	if count <= 0 {
		count = 8
	}

	// Record which side answered: the bootloader and the application each issue
	// their own nonces, so a result from one says nothing about the other.
	role := "unknown"
	if reply, err := udpAsk(cfg, "openplc_server_where_r_y", 3*time.Second); err == nil {
		role = reply
	}

	switch cfg.phase {
	case 1:
		samples, err := collectNonces(cfg, count)
		if err != nil {
			return fail("phase 1: %v", err)
		}
		// Fail early rather than sending somebody to pull the plug for nothing.
		if r := checkRun(samples, "phase 1"); !r.pass {
			return r
		}
		st := au1State{Role: role, Taken: time.Now().Format(time.RFC3339), Samples: samples}
		blob, err := json.MarshalIndent(st, "", "  ")
		if err != nil {
			return fail("phase 1: could not encode state: %v", err)
		}
		if err := os.WriteFile(cfg.stateFile, blob, 0644); err != nil {
			return fail("phase 1: could not write %s: %v", cfg.stateFile, err)
		}
		for i, s := range samples {
			fmt.Printf("    %2d  %s\n", i+1, s.Hex)
		}
		fmt.Printf("    answering side: %s\n", role)
		fmt.Printf("    wrote %s -- now REMOVE POWER, restore it, and run phase 2\n", cfg.stateFile)
		return pass("phase 1 took %d distinct nonces; power-cycle the board and run phase 2", len(samples))

	case 2:
		blob, err := os.ReadFile(cfg.stateFile)
		if err != nil {
			return fail("phase 2: could not read %s: %v -- run phase 1 first", cfg.stateFile, err)
		}
		var st au1State
		if err := json.Unmarshal(blob, &st); err != nil {
			return fail("phase 2: %s is not valid state: %v", cfg.stateFile, err)
		}
		if len(st.Samples) == 0 {
			return fail("phase 2: %s holds no phase 1 nonces", cfg.stateFile)
		}

		after, err := collectNonces(cfg, count)
		if err != nil {
			return fail("phase 2: %v", err)
		}
		for i, s := range after {
			fmt.Printf("    %2d  %s\n", i+1, s.Hex)
		}
		fmt.Printf("    answering side: phase 1 %s / phase 2 %s\n", st.Role, role)

		return checkAcrossPowerCycle(st.Samples, after)

	default:
		return fail("needs --phase=1 (before the power cycle) or --phase=2 (after it)")
	}
}

// checkRun validates one uninterrupted run of challenges: every nonce distinct.
func checkRun(s []nonceSample, label string) result {
	if len(s) < 2 {
		return fail("%s: only %d nonce(s); need at least 2", label, len(s))
	}
	return distinct(s, label)
}

// distinct fails on the first nonce seen twice.
func distinct(s []nonceSample, label string) result {
	seen := map[string]int{}
	for i, n := range s {
		if first, dup := seen[n.Hex]; dup {
			return fail("%s: nonce %s was issued twice (samples %d and %d) -- a captured (nonce, signature) "+
				"pair from the first can be replayed against the second", label, n.Hex, first+1, i+1)
		}
		seen[n.Hex] = i
	}
	return pass("%s: %d nonces, all distinct", label, len(s))
}

func checkAcrossPowerCycle(before, after []nonceSample) result {
	if r := checkRun(after, "phase 2"); !r.pass {
		return r
	}
	all := append(append([]nonceSample{}, before...), after...)
	if r := distinct(all, "across the power cycle"); !r.pass {
		return r
	}
	return pass("%d nonces across a power cycle, all distinct", len(all))
}
