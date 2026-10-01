// T1-12: an image whose signature is perfectly well-formed, but made with a key
// this board does not trust.
//
// T1-11 sends a signature no key could have produced (64 zero bytes). T1-12 sends a
// real ECDSA P-256 signature over the real image -- just from the wrong signer.
// The board must refuse both, but for different reasons, and keeping them apart
// is not pedantry: they were once tested as one case, and when it failed the
// cause was diagnosed as a bug in the verification code when what had actually
// happened was a key rotation.
//
// The signature comes from IAPTool itself, run as a subprocess with a throwaway
// key, for the same reason the transfer cases launch IAPTool rather than
// reimplementing the transfer: the only thing that should differ from a genuine
// upload is the one variable under test. A hand-rolled signer here would also be
// testing this file's idea of how r||s is encoded, which is T1-19/T1-20's job.
//
// IAPTool cannot produce this case on its own -- its getpubkey pre-check refuses
// to send an image the board will not accept, which is exactly case T1-18b.
package main

import (
	"encoding/hex"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
)

var pubKeyLineRe = regexp.MustCompile(`Public key: ([0-9a-fA-F]{128})`)

// genThrowawayKey makes a fresh P-256 key with "IAPTool genkey" and returns the
// path to the PEM and the matching public key as hex, parsed from the
// "Public key:" line genkey prints on stdout.
func genThrowawayKey(iapTool, dir string) (string, string, error) {
	cmd := exec.Command(iapTool, "genkey", "wrong_key")
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		return "", "", fmt.Errorf("IAPTool genkey failed: %v\n%s", err, out)
	}

	pubHex := ""
	if m := pubKeyLineRe.FindStringSubmatch(string(out)); m != nil {
		pubHex = strings.ToLower(m[1])
	}
	if len(pubHex) != 128 {
		return "", "", fmt.Errorf("genkey output parsed to %d hex chars, expected 128:\n%s", len(pubHex), out)
	}

	pem := filepath.Join(dir, "wrong_key.pem")
	if _, err := os.Stat(pem); err != nil {
		return "", "", fmt.Errorf("genkey reported success but %s is missing: %v", pem, err)
	}
	return pem, pubHex, nil
}

func runS2(cfg config) result {
	image, r := loadImage(cfg)
	if !r.pass {
		return r
	}
	if _, err := os.Stat(cfg.iapTool); err != nil {
		return fail("needs IAPTool to sign with (looked for %s): %v -- pass --iaptool=<path>", cfg.iapTool, err)
	}
	iapTool, err := filepath.Abs(cfg.iapTool)
	if err != nil {
		return fail("could not resolve %s: %v", cfg.iapTool, err)
	}

	dir, err := os.MkdirTemp("", "s2-wrongkey-")
	if err != nil {
		return fail("could not make a scratch directory: %v", err)
	}
	defer os.RemoveAll(dir)

	keyPath, wrongPubHex, err := genThrowawayKey(iapTool, dir)
	if err != nil {
		return fail("%v", err)
	}
	fmt.Printf("    throwaway signing key: %s...\n", wrongPubHex[:16])

	// Ask the board which key it trusts, and refuse to go on if it is the one
	// about to sign. A fresh P-256 key colliding is not a real possibility; what
	// this guards against is somebody later editing this case to sign with a
	// fixed key file. Getting that wrong would not fail the test -- it would
	// flash the board with whatever --bin points at.
	conn, err := dial(cfg)
	if err != nil {
		return fail("could not connect: %v", err)
	}
	boardPub, pubErr := ask(conn, "getpubkey", dialTimeout)
	conn.Close()

	switch {
	case pubErr != nil:
		return fail("getpubkey failed: %v", pubErr)
	case strings.Contains(boardPub, "Unknown command"):
		fmt.Printf("    board does not implement getpubkey (older bootloader); cannot confirm the keys differ\n")
	case len(strings.TrimSpace(boardPub)) != 128:
		return fail("getpubkey returned %q, which is not a 64-byte key -- refusing to upload blind", boardPub)
	case strings.EqualFold(strings.TrimSpace(boardPub), wrongPubHex):
		return fail("the board trusts the very key this case signs with -- uploading would flash it for real")
	default:
		fmt.Printf("    board trusts:          %s...  (different, good)\n", strings.ToLower(boardPub)[:16])
	}

	// Sign a copy, so the .sig lands in the scratch directory and the caller's
	// own .sig (if any) is left alone.
	imgCopy := filepath.Join(dir, "s2_image.bin")
	if err := os.WriteFile(imgCopy, image, 0644); err != nil {
		return fail("could not stage the image: %v", err)
	}
	signOut, err := exec.Command(iapTool, "sign", imgCopy, keyPath).CombinedOutput()
	if err != nil {
		return fail("IAPTool sign failed: %v\n%s", err, signOut)
	}
	sig, err := os.ReadFile(strings.TrimSuffix(imgCopy, ".bin") + ".sig")
	if err != nil {
		return fail("IAPTool sign produced no .sig: %v", err)
	}
	if len(sig) != 64 {
		return fail("the .sig is %d bytes, expected a raw 64-byte r||s signature", len(sig))
	}

	fmt.Printf("    signature is well-formed and covers this exact image -- only the signer is wrong\n")
	res := uploadWithSignature(cfg, image, hex.EncodeToString(sig))
	if res.pass {
		res.detail = "signed by an untrusted key: " + res.detail
	}
	return res
}
