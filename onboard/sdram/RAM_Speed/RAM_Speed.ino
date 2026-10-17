/* Required by the core: a sketch without a version does not link. */
OPENPLC_APP_VERSION(1, 0, 0);

/*
 * RAM speed: DTCM vs AXI SRAM vs SDRAM, D-cache off and on (TODO #74).
 *
 * A measurement, not a pass/fail test. Output on RS232, 115200:
 *   INFO <name> <value>      CPU clock, cache and MPU state at the time
 *   COLS ...                 column names of the ROW lines
 *   ROW <cache> <region> <op> <bytes> <cycles> <MiB/s> <check>
 *   DONE
 * How to run it and where the numbers go:
 * OpenPLC_Docs docs/engineering/HOW-TO-RUN-TESTS.md "RAM 速率实验".
 */

#include <OpenPLC_SDRAM.h>
#include <string.h>

/* Nothing is linked into DTCMRAM (variants/STM32H7xx/H743/ldscript.ld). */
static const uint32_t DTCM_BASE = 0x20000000UL;
static const uint32_t DTCM_BYTES = 128UL * 1024UL;
/* Both well past the 16 KiB D-cache. */
static const uint32_t AXI_BYTES = 256UL * 1024UL;
static const uint32_t SDRAM_BYTES = 256UL * 1024UL;

static const int REPS = 5;

static uint32_t axiBuf[AXI_BYTES / 4] __attribute__((aligned(32)));

struct Region {
  const char *name;
  uint32_t *base;
  uint32_t bytes;
};

struct Row {
  bool cacheOn;
  const char *region;
  const char *op;
  uint32_t bytes;
  uint32_t cycles;
  bool ok;
};

static Row rows[2 * 3 * 3];
static int nRows;

static void cycInit()
{
  CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
  DWT->LAR = 0xC5ACCE55UL; /* Cortex-M7 DWT is write-locked until this */
  DWT->CYCCNT = 0;
  DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
}

/* volatile keeps every access a single 32-bit load/store; unrolled by 8 so
 * loop overhead does not hide the faster regions. words is a multiple of 8. */
static void __attribute__((noinline)) write32(volatile uint32_t *p, uint32_t words)
{
  for (uint32_t i = 0; i < words; i += 8) {
    p[i] = i;         p[i + 1] = i + 1; p[i + 2] = i + 2; p[i + 3] = i + 3;
    p[i + 4] = i + 4; p[i + 5] = i + 5; p[i + 6] = i + 6; p[i + 7] = i + 7;
  }
}

static uint32_t __attribute__((noinline)) read32(const volatile uint32_t *p, uint32_t words)
{
  uint32_t s = 0;
  for (uint32_t i = 0; i < words; i += 8) {
    s += p[i];     s += p[i + 1]; s += p[i + 2]; s += p[i + 3];
    s += p[i + 4]; s += p[i + 5]; s += p[i + 6]; s += p[i + 7];
  }
  return s;
}

static void addRow(bool cacheOn, const char *region, const char *op,
                   uint32_t bytes, uint32_t cycles, bool ok)
{
  Row &r = rows[nRows++];
  r.cacheOn = cacheOn;
  r.region = region;
  r.op = op;
  r.bytes = bytes;
  r.cycles = cycles;
  r.ok = ok;
}

static void measure(bool cacheOn, const Region &rg)
{
  const uint32_t words = rg.bytes / 4;
  uint32_t best;

  best = 0xFFFFFFFFUL;
  for (int k = 0; k < REPS; k++) {
    uint32_t t0 = DWT->CYCCNT;
    write32(rg.base, words);
    uint32_t dt = DWT->CYCCNT - t0;
    if (dt < best) best = dt;
  }
  addRow(cacheOn, rg.name, "write32", rg.bytes, best, true);

  /* sum of 0..words-1, modulo 2^32 */
  const uint32_t expect = (uint32_t)(((uint64_t)words * (words - 1)) / 2);
  bool ok = true;
  best = 0xFFFFFFFFUL;
  for (int k = 0; k < REPS; k++) {
    uint32_t t0 = DWT->CYCCNT;
    uint32_t s = read32(rg.base, words);
    uint32_t dt = DWT->CYCCNT - t0;
    if (dt < best) best = dt;
    if (s != expect) ok = false;
  }
  addRow(cacheOn, rg.name, "read32", rg.bytes, best, ok);

  const uint32_t half = rg.bytes / 2;
  uint8_t *src = (uint8_t *)rg.base;
  uint8_t *dst = src + half;
  best = 0xFFFFFFFFUL;
  for (int k = 0; k < REPS; k++) {
    uint32_t t0 = DWT->CYCCNT;
    memcpy(dst, src, half);
    uint32_t dt = DWT->CYCCNT - t0;
    if (dt < best) best = dt;
  }
  addRow(cacheOn, rg.name, "memcpy", half, best, memcmp(dst, src, half) == 0);
}

static bool dcacheOn() { return (SCB->CCR & SCB_CCR_DC_Msk) != 0; }

/*
 * One pass over every region with the D-cache in the given state. The
 * Ethernet IRQ is masked for the pass: with the MPU off, the lwIP DMA buffers
 * become cacheable once the D-cache is on, so lwIP must not run meanwhile.
 * Nothing is printed inside the pass for the same reason.
 */
static void pass(bool cacheOn, const Region *regions, int n)
{
  RS232.flush();
  const bool ethIrq = NVIC_GetEnableIRQ(ETH_IRQn) != 0;
  NVIC_DisableIRQ(ETH_IRQn);

  if (cacheOn) {
    SCB_EnableDCache();
  } else {
    SCB_DisableDCache();
  }
  for (int i = 0; i < n; i++) {
    measure(cacheOn, regions[i]);
  }
  /* back to the build's default: off */
  SCB_DisableDCache();

  if (ethIrq) {
    NVIC_EnableIRQ(ETH_IRQn);
  }
}

static void info(const char *name, uint32_t v, bool hex = false)
{
  RS232.print("INFO ");
  RS232.print(name);
  RS232.print(hex ? " 0x" : " ");
  RS232.println(v, hex ? HEX : DEC);
}

void setup()
{
  RS232.begin(115200);
  delay(200);
  RS232.println();
  RS232.println("=== RAM speed ===");

  cycInit();
  const bool cacheAtBoot = dcacheOn();
  info("cpu_hz", SystemCoreClock);
  info("dcache_at_boot", cacheAtBoot ? 1 : 0);
  info("icache", (SCB->CCR & SCB_CCR_IC_Msk) ? 1 : 0);
  info("mpu_ctrl", MPU->CTRL, true);
  info("reps_best_of", REPS);

  Region regions[3];
  int n = 0;
  regions[n++] = { "DTCM", (uint32_t *)DTCM_BASE, DTCM_BYTES };
  regions[n++] = { "AXI", axiBuf, AXI_BYTES };

  const bool sdramOk = SDRAM.begin();
  info("sdram_begin", sdramOk ? 1 : 0);
  if (sdramOk) {
    uint32_t *p = (uint32_t *)SDRAM.allocUninitialized(SDRAM_BYTES, 32);
    if (p != nullptr) {
      regions[n++] = { "SDRAM", p, SDRAM_BYTES };
    }
  }

  pass(false, regions, n);
  pass(true, regions, n);
  if (cacheAtBoot) {
    SCB_EnableDCache();
  }

  RS232.println("COLS cache region op bytes cycles MiB_per_s check");
  for (int i = 0; i < nRows; i++) {
    const Row &r = rows[i];
    const double mibs = (double)r.bytes * (double)SystemCoreClock /
                        (double)r.cycles / (1024.0 * 1024.0);
    RS232.print("ROW ");
    RS232.print(r.cacheOn ? "on " : "off ");
    RS232.print(r.region);
    RS232.print(" ");
    RS232.print(r.op);
    RS232.print(" ");
    RS232.print(r.bytes);
    RS232.print(" ");
    RS232.print(r.cycles);
    RS232.print(" ");
    RS232.print(mibs, 1);
    RS232.println(r.ok ? " PASS" : " FAIL");
  }
  RS232.println("DONE");
}

void loop() {}
