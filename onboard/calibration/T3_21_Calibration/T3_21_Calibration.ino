// T3-21 on the real board: the calibration in sector 15 reaches the AI readings.
// Driven by tools/run_calibration_on_board.py. Prints on RS232 (C05/C06).
// Each line carries the raw 16-bit reading taken just before and just after the
// converted one, so the script can recompute nominal * gain + offset itself.
OPENPLC_APP_VERSION(1, 0, 0);

#include <OpenPLC_Ports.h>

static const char *name(calib_status_t s) {
  switch (s) {
    case CALIB_OK:          return "OK";
    case CALIB_BLANK:       return "BLANK";
    case CALIB_CORRUPT:     return "CORRUPT";
    case CALIB_OTHER_BOARD: return "OTHER_BOARD";
    default:                return "?";
  }
}

void setup() {
  pinMode(RS232_EN, OUTPUT);
  digitalWrite(RS232_EN, HIGH);
  RS232.begin(115200);
  delay(200);
  RS232.println("CAL boot");
  // First call: reference on, calibration read, and the library's one log line.
  (void)openplcReadAI1_mV();
  RS232.print("CAL status=");
  RS232.println(name(openplcCalibrationStatus()));
}

void loop() {
  // openplcReadAI*() leave analogRead() at 16 bits, so these raws are on the same scale.
  uint32_t a0 = analogRead(AIN_1);
  float mv = openplcReadAI1_mV();
  uint32_t a1 = analogRead(AIN_1);
  uint32_t b0 = analogRead(AIN_2);
  float ma = openplcReadAI2_mA();
  uint32_t b1 = analogRead(AIN_2);

  RS232.print("CAL ai1 raw=");
  RS232.print(a0);
  RS232.print(",");
  RS232.print(a1);
  RS232.print(" mv=");
  RS232.print(mv, 2);
  RS232.print(" ai2 raw=");
  RS232.print(b0);
  RS232.print(",");
  RS232.print(b1);
  RS232.print(" ma=");
  RS232.println(ma, 4);
  delay(500);
}
