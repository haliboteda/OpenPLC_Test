// TestCase drives the OpenPLC bootloader through its test cases.
//
// It is deliberately separate from IAPTool: IAPTool only uploads firmware,
// everything that exists to exercise or probe the device lives here. Cases that
// need a real upload running launch IAPTool as a subprocess rather than
// reimplementing the transfer, so what gets tested is the shipping code path.
package main

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"time"
)

type testCase struct {
	id    string
	title string
	// destructive cases leave the board somewhere other than where they found
	// it, so "all" runs them last -- otherwise they fail every case after them.
	destructive bool
	// manual cases need a person at the board (pulling power, moving a jumper),
	// so "all" cannot run them unattended. It names them instead of dropping
	// them quietly: a skipped case that is never mentioned reads as a pass.
	manual bool
	run    func(cfg config) result
}

type config struct {
	ip        string
	port      string
	binPath   string
	iapTool   string
	keyPath   string
	soak      time.Duration
	interval  time.Duration
	stateFile string
	phase     int
	count     int
}

type result struct {
	pass   bool
	detail string
}

func pass(format string, a ...interface{}) result {
	return result{pass: true, detail: fmt.Sprintf(format, a...)}
}

func fail(format string, a ...interface{}) result {
	return result{pass: false, detail: fmt.Sprintf(format, a...)}
}

var cases = map[string]testCase{}

func register(c testCase) { cases[c.id] = c }

func usage() {
	fmt.Fprintf(os.Stderr, "Usage:\n  TestCase <case-id|all> --ip=<addr> [--port=56865] [--bin=<file.bin>]\n"+
		"      [--iaptool=<path>] [--key=<signing.pem>] [--minutes=N] [--interval=<ms>]\n"+
		"      [--state=<file> --phase=1|2 --count=N]   (T1-17)\n\nCases:\n")
	ids := make([]string, 0, len(cases))
	for id := range cases {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		fmt.Fprintf(os.Stderr, "  %-5s %s\n", id, cases[id].title)
	}
	fmt.Fprintf(os.Stderr, "\nSee $PROD/docs/engineering/HOW-TO-RUN-TESTS.md for what each case proves and what it needs.\n")
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}

	target := os.Args[1]
	cfg := config{port: "56865", iapTool: defaultIAPToolPath(), soak: 10 * time.Minute}

	for _, arg := range os.Args[2:] {
		switch {
		case hasPrefix(arg, "--ip="):
			cfg.ip = arg[len("--ip="):]
		case hasPrefix(arg, "--port="):
			cfg.port = arg[len("--port="):]
		case hasPrefix(arg, "--bin="):
			cfg.binPath = arg[len("--bin="):]
		case hasPrefix(arg, "--iaptool="):
			cfg.iapTool = arg[len("--iaptool="):]
		case hasPrefix(arg, "--key="):
			cfg.keyPath = arg[len("--key="):]
		case hasPrefix(arg, "--minutes="):
			minutes, convErr := strconv.Atoi(arg[len("--minutes="):])
			if convErr != nil || minutes <= 0 {
				fmt.Fprintf(os.Stderr, "--minutes needs a positive whole number\n")
				os.Exit(2)
			}
			cfg.soak = time.Duration(minutes) * time.Minute
		case hasPrefix(arg, "--interval="):
			ms, convErr := strconv.Atoi(arg[len("--interval="):])
			if convErr != nil || ms <= 0 {
				fmt.Fprintf(os.Stderr, "--interval needs a positive number of milliseconds\n")
				os.Exit(2)
			}
			cfg.interval = time.Duration(ms) * time.Millisecond
		case hasPrefix(arg, "--state="):
			cfg.stateFile = arg[len("--state="):]
		case hasPrefix(arg, "--phase="):
			phase, convErr := strconv.Atoi(arg[len("--phase="):])
			if convErr != nil || (phase != 1 && phase != 2) {
				fmt.Fprintf(os.Stderr, "--phase needs 1 or 2\n")
				os.Exit(2)
			}
			cfg.phase = phase
		case hasPrefix(arg, "--count="):
			n, convErr := strconv.Atoi(arg[len("--count="):])
			if convErr != nil || n < 2 {
				fmt.Fprintf(os.Stderr, "--count needs at least 2\n")
				os.Exit(2)
			}
			cfg.count = n
		default:
			fmt.Fprintf(os.Stderr, "unknown option %q\n", arg)
			os.Exit(2)
		}
	}

	if cfg.ip == "" {
		fmt.Fprintln(os.Stderr, "--ip is required")
		os.Exit(2)
	}

	var toRun []testCase
	var skipped []testCase
	if target == "all" {
		ids := make([]string, 0, len(cases))
		for id := range cases {
			ids = append(ids, id)
		}
		sort.Slice(ids, func(i, j int) bool {
			a, b := cases[ids[i]], cases[ids[j]]
			if a.destructive != b.destructive {
				return !a.destructive
			}
			return ids[i] < ids[j]
		})
		for _, id := range ids {
			if cases[id].manual {
				skipped = append(skipped, cases[id])
				continue
			}
			toRun = append(toRun, cases[id])
		}
	} else {
		c, ok := cases[target]
		if !ok {
			fmt.Fprintf(os.Stderr, "unknown case %q\n\n", target)
			usage()
			os.Exit(2)
		}
		toRun = []testCase{c}
	}

	failed := 0
	for _, c := range toRun {
		fmt.Printf("=== %s  %s\n", c.id, c.title)
		r := c.run(cfg)
		if r.pass {
			fmt.Printf("--- PASS %s: %s\n\n", c.id, r.detail)
		} else {
			fmt.Printf("--- FAIL %s: %s\n\n", c.id, r.detail)
			failed++
		}
	}

	// Named, never silent: "all passed" has to mean something, and it cannot if
	// whole cases vanished from the run without being mentioned.
	for _, c := range skipped {
		fmt.Printf("SKIPPED %s (%s) -- needs somebody at the board; run it by id\n", c.id, c.title)
	}

	if failed > 0 {
		fmt.Printf("%d of %d case(s) FAILED\n", failed, len(toRun))
		os.Exit(1)
	}
	fmt.Printf("all %d case(s) passed", len(toRun))
	if len(skipped) > 0 {
		fmt.Printf(", %d skipped as manual", len(skipped))
	}
	fmt.Println()
}

func hasPrefix(s, p string) bool { return len(s) >= len(p) && s[:len(p)] == p }

// defaultIAPToolPath is this platform's build from IAPTranfer_Tool's
// compile_tool.sh, in the sibling repo (the IAPTool under test lives there).
// tools/run_case.py passes --iaptool explicitly; this is for running by hand
// from this repo's root.
func defaultIAPToolPath() string {
	name := "IAPTool"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	return filepath.Join("..", "IAPTranfer_Tool", "Output", runtime.GOOS, name)
}
