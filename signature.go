// T1-11: the pure signature-verification path.
//
// IAPTool cannot produce this case by design -- its getpubkey pre-check refuses
// to transfer an image the board will not accept. So this drives the protocol
// directly, using the shipping crypto package (IAPTool/iapcert) rather than a
// reimplementation: only the *image signature* is deliberately wrong, everything
// else (certificate, challenge signature, CRC, framing) is exactly what IAPTool
// would send.
package main

import (
	"fmt"
	"hash/crc32"
	"net"
	"os"
	"strings"
	"time"

	"IAPTool/iapcert"
	"IAPTool/iapproto"
)

const (
	chunkSize     = iapproto.ChunkSize
	verifyTimeout = 30 * time.Second
)

func init() {
	// Not destructive since SDRAM staging landed: the image is verified in the
	// staging buffer and the application region is only erased once it passes,
	// so a refused upload leaves the running application intact. Verified on
	// hardware 2026-08-17 -- T1-11 refused the image, and the board booted its
	// existing application after a reset.
	//
	// Before staging this case did leave the board unable to boot, which is why
	// it used to be flagged destructive and sorted last by "all".
	register(testCase{id: "T1-11", title: "an image with an invalid signature is rejected",
		destructive: false, run: runS1})

	// T1-12 is kept apart from T1-11 on purpose. Both end in "the board refuses to
	// run it", but they are different failures: T1-11 is a signature no key could
	// have produced, T1-12 is a perfectly well-formed signature from the wrong
	// key. Testing them together once led to a key rotation being diagnosed as
	// a bug in the verification code.
	register(testCase{id: "T1-12", title: "an image signed by a key the board does not trust is rejected",
		destructive: false, run: runS2})
}

// ask sends one text command and returns the reply.
func ask(conn net.Conn, cmd string, timeout time.Duration) (string, error) {
	if err := conn.SetDeadline(time.Now().Add(timeout)); err != nil {
		return "", err
	}
	if _, err := conn.Write([]byte(cmd + "\n")); err != nil {
		return "", fmt.Errorf("write %q: %w", cmd, err)
	}
	buf := make([]byte, 512)
	n, err := conn.Read(buf)
	if err != nil {
		return "", fmt.Errorf("read after %q: %w", cmd, err)
	}
	return strings.TrimSpace(string(buf[:n])), nil
}

// loadImage does the argument checking both signature cases need, and returns
// the image bytes. The key is the one the board trusts: session auth is
// certificate-based, so getting the command accepted at all means signing a
// fresh challenge with a key this board's root vouches for.
func loadImage(cfg config) ([]byte, result) {
	if cfg.binPath == "" {
		return nil, fail("needs --bin=<file.bin>")
	}
	if cfg.keyPath == "" {
		return nil, fail("needs --key=<owner.pem> (the key this board trusts, to authenticate the command)")
	}
	image, err := os.ReadFile(cfg.binPath)
	if err != nil {
		return nil, fail("could not read %s: %v", cfg.binPath, err)
	}
	if len(image) < chunkSize {
		return nil, fail("image is only %d bytes, too small to be a real app", len(image))
	}
	return image, result{pass: true}
}

func runS1(cfg config) result {
	image, r := loadImage(cfg)
	if !r.pass {
		return r
	}

	// Change the image so it also stops matching the metadata already stored on
	// the board. That way this one run exercises both halves of the signature
	// path: the upload-time check now, and the boot-time check on the next reset.
	image[len(image)/2] ^= 0xFF

	// 64 zero bytes: not a signature any key could ever produce.
	return uploadWithSignature(cfg, image, strings.Repeat("00", 64))
}

// uploadWithSignature runs a complete, correctly authenticated upload whose
// only defect is the signature it carries, and reports what the board said.
// Everything except sigHex is exactly what IAPTool would send.
func uploadWithSignature(cfg config, image []byte, sigHex string) result {
	return uploadImage(cfg, image, sigHex, crc32.ChecksumIEEE(image), judgeVerdict)
}

// uploadImage is the shared upload path. checksum and judge are parameters so a
// case can break exactly one thing and say what it expects back: the signature
// cases send the real CRC, the CRC case sends a wrong one.
func uploadImage(cfg config, image []byte, sigHex string, checksum uint32,
	judge func(string) result) result {
	conn, err := dial(cfg)
	if err != nil {
		return fail("could not connect: %v", err)
	}
	defer conn.Close()

	if err := alive(conn); err != nil {
		return fail("board did not answer ping: %v", err)
	}

	uidHex, err := ask(conn, "getuid", dialTimeout)
	if err != nil {
		return fail("getuid failed: %v", err)
	}
	fmt.Printf("    target UID=%s\n", strings.TrimSpace(uidHex))

	leafKey, err := iapcert.LoadKey(cfg.keyPath)
	if err != nil {
		return fail("could not use %s: %v", cfg.keyPath, err)
	}
	certHex, err := iapcert.Issue(cfg.keyPath, "")
	if err != nil {
		return fail("could not issue a certificate with %s: %v", cfg.keyPath, err)
	}

	authMsg := fmt.Sprintf("flash %d %x %s", len(image), checksum, sigHex)

	nonceHex, err := ask(conn, "authchallenge", dialTimeout)
	if err != nil {
		return fail("authchallenge failed: %v", err)
	}
	nonceSig, err := iapcert.NonceSig(leafKey, nonceHex, authMsg)
	if err != nil {
		return fail("could not sign the challenge: %v", err)
	}
	flashCmd := fmt.Sprintf("%s %s %s", authMsg, certHex, nonceSig)

	// The command itself must authenticate: a rejected command would prove
	// nothing about signature verification.
	reply, err := ask(conn, flashCmd, verifyTimeout)
	if err != nil {
		return fail("flash command failed: %v", err)
	}
	if !strings.Contains(reply, "OK") {
		return fail("board rejected the flash command itself (%q) -- this case needs it accepted, "+
			"otherwise nothing about signature checking is proven", reply)
	}
	fmt.Printf("    flash command accepted, sending %d bytes with a bogus signature...\n", len(image))

	for sent := 0; sent < len(image); {
		end := sent + chunkSize
		if end > len(image) {
			end = len(image)
		}
		if err := conn.SetDeadline(time.Now().Add(verifyTimeout)); err != nil {
			return fail("set deadline: %v", err)
		}
		if _, err := conn.Write(image[sent:end]); err != nil {
			return fail("sending bytes at offset %d: %v", sent, err)
		}
		buf := make([]byte, 256)
		n, err := conn.Read(buf)
		if err != nil {
			return fail("no reply after the chunk at offset %d: %v", sent, err)
		}
		sent = end

		// The last chunk is answered with "OK" and then the verdict, which may
		// arrive in the same read.
		if sent >= len(image) {
			verdict := strings.TrimSpace(string(buf[:n]))
			if !mentionsVerdict(verdict) {
				if err := conn.SetDeadline(time.Now().Add(verifyTimeout)); err != nil {
					return fail("set deadline: %v", err)
				}
				n, err = conn.Read(buf)
				if err != nil {
					return fail("board never reported a verdict: %v", err)
				}
				verdict = strings.TrimSpace(string(buf[:n]))
			}
			return judge(verdict)
		}
	}
	return fail("ran out of image without a verdict")
}

func mentionsVerdict(s string) bool {
	return strings.Contains(s, "Signature Failed") ||
		strings.Contains(s, "No Signature") ||
		strings.Contains(s, "Checksum Failed")
}

func judgeVerdict(verdict string) result {
	switch {
	case strings.Contains(verdict, "Signature Failed"), strings.Contains(verdict, "No Signature"):
		return pass("board refused the image: %q. The application region was never touched, "+
			"so the previously-installed application still boots -- reset to confirm (case T1-14)", verdict)
	case strings.Contains(verdict, "Checksum Failed"):
		return fail("board reported a checksum failure (%q), so the signature check never ran -- "+
			"the CRC this tool computed does not match what the board computed", verdict)
	default:
		return fail("board accepted an image it should have refused (replied %q). "+
			"Signature verification is not gating the update", verdict)
	}
}
