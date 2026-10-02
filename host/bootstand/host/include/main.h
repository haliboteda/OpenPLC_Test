/*
 * Stands in for Core/Inc/main.h and the HAL behind it: only what IAPServer and
 * the real flash driver (Core/Src/usbd_cdc_flash.c) call. Behaviour is in
 * hal_boot.c.
 */
#ifndef BOOTSTAND_MAIN_H
#define BOOTSTAND_MAIN_H

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define __IO volatile
#define FLASH_BASE 0x08000000UL

typedef enum { HAL_OK = 0, HAL_ERROR = 1, HAL_BUSY = 2, HAL_TIMEOUT = 3 } HAL_StatusTypeDef;

typedef struct { int dummy; } CRC_HandleTypeDef;
typedef struct { int dummy; } UART_HandleTypeDef;
typedef struct { int dummy; } GPIO_TypeDef;
typedef struct { int dummy; } RNG_HandleTypeDef;
typedef struct { int dummy; } RTC_HandleTypeDef;

#define GPIO_PIN_RESET 0
#define GPIO_PIN_SET   1
extern GPIO_TypeDef *BOOT0_GPIO_Port;
#define BOOT0_Pin 0

uint32_t HAL_GetTick(void);
void HAL_Delay(uint32_t ms);
void HAL_NVIC_SystemReset(void);
uint32_t HAL_CRC_Calculate(CRC_HandleTypeDef *h, uint32_t *buf, uint32_t len);
int HAL_GPIO_ReadPin(GPIO_TypeDef *port, uint16_t pin);
void HAL_MPU_Disable(void);
HAL_StatusTypeDef HAL_UART_DeInit(UART_HandleTypeDef *h);
HAL_StatusTypeDef HAL_RCC_DeInit(void);
HAL_StatusTypeDef HAL_DeInit(void);
uint32_t HAL_GetUIDw0(void);
uint32_t HAL_GetUIDw1(void);
uint32_t HAL_GetUIDw2(void);
void HAL_PWR_EnableBkUpAccess(void);
void HAL_PWR_DisableBkUpAccess(void);
HAL_StatusTypeDef HAL_PWREx_EnableBkUpReg(void);
HAL_StatusTypeDef HAL_RNG_GenerateRandomNumber(RNG_HandleTypeDef *h, uint32_t *out);
uint32_t HAL_RNG_GetError(RNG_HandleTypeDef *h);
uint32_t HAL_RTCEx_BKUPRead(RTC_HandleTypeDef *h, uint32_t reg);
void HAL_RTCEx_BKUPWrite(RTC_HandleTypeDef *h, uint32_t reg, uint32_t value);

/* Flash, for usbd_cdc_flash.c. */
#define FLASH_TYPEERASE_SECTORS     0U
#define FLASH_TYPEPROGRAM_FLASHWORD 0U
#define FLASH_VOLTAGE_RANGE_3       0U
#define FLASH_BANK_1                1U
#define FLASH_BANK_2                2U
#define FLASH_SECTOR_TOTAL          8U
#define FLASH_SECTOR_0 0U
#define FLASH_SECTOR_1 1U
#define FLASH_SECTOR_2 2U
#define FLASH_SECTOR_3 3U
#define FLASH_SECTOR_4 4U
#define FLASH_SECTOR_5 5U
#define FLASH_SECTOR_6 6U
#define FLASH_SECTOR_7 7U
typedef struct {
	uint32_t TypeErase;
	uint32_t Banks;
	uint32_t Sector;
	uint32_t NbSectors;
	uint32_t VoltageRange;
} FLASH_EraseInitTypeDef;
HAL_StatusTypeDef HAL_FLASH_Unlock(void);
HAL_StatusTypeDef HAL_FLASH_Lock(void);
HAL_StatusTypeDef HAL_FLASHEx_Erase(FLASH_EraseInitTypeDef *init, uint32_t *sector_error);
HAL_StatusTypeDef HAL_FLASH_Program(uint32_t type, uint32_t address, uint32_t data_address);

/* Core and reset bits. */
void SCB_DisableICache(void);
void SCB_EnableICache(void);
void SCB_CleanDCache_by_Addr(uint32_t *addr, int32_t size);
void SCB_InvalidateDCache_by_Addr(uint32_t *addr, int32_t size);
void __disable_irq(void);
#define __DSB() ((void)0)
#define __ISB() ((void)0)
#define __HAL_RCC_BKPRAM_CLK_ENABLE() ((void)0)
#define D3_BKPSRAM_BASE 0x38800000UL

typedef struct { volatile uint32_t CTRL, LOAD, VAL; } SysTick_Type;
typedef struct { volatile uint32_t ICER[8], ICPR[8]; } NVIC_Type;
typedef struct { volatile uint32_t VTOR; } SCB_Type;
typedef struct { volatile uint32_t RSR; } RCC_TypeDef;
extern SysTick_Type *SysTick;
extern NVIC_Type *NVIC;
extern SCB_Type *SCB;
extern RCC_TypeDef *RCC;
#define RCC_RSR_RMVF      (1UL << 16)
#define RCC_RSR_BORRSTF   (1UL << 21)
#define RCC_RSR_PINRSTF   (1UL << 22)
#define RCC_RSR_PORRSTF   (1UL << 23)
#define RCC_RSR_SFTRSTF   (1UL << 24)
#define RCC_RSR_IWDG1RSTF (1UL << 26)
#define RCC_RSR_WWDG1RSTF (1UL << 28)
#define RCC_RSR_D2RSTF    (1UL << 20)
#define __HAL_RCC_CLEAR_RESET_FLAGS() (RCC->RSR = 0U)

extern CRC_HandleTypeDef hcrc;
extern RNG_HandleTypeDef hrng;
extern RTC_HandleTypeDef hrtc;
extern UART_HandleTypeDef huart4;

#endif
