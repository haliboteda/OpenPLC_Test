/*
 * What both halves of the stand-in share: command-line state, the clock, the
 * UID, and how a reset or a jump into the application ends this process. The
 * supervisor (bootstand.py) reads the exit code and starts the next "boot".
 */

#ifndef BOOTSTAND_HOSTCTL_H_
#define BOOTSTAND_HOSTCTL_H_

#include <stdbool.h>
#include <stdint.h>

#define HOST_EXIT_RESET 3   /* HAL_NVIC_SystemReset(): boot the bootloader again */
#define HOST_EXIT_APP   4   /* the bootloader jumped to the application */

typedef struct {
	const char *state_dir;
	uint16_t port;            /* what OPENPLC_SERVER_PORT is served on */
	uint16_t discovery_port;  /* 0 = none */
	uint32_t uid[3];          /* HAL_GetUIDw0..2 */
	bool cold;                /* first boot after power-on */
	bool old_bootloader;      /* answer getpubkey with "Unknown command" (T1-18c) */
	const char *claim_hex;    /* setup only: claim this root before booting */
} host_args_t;

extern host_args_t host_args;

/* Parses argv into host_args; exits with a usage line on a bad argument. */
void host_parse_args(int argc, char **argv);
uint32_t host_tick_ms(void);
void host_sleep_ms(uint32_t ms);
/* Flushes stdout and ends this boot with the given code. */
void host_exit(int code);
/* "[stand-in] ..." on stdout, flushed: the test scripts read these lines. */
void host_log(const char *fmt, ...);
bool host_hex_decode(const char *hex, uint8_t *out, uint32_t len);

#endif /* BOOTSTAND_HOSTCTL_H_ */
