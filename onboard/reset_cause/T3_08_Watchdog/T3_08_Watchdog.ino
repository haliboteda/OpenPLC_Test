// T3-08 on the real board: the sketch reads a watchdog reset as WATCHDOG.
// Driven by tools/run_t3_08.py. Prints on RS232 (C05/C06).
// Boot 1 (not a watchdog reset): feed for 3 s, then stop feeding -> IWDG reset.
// Boot 2: must report WATCHDOG; then feeds forever and prints "fed ok".
OPENPLC_APP_VERSION(1, 0, 0);

#include <OpenPLC_Ports.h>
#include <IWatchdog.h>

static const char *name(openplc_reset_cause_t c) {
  switch (c) {
    case OPENPLC_RESET_POWER_ON: return "POWER_ON";
    case OPENPLC_RESET_PIN:      return "PIN";
    case OPENPLC_RESET_SOFTWARE: return "SOFTWARE";
    case OPENPLC_RESET_WATCHDOG: return "WATCHDOG";
    case OPENPLC_RESET_BROWNOUT: return "BROWNOUT";
    default:                     return "UNKNOWN";
  }
}

static bool hang;

void setup() {
  pinMode(RS232_EN, OUTPUT);
  digitalWrite(RS232_EN, HIGH);
  RS232.begin(115200);
  delay(200);
  openplc_reset_cause_t c = openplcResetCause();
  RS232.print("WDT cause=");
  RS232.print(name(c));
  RS232.print(" isReset=");
  RS232.println(IWatchdog.isReset() ? 1 : 0);
  hang = (c != OPENPLC_RESET_WATCHDOG);
  IWatchdog.begin(1000000);  // 1 s
}

void loop() {
  static uint32_t t0 = millis();
  static uint32_t last;
  if (!hang || millis() - t0 < 3000) {
    IWatchdog.reload();
  } else if (millis() - last > 100) {
    last = millis();
    RS232.println("WDT not feeding");
  }
  if (!hang && millis() - last > 1000) {
    last = millis();
    RS232.println("WDT fed ok");
  }
}
