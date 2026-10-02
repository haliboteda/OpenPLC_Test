#include "hostctl.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
/* A crash in firmware code would otherwise end the process without a word. */
static LONG WINAPI on_crash(EXCEPTION_POINTERS *e)
{
	/* As an offset into the image, so nm on the .exe can name it. */
	fprintf(stderr, "[stand-in] crash 0x%08lx at image+0x%llx, address %p\n",
			(unsigned long)e->ExceptionRecord->ExceptionCode,
			(unsigned long long)((char *)e->ExceptionRecord->ExceptionAddress - (char *)GetModuleHandle(NULL)),
			(e->ExceptionRecord->NumberParameters > 1) ? (void *)e->ExceptionRecord->ExceptionInformation[1] : NULL);
	fflush(stderr);
	return EXCEPTION_EXECUTE_HANDLER;
}
#else
#include <time.h>
#include <unistd.h>
#endif

host_args_t host_args = { ".", 61865, 0, { 0x39333639U, 0x31325110U, 0x00330034U }, false, false, NULL };

static void usage(void)
{
	fprintf(stderr, "usage: --state DIR [--port N] [--discovery-port N] [--uid HEX24] [--cold]"
			" [--claim HEX128]\n");
	exit(2);
}

bool host_hex_decode(const char *hex, uint8_t *out, uint32_t len)
{
	if (strlen(hex) != (size_t)len * 2U) {
		return false;
	}
	for (uint32_t i = 0; i < len; i++) {
		unsigned int b;
		if (sscanf(&hex[i * 2U], "%2x", &b) != 1) {
			return false;
		}
		out[i] = (uint8_t)b;
	}
	return true;
}

void host_parse_args(int argc, char **argv)
{
#ifdef _WIN32
	SetUnhandledExceptionFilter(on_crash);
#endif
	for (int i = 1; i < argc; i++) {
		const char *a = argv[i];
		const char *v = (i + 1 < argc) ? argv[i + 1] : NULL;
		if (strcmp(a, "--state") == 0 && v) {
			host_args.state_dir = v; i++;
		} else if (strcmp(a, "--port") == 0 && v) {
			host_args.port = (uint16_t)atoi(v); i++;
		} else if (strcmp(a, "--discovery-port") == 0 && v) {
			host_args.discovery_port = (uint16_t)atoi(v); i++;
		} else if (strcmp(a, "--uid") == 0 && v) {
			/* As iap_keyderive prints it: w2, w1, w0. */
			uint8_t b[12];
			if (!host_hex_decode(v, b, 12U)) {
				usage();
			}
			for (int w = 0; w < 3; w++) {
				uint32_t x = ((uint32_t)b[w * 4] << 24) | ((uint32_t)b[w * 4 + 1] << 16)
						| ((uint32_t)b[w * 4 + 2] << 8) | (uint32_t)b[w * 4 + 3];
				host_args.uid[2 - w] = x;
			}
			i++;
		} else if (strcmp(a, "--cold") == 0) {
			host_args.cold = true;
		} else if (strcmp(a, "--claim") == 0 && v) {
			host_args.claim_hex = v; i++;
		} else {
			usage();
		}
	}
}

uint32_t host_tick_ms(void)
{
#ifdef _WIN32
	static ULONGLONG t0;
	if (t0 == 0U) {
		t0 = GetTickCount64();
	}
	return (uint32_t)(GetTickCount64() - t0);
#else
	static struct timespec t0;
	struct timespec t;
	if (t0.tv_sec == 0) {
		clock_gettime(CLOCK_MONOTONIC, &t0);
	}
	clock_gettime(CLOCK_MONOTONIC, &t);
	return (uint32_t)((t.tv_sec - t0.tv_sec) * 1000 + (t.tv_nsec - t0.tv_nsec) / 1000000);
#endif
}

void host_sleep_ms(uint32_t ms)
{
#ifdef _WIN32
	Sleep(ms);
#else
	usleep(ms * 1000U);
#endif
}

void host_exit(int code)
{
	fflush(stdout);
	fflush(stderr);
	exit(code);
}

void host_log(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	fputs("[stand-in] ", stdout);
	vfprintf(stdout, fmt, ap);
	fputs("\n", stdout);
	va_end(ap);
	fflush(stdout);
}
