/*
 * One boot of the bootloader half: the same calls, in the same order, as the
 * bootloader path of $BOOT/Core/Src/main.c, with the boot window's BOOT0 gesture
 * taken from --gesture. Ends by exiting: HOST_EXIT_RESET when the firmware resets,
 * HOST_EXIT_APP when it would jump to the application.
 */

#include "IAP_boot_handoff.h"
#include "IAP_server.h"
#include "bootloader_state.h"
#include "iap_auth.h"
#include "net_rand.h"
#include "owner_slot.h"
#include "lwip_host.h"
#include "hostctl.h"
#include "hostmem.h"

#include <stdio.h>
#include <string.h>

void host_hal_init(void);

/* IAP_server.c's receive state, read so the filter only ever replaces a whole
 * command line, never a slice of an image being received. */
extern volatile IAP_STATUS current_status;
extern volatile uint32_t len_in_RX_buffer;

static void print_root(void)
{
	static bool printed;
	static uint8_t last[64];
	const uint8_t *root = owner_slot_root();
	uint8_t now[64];

	memset(now, 0, sizeof(now));
	if (root != NULL) {
		memcpy(now, root, sizeof(now));
	}
	if (printed && (memcmp(now, last, sizeof(now)) == 0)) {
		return;
	}
	printed = true;
	memcpy(last, now, sizeof(last));
	if (root == NULL) {
		host_log("root none");
		return;
	}
	char hex[129];
	for (int i = 0; i < 64; i++) {
		snprintf(&hex[i * 2], 3, "%02x", root[i]);
	}
	host_log("root %s", hex);
}

int main(int argc, char **argv)
{
	setvbuf(stdout, NULL, _IONBF, 0);
	host_parse_args(argc, argv);
	hostmem_init(host_args.state_dir);
	host_hal_init();
	bridge_init(OPENPLC_SERVER_PORT, host_args.port, host_args.discovery_port);

	printf("** Reset cause: %s\r\n", boot_handoff_reset_cause_str());
	bootloader_state_init();

	if (host_args.claim_hex != NULL) {
		/* Test setup: the state a takeown would leave, without a tool. */
		uint8_t key[64];
		if (!host_hex_decode(host_args.claim_hex, key, sizeof(key)) || !owner_slot_claim(key)) {
			host_log("setup claim refused");
			host_exit(2);
		}
	}

	/* The boot window, as $BOOT/Core/Src/main.c handles its gesture: a factory
	 * reset before server_decide() scans the owner slot, and either gesture
	 * keeps the board in the bootloader. */
	if (host_args.gesture == HOST_GESTURE_FACTORY) {
		(void)owner_slot_factory_reset(true);
	}
	IAP_Method mode = server_decide((host_args.gesture == HOST_GESTURE_NONE) ? 0U : 1U);
	print_root();
	if (mode == IAP_NONE) {
		server_jump_to_app();   /* exits with HOST_EXIT_APP */
	}

	net_rand_seed();
	iap_auth_report_backup_domain();
	IAP_servers_start(mode);
	host_log("bootloader serving on %u", (unsigned)host_args.port);

	for (;;) {
		IAP_task();
		bridge_poll(10);
		print_root();
	}
}
