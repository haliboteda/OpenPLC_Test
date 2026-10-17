/*
 * CDC_Stall -- probe for TODO #70: when does USB CDC output stop, and does it
 * come back on its own. Driven by tools/run_usb_cdc_stall.py.
 *
 * USB (Serial): "seq N" every 5 ms.
 * RS232: every 500 ms "stat seq=N dtr=D conn=C dropped=X", where dropped
 * counts writes that came back short (the core drops data while
 * CDC_connected() is false).
 */

OPENPLC_APP_VERSION(1, 0, 0);

extern "C" bool CDC_connected(void);

static uint32_t seq;
static uint32_t dropped;
static uint32_t lastStat;

void setup() {
  Serial.begin(115200);
  RS232.begin(115200);
  RS232.println("CDC_Stall: ready");
}

void loop() {
  char line[32];
  int n = snprintf(line, sizeof line, "seq %lu\r\n", (unsigned long)seq++);
  if (Serial.write((const uint8_t *)line, n) != (size_t)n) {
    dropped++;
  }
  if (millis() - lastStat >= 500) {
    lastStat = millis();
    RS232.print("stat seq=");
    RS232.print(seq);
    RS232.print(" dtr=");
    RS232.print(Serial.dtr() ? 1 : 0);
    RS232.print(" conn=");
    RS232.print(CDC_connected() ? 1 : 0);
    RS232.print(" dropped=");
    RS232.println(dropped);
  }
  delay(5);
}
