// T1-17: the device must never issue the same authentication nonce twice, and in
// particular must not start over after losing power.
//
// The board has no hardware RNG, so the nonce is not random -- it is
//
//	counter(4B, LE) || UID word0(4B, LE) || HAL_GetTick()(4B, LE) || 0(4B)
//
// (IAPServer/iap_auth.c iap_auth_issue_challenge). What defeats a replay is
// therefore uniqueness, not unpredictability: an attacker needs the HMAC key,
// which observing nonces never yields. Uniqueness rests entirely on the counter
// living in an RTC backup register kept alive by VBAT. If that register is
// zeroed or shared, the counter restarts and a captured (nonce, HMAC) pair
// becomes replayable.
//
// That is not hypothetical. On 2026-08-17 the bootloader's VBAT witness and the
// application's nonce counter both occupied DR2, so the application reissued the
// same run of nonce numbers after every visit to the bootloader. It was found by
// reading serial logs by eye. This case is the instrument that finds it
// automatically.
//
// The power cycle has to be real -- pulling power, not a reset. A reset never
// touches the backup domain, so a reset-only version of this case would pass on
// a board with a dead VBAT cell, which is exactly the board it exists to catch.
// Since the network drops with the power, the case runs in two phases with the
// nonces from phase 1 kept in a state file. tools/run_au1.py drives both.
package main

import (
	"encoding/binary"
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
	Hex     string `json:"hex"`
	Counter uint32 `json:"counter"`
	UID0    uint32 `json:"uid0"`
	Tick    uint32 `json:"tick"`
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
	// The tail is memset to zero by the firmware. If it is not zero the layout
	// assumed here is wrong, and every conclusion below would be drawn from the
	// wrong four bytes -- so this is checked rather than ignored.
	for _, b := range raw[12:16] {
		if b != 0 {
			return nonceSample{}, fmt.Errorf("bytes 12..16 are %x, expected zero -- nonce layout has changed", raw[12:16])
		}
	}
	return nonceSample{
		Hex:     strings.ToLower(s),
		Counter: binary.LittleEndian.Uint32(raw[0:4]),
		UID0:    binary.LittleEndian.Uint32(raw[4:8]),
		Tick:    binary.LittleEndian.Uint32(raw[8:12]),
	}, nil
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

	// Record which side answered. The bootloader and the application keep their
	// counters in different backup registers (DR1 and DR2), so a result from one
	// says nothing about the other -- and the collision this case exists to catch
	// was precisely between those two.
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
			fmt.Printf("    %2d  %s  counter=%d tick=%d\n", i+1, s.Hex, s.Counter, s.Tick)
		}
		fmt.Printf("    answering side: %s\n", role)
		fmt.Printf("    wrote %s -- now REMOVE POWER, restore it, and run phase 2\n", cfg.stateFile)
		return pass("phase 1 took %d nonces, counter %d..%d; power-cycle the board and run phase 2",
			len(samples), samples[0].Counter, samples[len(samples)-1].Counter)

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
			fmt.Printf("    %2d  %s  counter=%d tick=%d\n", i+1, s.Hex, s.Counter, s.Tick)
		}
		fmt.Printf("    answering side: phase 1 %s / phase 2 %s\n", st.Role, role)

		return checkAcrossPowerCycle(st.Samples, after)

	default:
		return fail("needs --phase=1 (before the power cycle) or --phase=2 (after it)")
	}
}

// checkRun validates one uninterrupted run of challenges. Nothing else talks to
// the board during a phase, so the counter must advance by exactly one each
// time: a jump would mean somebody else is consuming challenges, which would
// make the cross-phase comparison meaningless.
func checkRun(s []nonceSample, label string) result {
	if len(s) < 2 {
		return fail("%s: only %d nonce(s); need at least 2 to see the counter move", label, len(s))
	}
	uid := s[0].UID0
	for i := range s {
		if s[i].UID0 != uid {
			return fail("%s: nonce %d carries UID word0 %08x but nonce 1 carried %08x -- two different boards answered",
				label, i+1, s[i].UID0, uid)
		}
		if i > 0 && s[i].Counter != s[i-1].Counter+1 {
			return fail("%s: counter went %d -> %d between nonce %d and %d, expected +1 "+
				"(something else is consuming challenges, or the counter is not monotonic)",
				label, s[i-1].Counter, s[i].Counter, i, i+1)
		}
	}
	return pass("%s: counter advanced %d -> %d by one each time", label, s[0].Counter, s[len(s)-1].Counter)
}

// checkAcrossPowerCycle is the assertion C5 actually rests on.
func checkAcrossPowerCycle(before, after []nonceSample) result {
	if r := checkRun(after, "phase 2"); !r.pass {
		return r
	}

	if before[0].UID0 != after[0].UID0 {
		return fail("phase 1 saw UID word0 %08x and phase 2 saw %08x -- phase 2 talked to a different board, "+
			"so nothing here is comparable", before[0].UID0, after[0].UID0)
	}

	// Every nonce, both phases, must be distinct as a whole 16-byte value.
	seen := map[string]int{}
	all := append(append([]nonceSample{}, before...), after...)
	for i, s := range all {
		if first, dup := seen[s.Hex]; dup {
			return fail("nonce %s was issued twice (samples %d and %d) -- a captured (nonce, HMAC) pair "+
				"from the first can be replayed against the second", s.Hex, first+1, i+1)
		}
		seen[s.Hex] = i
	}

	lastBefore := before[len(before)-1].Counter
	firstAfter := after[0].Counter

	// The failure this is built for: the counter restarting. It shows up as the
	// post-cycle counter being at or below where the pre-cycle run ended, which
	// means the same counter values -- and so the same nonces -- get handed out
	// again.
	if firstAfter <= lastBefore {
		reused := 0
		for _, s := range after {
			if s.Counter <= lastBefore {
				reused++
			}
		}
		hint := "the RTC backup register did not survive the power cycle: VBAT cell dead or absent, " +
			"or another firmware image is writing the same register"
		if firstAfter <= 1 {
			hint = "the counter restarted from zero -- the backup domain was lost entirely"
		}
		return fail("counter was %d before the power cycle and %d after it: %d of %d post-cycle nonces "+
			"reuse counter values already issued. %s",
			lastBefore, firstAfter, reused, len(after), hint)
	}

	gap := firstAfter - lastBefore
	return pass("%d nonces across a power cycle, all distinct; counter continued %d -> %d (gap %d, "+
		"the challenges the reboot path itself consumed) and never restarted",
		len(all), lastBefore, firstAfter, gap)
}
