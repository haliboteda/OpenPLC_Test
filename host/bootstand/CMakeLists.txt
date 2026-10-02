# The bootloader stand-in: the real $BOOT/IAPServer sources and the real
# OpenPLC_IAP app-side reboot handshake, built for the PC, for IAPTool's
# contract cases T1-18 and T1-34. Design: $PROD/docs/engineering/BOOTLOADER-STAND-IN.md
#
# Two programs, because the bootloader and the board package carry their own
# copies of the same functions (iap_auth, iap_cert, sha256, uECC) and one link
# cannot hold both. bootstand.py runs them in turn, as resets would.
cmake_minimum_required(VERSION 3.20)
project(bootstand C)

set(BOOT_ROOT "" CACHE PATH "the bootloader repo (open_plc_cube_ide)")
set(CORE_ROOT "" CACHE PATH "the board package repo (open_plc_arduino)")
# openplc_app_version the application half reports. 0.0.1: older than any
# sketch, so IAPTool's version gate never refuses. Not a cache entry, so a build
# directory cannot keep an old default; -DAPP_VERSION=... still overrides.
if(NOT DEFINED APP_VERSION)
  set(APP_VERSION "0.0.1")
endif()
set(FW_VERSION "0.1.3" CACHE STRING "OPENPLC_FW_VERSION, as boards.txt sets it")
if(NOT EXISTS "${BOOT_ROOT}/IAPServer/IAP_server.c")
  message(FATAL_ERROR "BOOT_ROOT is not the bootloader repo: '${BOOT_ROOT}'")
endif()
if(NOT EXISTS "${CORE_ROOT}/libraries/OpenPLC_IAP/src/udp_server.c")
  message(FATAL_ERROR "CORE_ROOT is not the board package repo: '${CORE_ROOT}'")
endif()

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
# -O2: the bootloader's boot-time crypto self-test is slow unoptimised, and a
# reset has to come back within IAPTool's reboot wait.
add_compile_options(-Wall -O2 -g)

set(HOST ${CMAKE_CURRENT_SOURCE_DIR}/host)
set(HOST_SRC ${HOST}/hostmem.c ${HOST}/hostctl.c ${HOST}/lwip_bridge.c ${HOST}/hal_host.c)
if(WIN32)
  set(HOST_LIBS ws2_32 bcrypt)
endif()
if(MINGW)
  # No MinGW runtime DLL lookup at run time: the test scripts start these with
  # whatever PATH they have.
  add_link_options(-static)
  # The firmware is 32-bit and passes buffer addresses as uint32_t (the flash
  # driver's HAL_FLASH_Program call, for one). Without high-entropy ASLR and with
  # a low image base, the stack, heap and image all sit below 4 GiB, clear of the
  # board addresses hostmem.c maps.
  add_link_options(-Wl,--disable-high-entropy-va,--disable-dynamicbase,--image-base=0x60000000)
endif()

# --- bootloader half ------------------------------------------------------------
set(IAP ${BOOT_ROOT}/IAPServer)
add_executable(bootstand_boot
  ${HOST_SRC}
  boot/boot_glue.c boot/main_boot.c
  ${IAP}/IAP_server.c ${IAP}/tcp_server.c ${IAP}/udp_server.c
  ${IAP}/bootloader_state.c ${IAP}/bkp_stash.c ${IAP}/owner_slot.c
  ${IAP}/iap_auth.c ${IAP}/iap_cert.c ${IAP}/iap_keyderive.c ${IAP}/fw_verify.c
  ${IAP}/sha256.c ${IAP}/net_rand.c ${IAP}/uecc/uECC.c ${IAP}/IAP_boot_handoff.c
  ${BOOT_ROOT}/Core/Src/usbd_cdc_flash.c)
# Stubs first, so they shadow the HAL-backed headers in Core/Inc.
target_include_directories(bootstand_boot PRIVATE
  ${HOST} ${HOST}/include boot/stubs ${IAP} ${BOOT_ROOT}/Core/Inc)
target_compile_definitions(bootstand_boot PRIVATE IAP_SERVER_HOST_TEST=1)
target_link_libraries(bootstand_boot PRIVATE ${HOST_LIBS})

# --- application half -----------------------------------------------------------
set(OIAP ${CORE_ROOT}/libraries/OpenPLC_IAP/src)
add_executable(bootstand_app
  ${HOST_SRC}
  app/main_app.c
  ${OIAP}/udp_server.c ${OIAP}/iap_auth.c ${OIAP}/iap_cert.c ${OIAP}/iap_keyderive.c
  ${OIAP}/owner_root_ro.c ${OIAP}/fw_verify.c ${OIAP}/sha256.c ${OIAP}/uecc/uECC.c
  ${CORE_ROOT}/cores/arduino/stm32/IAP_boot_handoff.c)
target_include_directories(bootstand_app PRIVATE
  ${HOST} ${HOST}/include app/stubs ${OIAP}
  ${CORE_ROOT}/libraries/OpenPLC_Net/src ${CORE_ROOT}/cores/arduino/stm32 ${CORE_ROOT}/cores/arduino)
target_compile_definitions(bootstand_app PRIVATE
  OPENPLC_FW_VERSION="${FW_VERSION}" BOOTSTAND_APP_VERSION="${APP_VERSION}")
# The real stm32_def.h sits beside IAP_boot_handoff.c; taking its guard first
# keeps it out (see app/stubs/stm32_def.h).
target_compile_options(bootstand_app PRIVATE -include ${CMAKE_CURRENT_SOURCE_DIR}/app/stubs/stm32_def.h)
target_link_libraries(bootstand_app PRIVATE ${HOST_LIBS})
