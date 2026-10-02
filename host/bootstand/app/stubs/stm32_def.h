/*
 * Stands in for the core's stm32_def.h. Takes its include guard on purpose:
 * IAP_boot_handoff.c sits next to the real one, and a quoted include always
 * looks there first, so the guard is what keeps the real header out. Passed to
 * the app build with -include.
 */
#ifndef _STM32_DEF_
#define _STM32_DEF_
#include "main.h"
#endif
