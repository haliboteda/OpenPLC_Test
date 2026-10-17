#include "hostmem.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <windows.h>

volatile uint32_t *hostmem_rtc_bkp;

static void die(const char *what, const char *name)
{
	fprintf(stderr, "[stand-in] cannot map %s (%s)\n", name, what);
	exit(2);
}

/* A file of `size` bytes holding the region; created filled with `fill`. */
static void ensure_file(const char *path, uint32_t size, uint8_t fill)
{
	FILE *f = fopen(path, "rb");
	if (f != NULL) {
		fclose(f);
		return;
	}
	f = fopen(path, "wb");
	if (f == NULL) {
		die("create", path);
	}
	uint8_t chunk[4096];
	memset(chunk, fill, sizeof(chunk));
	for (uint32_t done = 0; done < size; done += sizeof(chunk)) {
		fwrite(chunk, 1, sizeof(chunk), f);
	}
	fclose(f);
}

static void *map_file(const char *path, uintptr_t addr, uint32_t size)
{
	HANDLE fh = CreateFileA(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
			NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
	if (fh == INVALID_HANDLE_VALUE) {
		die("open", path);
	}
	HANDLE mh = CreateFileMappingA(fh, NULL, PAGE_READWRITE, 0, size, NULL);
	if (mh == NULL) {
		die("CreateFileMapping", path);
	}
	void *p = MapViewOfFileEx(mh, FILE_MAP_ALL_ACCESS, 0, 0, size, (void *)addr);
	if ((addr != 0U) && (p != (void *)addr)) {
		die("MapViewOfFileEx at the firmware address", path);
	}
	return p;
}

static void *map_anon(uintptr_t addr, uint32_t size)
{
	void *p = VirtualAlloc((void *)addr, size, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
	if (p != (void *)addr) {
		die("VirtualAlloc at the firmware address", "SDRAM");
	}
	return p;
}

static void map_region(const char *dir, const char *name, uintptr_t addr, uint32_t size, uint8_t fill,
		void **out)
{
	char path[1024];
	snprintf(path, sizeof(path), "%s/%s.bin", dir, name);
	ensure_file(path, size, fill);
	void *p = map_file(path, addr, size);
	if (out != NULL) {
		*out = p;
	}
}

void hostmem_init(const char *state_dir)
{
	void *rtc = NULL;

	/* Erased flash reads 0xFF; the RAMs start at zero. SRAM4 is wiped by the
	 * supervisor on a cold start, since only a soft reset keeps it. */
	map_region(state_dir, "flash", HOST_FLASH_BASE, HOST_FLASH_SIZE, 0xFF, NULL);
	map_region(state_dir, "sram4", HOST_SRAM4_BASE, HOST_SRAM4_SIZE, 0x00, NULL);
	map_region(state_dir, "bkpsram", HOST_BKPSRAM_BASE, HOST_BKPSRAM_SIZE, 0x00, NULL);
	map_region(state_dir, "rtc_bkp", 0U, 32U * 4U, 0x00, &rtc);
	hostmem_rtc_bkp = (volatile uint32_t *)rtc;
	(void)map_anon(HOST_SDRAM_BASE, HOST_SDRAM_SIZE);
}
