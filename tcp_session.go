// Test cases for the bootloader's TCP session rules: one client at a time, and
// an idle client does not hold the port forever.
package main

import (
	"fmt"
	"net"
	"os/exec"
	"time"

	"IAPTool/iapproto"
)

const (
	// The bootloader drops a connection that has been silent this long.
	idleKickAfter = 60 * time.Second
	// How far past the deadline T1-07 waits before calling it a miss.
	idleKickSlack = 15 * time.Second
	// How long T1-08 stays silent while expecting to survive.
	idleSurvive = 50 * time.Second

	dialTimeout = 5 * time.Second
)

func init() {
	register(testCase{id: "T1-07", title: "an idle connection is dropped after 60s", run: runT1})
	register(testCase{id: "T1-08", title: "an idle connection survives 50s", run: runT1b})
	register(testCase{id: "T1-06", title: "a second connection is refused while one is open", run: runT2})
	register(testCase{id: "T1-10", title: "a new connection is accepted after the first closes", run: runT4})

	// Flashes the board, which then reboots into the application.
	register(testCase{id: "T1-09", title: "a second connection does not disturb a running transfer",
		destructive: true, run: runT3})
}

// dial opens the board's TCP channel pinned to the physical NIC, as IAPTool
// does: a VPN endpoint will complete a handshake for an address the board does
// not hold, so an unpinned dial reports "the TCP server is up" for a board that
// is switched off (2026-09-18). See $PROD/docs/tables/DECISIONS.md decision 51.
func dial(cfg config) (net.Conn, error) {
	return iapproto.DialTCP(cfg.ip, cfg.port, dialTimeout)
}

// alive reports whether the session still answers. "ping" is the cheapest
// command the bootloader implements and it replies "OK".
func alive(conn net.Conn) error {
	if err := conn.SetDeadline(time.Now().Add(dialTimeout)); err != nil {
		return err
	}
	if _, err := conn.Write([]byte("ping\n")); err != nil {
		return fmt.Errorf("write: %w", err)
	}
	buf := make([]byte, 64)
	n, err := conn.Read(buf)
	if err != nil {
		return fmt.Errorf("read: %w", err)
	}
	if n < 2 || string(buf[:2]) != "OK" {
		return fmt.Errorf("unexpected reply %q", string(buf[:n]))
	}
	return nil
}

// waitClosed blocks until the peer closes the connection or limit elapses.
// Returns how long it took, and whether a close was actually seen.
func waitClosed(conn net.Conn, limit time.Duration) (time.Duration, bool) {
	start := time.Now()
	if err := conn.SetReadDeadline(time.Now().Add(limit)); err != nil {
		return 0, false
	}
	buf := make([]byte, 64)
	for {
		_, err := conn.Read(buf)
		if err == nil {
			continue // unsolicited data: keep waiting for the close
		}
		if netErr, ok := err.(net.Error); ok && netErr.Timeout() {
			return time.Since(start), false
		}
		return time.Since(start), true
	}
}

func runT1(cfg config) result {
	conn, err := dial(cfg)
	if err != nil {
		return fail("could not connect: %v", err)
	}
	defer conn.Close()

	fmt.Printf("    connected, staying silent for up to %s...\n", idleKickAfter+idleKickSlack)
	elapsed, closed := waitClosed(conn, idleKickAfter+idleKickSlack)
	if !closed {
		return fail("still connected after %s, expected a drop at ~%s", elapsed.Round(time.Second), idleKickAfter)
	}
	return pass("dropped after %s", elapsed.Round(time.Second))
}

func runT1b(cfg config) result {
	conn, err := dial(cfg)
	if err != nil {
		return fail("could not connect: %v", err)
	}
	defer conn.Close()

	fmt.Printf("    connected, staying silent for %s...\n", idleSurvive)
	elapsed, closed := waitClosed(conn, idleSurvive)
	if closed {
		return fail("dropped after only %s, must survive %s", elapsed.Round(time.Second), idleSurvive)
	}
	if err := alive(conn); err != nil {
		return fail("survived %s but stopped answering: %v", idleSurvive, err)
	}
	return pass("still connected and answering after %s", idleSurvive)
}

func runT2(cfg config) result {
	first, err := dial(cfg)
	if err != nil {
		return fail("could not open the first connection: %v", err)
	}
	defer first.Close()

	if err := alive(first); err != nil {
		return fail("first connection did not answer: %v", err)
	}

	second, err := dial(cfg)
	if err != nil {
		// Refused at connect time is the cleanest possible outcome.
		if err := alive(first); err != nil {
			return fail("second connection was refused but the first one broke: %v", err)
		}
		return pass("second connection refused (%v), first still answering", err)
	}
	defer second.Close()

	// Accepted at TCP level: it must then be dropped without being served.
	if err := alive(second); err == nil {
		return fail("second connection was accepted and served -- the board is talking to two clients")
	}
	if err := alive(first); err != nil {
		return fail("second connection was rejected but the first one broke: %v", err)
	}
	return pass("second connection accepted but not served, first still answering")
}

func runT3(cfg config) result {
	if cfg.binPath == "" {
		return fail("needs --bin=<file.bin>: this case runs a real upload")
	}

	// No --downgrade flag: 382086d (2026-09-03) removed anti-rollback from
	// IAPTool and cleaned up every other caller, but missed this one. The tool
	// answers an unknown option with [FATAL], so this case could not run at all
	// between then and 2026-09-18 -- while R1-02 and R1-18 still counted it as
	// their evidence.
	cmd := exec.Command(cfg.iapTool, "ether", cfg.binPath, cfg.ip)
	out, err := cmd.StdoutPipe()
	if err != nil {
		return fail("could not capture IAPTool output: %v", err)
	}
	cmd.Stderr = cmd.Stdout
	if err := cmd.Start(); err != nil {
		return fail("could not start %s: %v", cfg.iapTool, err)
	}

	transferred := make(chan bool, 1)
	sending := make(chan struct{})
	completed := make(chan struct{})
	ended := make(chan struct{})
	go watchForCompletion(out, transferred, sending, completed, ended)

	// Knock once the data phase has demonstrably started, never on a timer.
	//
	// Until 2026-09-18 this slept 8s. An ethernet upload is four phases --
	// signing, a preflight connection, a gap with no session open, then the
	// data connection -- so a fixed delay landed in a different one each run
	// and the case failed about half the time for two reasons that were both
	// correct board behaviour: in the gap the board has no session, so it
	// accepts the knock (read as "the intruder was served"), and the knock can
	// then hold the slot that IAPTool's data connection needs (read as "the
	// transfer failed"). Measured over 15 runs: 7 pass, 8 fail, and the board
	// never once printed "Refused second connection."
	select {
	case <-sending:
	case <-ended:
		// IAPTool stopped without ever sending a chunk. Most often the board is
		// still rebooting from the previous run's upload and this one arrived
		// too early. Nothing was knocked against, so this says nothing about
		// R1-18 -- report it as setup, not as a verdict.
		_ = cmd.Wait()
		<-transferred
		return fail("IAPTool exited before sending any data -- nothing to knock " +
			"against. Give the board time to finish rebooting between runs")
	case <-time.After(3 * time.Minute):
		return fail("IAPTool never reported sending data; nothing was knocked against")
	}
	// Knock immediately: the first "Sent" line means the data connection is open
	// at this instant. There is no margin to spend waiting -- the PC-side send
	// of a 1.75 MB image takes only a few seconds (the ~34s in run_s4.py is the
	// board's staging-to-erase window, not the wire time), and even a 500ms
	// pause lost the race half the time.
	//
	// The transfer can still finish first on a fast link or a small image. That
	// is not a failure of R1-18, so it is reported as a setup problem -- but
	// IAPTool has to be reaped either way, or the next run starts against a
	// board that is still mid-upload.
	if finishedBeforeKnock(completed) {
		_ = cmd.Wait()
		<-transferred
		return fail("the transfer finished before the knock -- use a larger image " +
			"(run_s4.py pads to IAP_APP_MAX_SIZE for the same reason)")
	}

	second, dialErr := dial(cfg)
	intruderServed := false
	if dialErr == nil {
		intruderServed = alive(second) == nil
		second.Close()
	}

	waitErr := cmd.Wait()
	ok := <-transferred

	switch {
	case intruderServed:
		return fail("the intruding connection was served while a transfer was running")
	case waitErr != nil:
		return fail("the transfer failed while a second connection knocked: %v", waitErr)
	case !ok:
		return fail("IAPTool exited cleanly but never reported a completed transfer")
	}
	return pass("transfer completed; intruder was refused (dial err: %v)", dialErr)
}

func runT4(cfg config) result {
	first, err := dial(cfg)
	if err != nil {
		return fail("could not open the first connection: %v", err)
	}
	if err := alive(first); err != nil {
		first.Close()
		return fail("first connection did not answer: %v", err)
	}
	first.Close()

	// The board needs a moment to notice the FIN and free the slot.
	time.Sleep(2 * time.Second)

	second, err := dial(cfg)
	if err != nil {
		return fail("could not reconnect after a clean close: %v -- the slot is stuck", err)
	}
	defer second.Close()

	if err := alive(second); err != nil {
		return fail("reconnected but the session did not answer: %v -- the slot is stuck", err)
	}
	return pass("reconnected and served after the first session closed")
}
