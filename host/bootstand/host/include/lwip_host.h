/*
 * The slice of the lwIP raw API that IAPServer and OpenPLC_IAP call, served by
 * PC sockets (lwip_bridge.c). Every header under lwip/ here includes only
 * this file. Signatures match lwIP 2.1.2 for the calls the firmware makes.
 */

#ifndef BOOTSTAND_LWIP_HOST_H_
#define BOOTSTAND_LWIP_HOST_H_

#include <stdint.h>
/* On the board lwipopts.h brings main.h, and the firmware leans on that for
 * HAL_GetTick(). */
#include "main.h"

typedef uint8_t u8_t;
typedef uint16_t u16_t;
typedef uint32_t u32_t;
typedef int8_t err_t;

#define ERR_OK   0
#define ERR_MEM  (-1)
#define ERR_VAL  (-6)
#define ERR_ABRT (-13)
#define ERR_RST  (-14)

#define TCP_WRITE_FLAG_COPY 0x01

typedef struct { u32_t addr; } ip4_addr_t;
typedef ip4_addr_t ip_addr_t;
#define IP_ADDR_ANY ((const ip_addr_t *)0)
#define ip_2_ip4(a) (a)
#define ip4_addr_get_u32(a) ((a)->addr)
char *ipaddr_ntoa(const ip_addr_t *addr);

typedef enum { PBUF_TRANSPORT } pbuf_layer;
typedef enum { PBUF_RAM } pbuf_type;
struct pbuf {
	struct pbuf *next;
	void *payload;
	u16_t tot_len;
	u16_t len;
};
struct pbuf *pbuf_alloc(pbuf_layer layer, u16_t length, pbuf_type type);
u8_t pbuf_free(struct pbuf *p);

struct udp_pcb;
typedef void (*udp_recv_fn)(void *arg, struct udp_pcb *pcb, struct pbuf *p, const ip_addr_t *addr, u16_t port);
struct udp_pcb *udp_new(void);
err_t udp_bind(struct udp_pcb *pcb, const ip_addr_t *ipaddr, u16_t port);
void udp_recv(struct udp_pcb *pcb, udp_recv_fn recv, void *recv_arg);
err_t udp_sendto(struct udp_pcb *pcb, struct pbuf *p, const ip_addr_t *dst_ip, u16_t dst_port);
void udp_disconnect(struct udp_pcb *pcb);
void udp_remove(struct udp_pcb *pcb);

struct tcp_pcb_host;
struct tcp_pcb {
	ip_addr_t remote_ip;
	struct tcp_pcb_host *host;
};
typedef err_t (*tcp_recv_fn)(void *arg, struct tcp_pcb *tpcb, struct pbuf *p, err_t err);
typedef err_t (*tcp_accept_fn)(void *arg, struct tcp_pcb *newpcb, err_t err);
typedef err_t (*tcp_sent_fn)(void *arg, struct tcp_pcb *tpcb, u16_t len);
typedef err_t (*tcp_poll_fn)(void *arg, struct tcp_pcb *tpcb);
typedef void (*tcp_err_fn)(void *arg, err_t err);
struct tcp_pcb *tcp_new(void);
err_t tcp_bind(struct tcp_pcb *pcb, const ip_addr_t *ipaddr, u16_t port);
struct tcp_pcb *tcp_listen(struct tcp_pcb *pcb);
void tcp_accept(struct tcp_pcb *pcb, tcp_accept_fn accept);
void tcp_arg(struct tcp_pcb *pcb, void *arg);
void tcp_recv(struct tcp_pcb *pcb, tcp_recv_fn recv);
void tcp_sent(struct tcp_pcb *pcb, tcp_sent_fn sent);
void tcp_err(struct tcp_pcb *pcb, tcp_err_fn err);
void tcp_poll(struct tcp_pcb *pcb, tcp_poll_fn poll, u8_t interval);
err_t tcp_close(struct tcp_pcb *pcb);
void tcp_abort(struct tcp_pcb *pcb);
err_t tcp_write(struct tcp_pcb *pcb, const void *dataptr, u16_t len, u8_t apiflags);
err_t tcp_output(struct tcp_pcb *pcb);
void tcp_recved(struct tcp_pcb *pcb, u16_t len);

struct netif {
	int link_up;
};
extern struct netif *netif_default;
extern struct netif gnetif;
#define netif_is_link_up(n) ((n)->link_up)

/* --- the stand-in's side ---------------------------------------------------- */

/* The firmware binds fw_port (OPENPLC_SERVER_PORT); the bridge serves it on
 * real_port instead, and, when discovery_port is not 0, also answers UDP on
 * that port through the same pcb. */
void bridge_init(u16_t fw_port, u16_t real_port, u16_t discovery_port);
/* Waits up to timeout_ms for network events and runs the firmware callbacks
 * they trigger, plus the tcp_poll timers. */
void bridge_poll(int timeout_ms);
/* A hook that sees every TCP segment before the firmware does; returns 1 when
 * it consumed the segment (and answered through bridge_tcp_reply). */
typedef int (*bridge_tcp_filter_fn)(struct tcp_pcb *pcb, const uint8_t *data, int len);
void bridge_set_tcp_filter(bridge_tcp_filter_fn fn);
void bridge_tcp_reply(struct tcp_pcb *pcb, const char *msg);

#endif /* BOOTSTAND_LWIP_HOST_H_ */
