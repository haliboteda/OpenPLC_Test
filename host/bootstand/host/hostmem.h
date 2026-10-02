/*
 * The board's memories at the addresses the firmware sources hard-code, so the
 * real IAPServer / OpenPLC_IAP code runs unmodified. Persistent ones are files
 * mapped in place: a simulated reset, and a test harness killing the process,
 * both leave them exactly as the firmware last wrote them.
 * $PROD/docs/engineering/BOOTLOADER-STAND-IN.md
 */

#ifndef BOOTSTAND_HOSTMEM_H_
#define BOOTSTAND_HOSTMEM_H_

#include <stdbool.h>
#include <stdint.h>

#define HOST_FLASH_BASE   0x08000000UL
#define HOST_FLASH_SIZE   (2UL * 1024UL * 1024UL)
#define HOST_SRAM4_BASE   0x38000000UL
#define HOST_SRAM4_SIZE   (64UL * 1024UL)
#define HOST_BKPSRAM_BASE 0x38800000UL
#define HOST_BKPSRAM_SIZE (4UL * 1024UL)
#define HOST_SDRAM_BASE   0xC0000000UL
#define HOST_SDRAM_SIZE   (2UL * 1024UL * 1024UL)

/* Maps every region under state_dir. Exits the process on failure: nothing in
 * the firmware can run without its memory map. */
void hostmem_init(const char *state_dir);

/* The RTC backup registers (32 words), persistent like the backup SRAM. */
extern volatile uint32_t *hostmem_rtc_bkp;

#endif /* BOOTSTAND_HOSTMEM_H_ */
