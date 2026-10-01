/* Required by the core since 2026-09-21: a sketch without a version does not
 * link. Test fixtures all use 1.0.0 -- the upload gate lets equal versions
 * through, so this never blocks re-flashing a fixture. */
OPENPLC_APP_VERSION(1, 0, 0);

// Minimal app image for boot + IAP acceptance runs.
// Prints a banner the test scripts can anchor on, then idles.
//
// Build (normal):
//   arduino-cli compile --fqbn OpenPLC_Alpha:stm32:OPEN-PLC \
//       --output-dir <dir> onboard/iap_probe
//
// Build with a MAC that is NOT the one derived from the chip UID -- this is what
// R1-13 needs, and it takes no code change: OpenPLC_Net/src/ethernetif.c uses
// MAC_ADDR0..5 verbatim when all six are defined, and only derives from the UID
// otherwise.
//
//   FLAGS="-DVECT_TAB_OFFSET={build.flash_offset} \
//          -DMAC_ADDR0=0x02 -DMAC_ADDR1=0xBB -DMAC_ADDR2=0x49 \
//          -DMAC_ADDR3=0xDE -DMAC_ADDR4=0xAD -DMAC_ADDR5=0x01"
//   arduino-cli compile --fqbn OpenPLC_Alpha:stm32:OPEN-PLC --clean \
//       --build-property "compiler.c.extra_flags=$FLAGS" \
//       --build-property "compiler.cpp.extra_flags=$FLAGS" \
//       --output-dir <dir> onboard/iap_probe
//
// ⛔ -DVECT_TAB_OFFSET={build.flash_offset} is not optional. --build-property
// REPLACES the property, and platform.txt already sets compiler.c.extra_flags to
// exactly that define. Dropping it builds an image whose vector table is at the
// wrong offset: it flashes and verifies fine, then faults the instant the
// bootloader jumps into it, leaving a board that answers neither ethernet nor
// CDC. Recovering one takes the BOOT0 gesture. Measured 2026-09-18.
//
// The board takes its address by DHCP, so a changed MAC also changes the IP.
// That is the point of R1-13: find the board by broadcast and UID, not by MAC.

#include "OpenPLC_IAP_Autostart.h"   // udp receive counters, see loop()

// Stamped into the boot banner so a script can tell two builds of this sketch
// apart. The five images built before 2026-09-21 all printed byte-identical
// banners, so nothing could judge "did the upgrade take". Set it at build time:
// tools/build_probe_image.py --ver v2. "dev" means nobody said.
//
// A BARE token, not a string: quotes do not survive the trip through
// arduino-cli's --build-property into the compiler command line (measured
// 2026-09-21: "missing terminating \" character"). PROBE_VER_STR does the
// quoting here, where nothing can eat it.
#ifndef PROBE_VER
#define PROBE_VER dev
#endif
#define PROBE_VER_STR2(x) #x
#define PROBE_VER_STR(x)  PROBE_VER_STR2(x)

void setup() {
  // Open the RS232 transceiver. The core only does pinMode(PB_10, OUTPUT) and
  // never drives it, so the pin sits low and MAX3221's charge pump stays off --
  // with it off the driver physically cannot make a line level, and every
  // printf() inside the IAP libraries is lost. Serial (USB CDC) is unaffected;
  // this is what makes the library's own diagnostics visible.
  // See $PROD/docs/hardware/HARDWARE-FACTS.md, "RS232".
  pinMode(RS232_EN_Pin, OUTPUT);
  digitalWrite(RS232_EN_Pin, HIGH);

  // newlib buffers stdout, so a printf() inside the IAP libraries sits in RAM
  // until the buffer fills -- the diagnostics never reach the line. Unbuffered
  // is what makes them visible at the moment they happen.
  setvbuf(stdout, NULL, _IONBF, 0);

  Serial.begin(115200);
  Serial.println("IAP_PROBE_APP up " PROBE_VER_STR(PROBE_VER));
  printf("IAP_PROBE_APP up " PROBE_VER_STR(PROBE_VER) " (RS232 enabled)\r\n");
}

void loop() {
  Serial.println("IAP_PROBE_APP alive");
  // Also on RS232, so the library diagnostics and this heartbeat share one
  // channel -- Serial is USB CDC and never reaches the RS232 terminals.
  // Both channels on purpose: Serial_Test is the RS232 console directly, and
  // printf is the path the IAP libraries use. They agreed 30 lines to 30 over
  // the same window on 2026-09-21, once DEBUG_UART was pinned to USART3/PC10 --
  // before that printf resolved to PH13, the expansion header, and was lost.
  Serial_Test.println("IAP_PROBE_APP alive");
  printf("IAP_PROBE_APP alive via printf\r\n");

  // UDP receive counters, straight from the IAP library (getters are already
  // declared in OpenPLC_IAP_Autostart.h). A reboot request that never arrives
  // leaves rx unchanged; one that arrives but fails to parse bumps rx and
  // shows its length. That is the whole difference this probe exists to see.
  printf("  udp rx=%lu len=%u tick=%lu\r\n",
         openplc_udp_server_recv_count(),
         (unsigned)openplc_udp_server_last_rx_len(),
         openplc_udp_server_last_rx_tick());
  delay(1000);
}
