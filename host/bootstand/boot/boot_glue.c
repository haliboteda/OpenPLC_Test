/*
 * Bootloader-only pieces around the real IAPServer sources: the parts of the
 * board the stand-in does not model, and the end of a boot that jumps to the
 * application.
 */

#include "main.h"
#include "boot_selfupgrade.h"
#include "fmc.h"
#include "usart.h"
#include "usbd_cdc_if.h"
#include "hostctl.h"

void Disable_RX_RS232(void) { }
bool iap_sdram_selftest_passed(void) { return true; }
uint8_t CDC_Transmit_FS(uint8_t *buf, uint16_t len) { (void)buf; (void)len; return USBD_OK; }

/* flashboot rewrites sector 0 while running from it; T1-18 and T1-34 never
 * send one, so the stand-in refuses rather than emulate the copy-and-reset. */
uint32_t boot_selfupgrade_max_size(void) { return 128U * 1024U; }
bool boot_selfupgrade_commit(uint32_t image_size)
{
	(void)image_size;
	host_log("flashboot is not emulated");
	return false;
}

void iap_server_host_jump_to_app(uint32_t msp, uint32_t reset_vector)
{
	(void)msp;
	(void)reset_vector;
	host_log("jump to app");
	host_exit(HOST_EXIT_APP);
}

/* The PC has no DO / AO pins to drive to 0 (decision 81, $BOOT/IAPServer/safe_outputs.c). */
void safe_outputs_init(void)
{
}
