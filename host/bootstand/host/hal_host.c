/*
 * The hardware under both halves: flash with the H7's rules (32-byte
 * words, program once, erase a whole 128 KiB sector), the CRC unit as Core/Src
 * /crc.c configures it, RNG, RTC backup registers, reset. Peripherals the
 * stand-in has no use for are no-ops.
 */

#include "main.h"
#include "hostctl.h"
#include "hostmem.h"

#include <stdlib.h>

#ifdef _WIN32
#include <windows.h>
#include <bcrypt.h>
#endif

CRC_HandleTypeDef hcrc;
RNG_HandleTypeDef hrng;
RTC_HandleTypeDef hrtc;
UART_HandleTypeDef huart4;
static GPIO_TypeDef s_boot0_port;
GPIO_TypeDef *BOOT0_GPIO_Port = &s_boot0_port;

static SysTick_Type s_systick;
static NVIC_Type s_nvic;
static SCB_Type s_scb;
static RCC_TypeDef s_rcc;
SysTick_Type *SysTick = &s_systick;
NVIC_Type *NVIC = &s_nvic;
SCB_Type *SCB = &s_scb;
RCC_TypeDef *RCC = &s_rcc;

#define SECTOR_SIZE (128UL * 1024UL)
#define BANK_SIZE   (8UL * SECTOR_SIZE)

void host_hal_init(void)
{
	/* What the reset left in RCC->RSR: the supervisor says whether this boot is
	 * the first after power-on, which is what the handoff record keys off. */
	RCC->RSR = host_args.cold ? RCC_RSR_PORRSTF : RCC_RSR_SFTRSTF;
}

uint32_t HAL_GetTick(void) { return host_tick_ms(); }
void HAL_Delay(uint32_t ms) { host_sleep_ms(ms); }

void HAL_NVIC_SystemReset(void)
{
	host_log("reset");
	host_exit(HOST_EXIT_RESET);
}

uint32_t HAL_CRC_Calculate(CRC_HandleTypeDef *h, uint32_t *buf, uint32_t len)
{
	(void)h;
	/* Bytes in, both inversions on, default polynomial and init, and no final
	 * XOR: the firmware compares the IEEE CRC the tool sends with ~this. */
	const uint8_t *p = (const uint8_t *)buf;
	uint32_t crc = 0xFFFFFFFFU;
	for (uint32_t i = 0; i < len; i++) {
		crc ^= p[i];
		for (int b = 0; b < 8; b++) {
			crc = (crc >> 1) ^ (0xEDB88320U & (0U - (crc & 1U)));
		}
	}
	return crc;
}

int HAL_GPIO_ReadPin(GPIO_TypeDef *port, uint16_t pin) { (void)port; (void)pin; return GPIO_PIN_RESET; }
void HAL_MPU_Disable(void) { }
HAL_StatusTypeDef HAL_UART_DeInit(UART_HandleTypeDef *h) { (void)h; return HAL_OK; }
HAL_StatusTypeDef HAL_RCC_DeInit(void) { return HAL_OK; }
HAL_StatusTypeDef HAL_DeInit(void) { return HAL_OK; }
uint32_t HAL_GetUIDw0(void) { return host_args.uid[0]; }
uint32_t HAL_GetUIDw1(void) { return host_args.uid[1]; }
uint32_t HAL_GetUIDw2(void) { return host_args.uid[2]; }
void HAL_PWR_EnableBkUpAccess(void) { }
void HAL_PWR_DisableBkUpAccess(void) { }
HAL_StatusTypeDef HAL_PWREx_EnableBkUpReg(void) { return HAL_OK; }

HAL_StatusTypeDef HAL_RNG_GenerateRandomNumber(RNG_HandleTypeDef *h, uint32_t *out)
{
	(void)h;
#ifdef _WIN32
	if (BCryptGenRandom(NULL, (PUCHAR)out, sizeof(*out), BCRYPT_USE_SYSTEM_PREFERRED_RNG) != 0) {
		return HAL_ERROR;
	}
#else
	FILE *f = fopen("/dev/urandom", "rb");
	if ((f == NULL) || (fread(out, sizeof(*out), 1, f) != 1)) {
		if (f != NULL) fclose(f);
		return HAL_ERROR;
	}
	fclose(f);
#endif
	return HAL_OK;
}

uint32_t HAL_RNG_GetError(RNG_HandleTypeDef *h) { (void)h; return 0U; }
uint32_t HAL_RTCEx_BKUPRead(RTC_HandleTypeDef *h, uint32_t reg) { (void)h; return hostmem_rtc_bkp[reg & 31U]; }
void HAL_RTCEx_BKUPWrite(RTC_HandleTypeDef *h, uint32_t reg, uint32_t v) { (void)h; hostmem_rtc_bkp[reg & 31U] = v; }

HAL_StatusTypeDef HAL_FLASH_Unlock(void) { return HAL_OK; }
HAL_StatusTypeDef HAL_FLASH_Lock(void) { return HAL_OK; }

/* An injected power cut right after the Nth erase / program of this boot. The
 * flash is a mapped file, so what was written so far stays, as on the chip. */
static void maybe_cut(uint32_t *count, uint32_t limit, const char *what)
{
	if ((limit != 0U) && (++*count == limit)) {
		host_log("power cut after %s #%u", what, (unsigned)limit);
		host_exit(HOST_EXIT_POWER);
	}
}

static uint32_t s_erases;
static uint32_t s_programs;

HAL_StatusTypeDef HAL_FLASHEx_Erase(FLASH_EraseInitTypeDef *init, uint32_t *sector_error)
{
	uint32_t base = HOST_FLASH_BASE + (init->Banks == FLASH_BANK_2 ? BANK_SIZE : 0U);
	*sector_error = 0xFFFFFFFFU;
	if (init->Sector + init->NbSectors > FLASH_SECTOR_TOTAL) {
		*sector_error = init->Sector;
		return HAL_ERROR;
	}
	memset((void *)(uintptr_t)(base + init->Sector * SECTOR_SIZE), 0xFF, init->NbSectors * SECTOR_SIZE);
	maybe_cut(&s_erases, host_args.fail_after_erase, "erase");
	return HAL_OK;
}

HAL_StatusTypeDef HAL_FLASH_Program(uint32_t type, uint32_t address, uint32_t data_address)
{
	(void)type;
	uint8_t *dst = (uint8_t *)(uintptr_t)address;
	if (((address % 32U) != 0U) || (address < HOST_FLASH_BASE)
			|| (address + 32U > HOST_FLASH_BASE + HOST_FLASH_SIZE)) {
		return HAL_ERROR;
	}
	for (int i = 0; i < 32; i++) {
		if (dst[i] != 0xFFU) {
			return HAL_ERROR;   /* a flash word programs once per erase */
		}
	}
	memcpy(dst, (const void *)(uintptr_t)data_address, 32U);
	maybe_cut(&s_programs, host_args.fail_after_program, "program");
	return HAL_OK;
}

void SCB_DisableICache(void) { }
void SCB_EnableICache(void) { }
void SCB_CleanDCache_by_Addr(uint32_t *addr, int32_t size) { (void)addr; (void)size; }
void SCB_InvalidateDCache_by_Addr(uint32_t *addr, int32_t size) { (void)addr; (void)size; }
void __disable_irq(void) { }

