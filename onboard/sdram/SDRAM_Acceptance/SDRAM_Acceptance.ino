/* Required by the core since 2026-09-21: a sketch without a version does not
 * link. Test fixtures all use 1.0.0 -- the upload gate lets equal versions
 * through, so this never blocks re-flashing a fixture. */
OPENPLC_APP_VERSION(1, 0, 0);

/*
 * M3 / E5 acceptance: the OpenPLC_SDRAM wrapper.
 *
 * Prints one "RESULT <name> PASS|FAIL ..." line per check, then "DONE".
 * tools/run-sdram.ps1 reads those lines; nothing here depends on a human
 * looking at the output.
 *
 * The measurements at the end are not pass/fail -- they exist to answer the
 * open design question "how long does zeroing 64 MB actually take", which was
 * being guessed at.
 */

#include <OpenPLC_SDRAM.h>

static void result(const char *name, bool ok, const char *detail = nullptr)
{
  Serial_Test.print("RESULT ");
  Serial_Test.print(name);
  Serial_Test.print(ok ? " PASS" : " FAIL");
  if (detail) {
    Serial_Test.print(" ");
    Serial_Test.print(detail);
  }
  Serial_Test.println();
}

void setup()
{
  pinMode(RS232_EN_Pin, OUTPUT);
  digitalWrite(RS232_EN_Pin, HIGH);
  Serial_Test.begin(115200);
  delay(200);
  Serial_Test.println();
  Serial_Test.println("=== SDRAM acceptance ===");

  /* --- before begin(): the API must refuse, not hand out addresses ------- */
  result("alloc_before_begin_returns_null", SDRAM.alloc(1024) == nullptr);
  result("not_ready_before_begin", !SDRAM.ready());

  /* --- bring it up ------------------------------------------------------- */
  uint32_t t0 = micros();
  bool ok = SDRAM.begin();
  uint32_t beginUs = micros() - t0;
  result("begin", ok);
  if (!ok) {
    Serial_Test.println("DONE");
    return;
  }
  result("ready_after_begin", SDRAM.ready());
  result("begin_is_idempotent", SDRAM.begin());

  /* --- the claim the wrapper exists for: memory comes back zeroed -------- */
  const size_t ONE_MB = 1024UL * 1024UL;
  uint8_t *a = (uint8_t *)SDRAM.alloc(ONE_MB);
  result("alloc_1MB", a != nullptr);
  if (a == nullptr) {
    Serial_Test.println("DONE");
    return;
  }

  bool allZero = true;
  for (size_t i = 0; i < ONE_MB; i++) {
    if (a[i] != 0) { allZero = false; break; }
  }
  result("alloc_is_zeroed", allZero);

  result("alloc_is_aligned", ((uintptr_t)a % 8) == 0);
  result("alloc_is_in_sdram",
         (uintptr_t)a >= OpenPLC_SDRAM_Class::BASE &&
         (uintptr_t)a < OpenPLC_SDRAM_Class::BASE + OpenPLC_SDRAM_Class::CAPACITY);

  /* --- it must actually store data --------------------------------------- */
  for (size_t i = 0; i < ONE_MB; i += 4093) {
    a[i] = (uint8_t)(i * 31 + 7);
  }
  bool held = true;
  for (size_t i = 0; i < ONE_MB; i += 4093) {
    if (a[i] != (uint8_t)(i * 31 + 7)) { held = false; break; }
  }
  result("write_readback_1MB", held);

  /* --- two allocations must not overlap ---------------------------------- */
  uint8_t *b = (uint8_t *)SDRAM.alloc(ONE_MB);
  result("second_alloc", b != nullptr);
  if (b) {
    result("allocs_do_not_overlap", (b >= a + ONE_MB) || (a >= b + ONE_MB));
    /* the first buffer must survive the second allocation being zeroed */
    bool stillHeld = true;
    for (size_t i = 0; i < ONE_MB; i += 4093) {
      if (a[i] != (uint8_t)(i * 31 + 7)) { stillHeld = false; break; }
    }
    result("first_buffer_survives_second_alloc", stillHeld);
  }

  /* --- accounting and the refusal path ----------------------------------- */
  result("used_is_at_least_2MB", SDRAM.used() >= 2 * ONE_MB);
  result("available_plus_used_is_capacity",
         SDRAM.available() + SDRAM.used() == OpenPLC_SDRAM_Class::CAPACITY);
  result("oversize_alloc_returns_null",
         SDRAM.alloc(OpenPLC_SDRAM_Class::CAPACITY) == nullptr);
  result("zero_size_alloc_returns_null", SDRAM.alloc(0) == nullptr);

  /* --- measurements (not pass/fail) -------------------------------------- */
  Serial_Test.print("MEASURE begin_us ");
  Serial_Test.println(beginUs);

  size_t left = SDRAM.available();
  size_t chunk = 16UL * ONE_MB;
  if (chunk > left) { chunk = left; }
  t0 = micros();
  void *big = SDRAM.alloc(chunk);
  uint32_t zeroUs = micros() - t0;
  Serial_Test.print("MEASURE zero_bytes ");
  Serial_Test.println((uint32_t)chunk);
  Serial_Test.print("MEASURE zero_us ");
  Serial_Test.println(zeroUs);
  result("large_alloc", big != nullptr);

  t0 = micros();
  void *raw = SDRAM.allocUninitialized(chunk / 2);
  uint32_t rawUs = micros() - t0;
  Serial_Test.print("MEASURE uninit_us ");
  Serial_Test.println(rawUs);
  result("alloc_uninitialized", raw != nullptr);

  Serial_Test.println("DONE");
}

void loop() {}
