/*
 * The application half: what the core's main.cpp brings up for IAP before the
 * sketch runs, i.e. the OpenPLC_IAP UDP server that answers discovery and the
 * authenticated reboot request. No sketch: what was flashed is only checked by
 * the bootloader half, never executed. An accepted reboot request stores the
 * ethernet handoff record and resets (HOST_EXIT_RESET).
 */

#include "IAP_config.h"
#include "OpenPLC_IAP_Autostart.h"
#include "openplc_app_version.h"
#include "openplc_rng.h"
#include "main.h"
#include "lwip_host.h"
#include "hostctl.h"
#include "hostmem.h"

#include <stdio.h>

void host_hal_init(void);

/* The sketch defines this with OPENPLC_APP_VERSION(); here the build does. */
const char openplc_app_version[] = BOOTSTAND_APP_VERSION;

bool openplc_rng_words(uint32_t *words, uint32_t n)
{
	for (uint32_t i = 0; i < n; i++) {
		if (HAL_RNG_GenerateRandomNumber(&hrng, &words[i]) != HAL_OK) {
			return false;
		}
	}
	return true;
}

int main(int argc, char **argv)
{
	setvbuf(stdout, NULL, _IONBF, 0);
	host_parse_args(argc, argv);
	hostmem_init(host_args.state_dir);
	host_hal_init();
	bridge_init(OPENPLC_SERVER_PORT, host_args.port, host_args.discovery_port);

	openplc_udp_server_start(NULL);
	host_log("app %s serving on %u", openplc_app_version, (unsigned)host_args.port);
	for (;;) {
		bridge_poll(10);
	}
}
