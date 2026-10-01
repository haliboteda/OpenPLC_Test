// netifquery prints the local IP a socket to the given target should be
// bound to, using the same physical-vs-virtual interface rule as IAP_Ether.go
// and $CORE_REPO/tools/discovery.
//
// It exists so the Python test scripts (which cannot import a Go package)
// get the same answer without a second implementation of the per-OS
// classifiers -- writing that logic a third time is how one rule ends up
// with three unmaintained copies. See $PROD/docs/tables/DECISIONS.md
// decision 51.
//
//	go run ./tools/netifquery 192.168.0.3
//
// Prints the local IP on stdout and exits 0. Prints nothing (still exit 0)
// when no physical interface shares that subnet: the target is reached
// through a router, and the caller should let the OS route it normally, not
// treat empty output as an error. Exit 2 on a bad argument.
package main

import (
	"fmt"
	"net"
	"os"

	"IAPTool/netiface"
)

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: netifquery <target-ip>")
		os.Exit(2)
	}
	target := net.ParseIP(os.Args[1])
	if target == nil {
		fmt.Fprintf(os.Stderr, "not an IP address: %q\n", os.Args[1])
		os.Exit(2)
	}
	if local := netiface.LocalIPFor(target); local != nil {
		fmt.Println(local.String())
	}
}
