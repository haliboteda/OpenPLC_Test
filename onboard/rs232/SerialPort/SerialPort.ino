/* Required by the core since 2026-09-21: a sketch without a version does not
 * link. Test fixtures all use 1.0.0 -- the upload gate lets equal versions
 * through, so this never blocks re-flashing a fixture. */
OPENPLC_APP_VERSION(1, 0, 0);

/*
 * SerialPort - Echo test for the hardware UART and the USB-CDC virtual port.
 *
 * Every byte received on either port is echoed back out on that same port,
 * prefixed with "U:" (UART) or "C:" (CDC) so it's obvious which port
 * produced a given echo when watching both at once. Used to bench-test the
 * physical UART wiring and the USB-CDC stack independently of the rest of
 * the firmware.
 *
 * Board menu requirement: Tools > USB > "CDC (no generic 'Serial')".
 * That setting keeps Serial bound to the hardware UART and adds SerialUSB
 * as a separate USB-CDC port; the other USB menu option ("CDC (generic
 * 'Serial' supersede U(S)ART)") replaces Serial with the CDC port instead,
 * which would leave the hardware UART untested by this sketch.
 *
 * Wiring:
 *   Serial (UART4) RX : PH14  (JunctionLink connector)
 *   Serial (UART4) TX : PH13  (JunctionLink connector)
 *   SerialUSB         : the board's USB device connector
 */

#define ECHO_BAUD 115200

void setup()
{
    // The UART reaches terminals C05/C06 through the on-board MAX3221 (U5),
    // whose enable is gated by RS232_EN_Pin via MOSFET Q5. The Arduino core's
    // main.cpp only does pinMode(PB_10, OUTPUT) and never writes a level, so
    // the pin sits at its reset value (low) and the transceiver stays off --
    // nothing reaches the terminals until this drives it high.
    digitalWrite(RS232_EN_Pin, HIGH);

    Serial_Test.begin(ECHO_BAUD);
    Serial_Test.println("UART echo ready");

#if defined(USBCON) && defined(USBD_USE_CDC)
    SerialUSB.begin(ECHO_BAUD);
    SerialUSB.println("CDC echo ready");
#endif
}

void loop()
{
    while (Serial_Test.available()) {
        Serial_Test.print("U:");
        Serial_Test.write(Serial_Test.read());
    }

#if defined(USBCON) && defined(USBD_USE_CDC)
    while (SerialUSB.available()) {
        SerialUSB.print("C:");
        SerialUSB.write(SerialUSB.read());
    }
#endif
}
