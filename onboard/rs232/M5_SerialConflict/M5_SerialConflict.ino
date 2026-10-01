/* Required by the core since 2026-09-21: a sketch without a version does not
 * link. Test fixtures all use 1.0.0 -- the upload gate lets equal versions
 * through, so this never blocks re-flashing a fixture. */
OPENPLC_APP_VERSION(1, 0, 0);

/*
 * M5 / E7 acceptance: the core's diagnostic port must survive a user sketch
 * opening its own Serial.
 *
 * Serial_Test (PC11/PC10, the RS232 terminals C05/C06) is what the core prints
 * [BOOT] and [NET] on. Serial is the user's port. Both used to resolve to
 * UART4, and uart_handlers[] has one slot per peripheral -- so the last begin()
 * won and the other one silently lost its RX.
 *
 * THE TEST IS RECEIVE, NOT TRANSMIT. That is the whole point: in the broken
 * configuration transmit keeps working, so anything that only checks output
 * reports a pass. Drive this with tools/run-m5.ps1, which sends a byte into the
 * terminal and waits for the echo.
 *
 * Expected:
 *   broken core  -> "[M5] ready" appears, but bytes sent in are never echoed
 *   fixed core   -> bytes sent in come back as "[echo] X"
 */

void setup() {
  // The transceiver is off by default (PB10 low = MAX3221 shutdown), so
  // nothing reaches the terminals until the sketch turns it on. See
  // docs/design/HARDWARE-FACTS.md.
  pinMode(RS232_EN_Pin, OUTPUT);
  digitalWrite(RS232_EN_Pin, HIGH);

  Serial_Test.begin(115200);

  // The collision, on purpose and in the order that triggers it: the user's
  // UART4 port is opened AFTER the core's. On a broken core this is the line
  // that kills Serial_Test's receive path.
  //
  // ⚠️ Serial4, NOT Serial. With the usb=CDCgen menu option -- which is what
  // this board builds with -- "Serial" is the USB CDC and never touches UART4,
  // so a test written against Serial passes on a broken core. That happened:
  // the first version of this sketch used Serial and reported a clean pass
  // against the very core it was meant to indict.
  Serial4.begin(115200);

  Serial_Test.println();
  Serial_Test.println("[M5] ready - send me a byte");
}

void loop() {
  while (Serial_Test.available()) {
    char c = (char)Serial_Test.read();
    Serial_Test.print("[echo] ");
    Serial_Test.println(c);
  }
  delay(5);
}
