// The CRC32 gate: an image whose checksum does not match is refused before the
// signature is ever looked at.
//
// Requirement R1-22 ("the image passes CRC32 first"), case T1-24. The shipping
// IAPTool cannot produce this case: it computes the checksum itself and has no
// way to get it wrong, so this drives the protocol directly, exactly as the
// signature cases do.
//
// The image also carries a bogus signature on purpose. That is what makes the
// reply meaningful: "Checksum Failed" rather than "Signature Failed" proves the
// CRC is checked FIRST, which is the part of the requirement that could silently
// stop being true.
package main

import (
	"hash/crc32"
	"strings"
)

func init() {
	// Not destructive: the CRC is checked against the staging buffer, and the
	// application region is only erased after every check has passed
	// (IAPServer/IAP_server.c -- the checksum branch sends its reply and logs
	// IAP_EVT_CRC_FAIL without touching flash).
	register(testCase{id: "T1-24", title: "an image with a wrong CRC32 is refused before the signature is checked",
		destructive: false, run: runBadCRC})
}

func runBadCRC(cfg config) result {
	image, r := loadImage(cfg)
	if !r.pass {
		return r
	}

	// Inverting every bit cannot collide with the real value.
	wrong := crc32.ChecksumIEEE(image) ^ 0xFFFFFFFF

	// 64 zero bytes: not a signature any key could produce. If the board were to
	// check the signature first, it would answer "Signature Failed" instead.
	return uploadImage(cfg, image, strings.Repeat("00", 64), wrong, judgeBadCRC)
}

func judgeBadCRC(verdict string) result {
	switch {
	case strings.Contains(verdict, "Checksum Failed"):
		return pass("board refused on the CRC before reaching the signature: %q", verdict)
	case strings.Contains(verdict, "Signature Failed"), strings.Contains(verdict, "No Signature"):
		return fail("board answered %q, so it checked the signature before the CRC -- "+
			"the CRC is not the first gate", verdict)
	default:
		return fail("board accepted an image whose CRC was wrong (replied %q). "+
			"The checksum is not gating the update", verdict)
	}
}
