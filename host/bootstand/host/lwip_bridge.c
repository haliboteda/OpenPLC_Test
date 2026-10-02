/*
 * lwIP raw API over PC sockets, single-threaded like the board's superloop:
 * nothing runs a firmware callback except bridge_poll(), so the firmware sees
 * the same "callbacks between IAP_task() calls" world it does on hardware.
 */

#include "lwip_host.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
typedef SOCKET sock_t;
#define BAD_SOCK INVALID_SOCKET
#define close_sock closesocket
static uint64_t now_ms(void) { return GetTickCount64(); }
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
typedef int sock_t;
#define BAD_SOCK (-1)
#define close_sock close
static uint64_t now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000U + (uint64_t)ts.tv_nsec / 1000000U;
}
#endif

/* One TCP segment, as lwIP hands them over: a command longer than this arrives
 * in pieces, which IAP_data_recv() has to reassemble on the board too. */
#define SEGMENT 1460
#define MAX_UDP 4
#define MAX_TCP 16
/* lwIP's slow timer; tcp_poll intervals count in these. */
#define TCP_SLOW_MS 500U

struct netif gnetif = { 1 };
struct netif *netif_default = &gnetif;

static u16_t s_fw_port, s_real_port, s_disc_port;
static bridge_tcp_filter_fn s_filter;

struct udp_pcb {
	int used;
	udp_recv_fn fn;
	void *arg;
	sock_t s[2];
	int ns;
	sock_t reply_via;   /* answer from the socket the request came in on */
};
static struct udp_pcb s_udp[MAX_UDP];

enum { T_FREE, T_NEW, T_LISTEN, T_CLIENT };
struct tcp_pcb_host {
	int kind;
	int dead;           /* closed by the firmware; freed after the callback returns */
	sock_t s;
	struct tcp_pcb pub;
	void *arg;
	tcp_accept_fn accept;
	tcp_recv_fn recv;
	tcp_err_fn err;
	tcp_poll_fn poll;
	u8_t poll_interval;
	uint64_t next_poll;
	u16_t bind_port;
};
static struct tcp_pcb_host s_tcp[MAX_TCP];

static u16_t real_port(u16_t fw)
{
	return (fw == s_fw_port) ? s_real_port : fw;
}

void bridge_init(u16_t fw_port, u16_t real, u16_t discovery_port)
{
#ifdef _WIN32
	WSADATA wsa;
	WSAStartup(MAKEWORD(2, 2), &wsa);
#endif
	s_fw_port = fw_port;
	s_real_port = real;
	s_disc_port = discovery_port;
}

void bridge_set_tcp_filter(bridge_tcp_filter_fn fn)
{
	s_filter = fn;
}

char *ipaddr_ntoa(const ip_addr_t *addr)
{
	static char buf[24];
	struct in_addr a;
	a.s_addr = (addr != NULL) ? addr->addr : 0U;
	snprintf(buf, sizeof(buf), "%s", inet_ntoa(a));
	return buf;
}

struct pbuf *pbuf_alloc(pbuf_layer layer, u16_t length, pbuf_type type)
{
	(void)layer;
	(void)type;
	struct pbuf *p = calloc(1, sizeof(*p) + length + 1U);
	if (p == NULL) {
		return NULL;
	}
	p->payload = (uint8_t *)(p + 1);
	p->len = length;
	p->tot_len = length;
	return p;
}

u8_t pbuf_free(struct pbuf *p)
{
	free(p);
	return 1;
}

/* --- UDP ---------------------------------------------------------------------- */

static sock_t udp_socket_on(u16_t port)
{
	sock_t s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
	struct sockaddr_in sa;
	memset(&sa, 0, sizeof(sa));
	sa.sin_family = AF_INET;
	sa.sin_port = htons(port);
	sa.sin_addr.s_addr = htonl(INADDR_ANY);
	if ((s == BAD_SOCK) || (bind(s, (struct sockaddr *)&sa, sizeof(sa)) != 0)) {
		fprintf(stderr, "[stand-in] UDP bind %u failed\n", (unsigned)port);
		if (s != BAD_SOCK) {
			close_sock(s);
		}
		return BAD_SOCK;
	}
	return s;
}

struct udp_pcb *udp_new(void)
{
	for (int i = 0; i < MAX_UDP; i++) {
		if (!s_udp[i].used) {
			memset(&s_udp[i], 0, sizeof(s_udp[i]));
			s_udp[i].used = 1;
			return &s_udp[i];
		}
	}
	return NULL;
}

err_t udp_bind(struct udp_pcb *pcb, const ip_addr_t *ipaddr, u16_t port)
{
	(void)ipaddr;
	sock_t s = udp_socket_on(real_port(port));
	if (s == BAD_SOCK) {
		return ERR_VAL;
	}
	pcb->s[0] = s;
	pcb->ns = 1;
	pcb->reply_via = s;
	if ((port == s_fw_port) && (s_disc_port != 0U) && (s_disc_port != s_real_port)) {
		sock_t d = udp_socket_on(s_disc_port);
		if (d != BAD_SOCK) {
			pcb->s[pcb->ns++] = d;
		}
	}
	return ERR_OK;
}

void udp_recv(struct udp_pcb *pcb, udp_recv_fn recv, void *recv_arg)
{
	pcb->fn = recv;
	pcb->arg = recv_arg;
}

err_t udp_sendto(struct udp_pcb *pcb, struct pbuf *p, const ip_addr_t *dst_ip, u16_t dst_port)
{
	struct sockaddr_in sa;
	memset(&sa, 0, sizeof(sa));
	sa.sin_family = AF_INET;
	sa.sin_port = htons(dst_port);
	sa.sin_addr.s_addr = dst_ip->addr;
	int n = sendto(pcb->reply_via, (const char *)p->payload, p->len, 0, (struct sockaddr *)&sa, sizeof(sa));
	return (n == (int)p->len) ? ERR_OK : ERR_VAL;
}

void udp_disconnect(struct udp_pcb *pcb)
{
	(void)pcb;
}

void udp_remove(struct udp_pcb *pcb)
{
	for (int i = 0; i < pcb->ns; i++) {
		close_sock(pcb->s[i]);
	}
	pcb->used = 0;
}

/* --- TCP ---------------------------------------------------------------------- */

static struct tcp_pcb_host *host_of(struct tcp_pcb *pcb)
{
	return pcb->host;
}

static struct tcp_pcb_host *tcp_alloc(void)
{
	for (int i = 0; i < MAX_TCP; i++) {
		if (s_tcp[i].kind == T_FREE) {
			memset(&s_tcp[i], 0, sizeof(s_tcp[i]));
			s_tcp[i].kind = T_NEW;
			s_tcp[i].s = BAD_SOCK;
			s_tcp[i].pub.host = &s_tcp[i];
			return &s_tcp[i];
		}
	}
	return NULL;
}

static void tcp_kill(struct tcp_pcb_host *h, int reset)
{
	if (h->s != BAD_SOCK) {
		if (reset) {
			struct linger lg = { 1, 0 };
			setsockopt(h->s, SOL_SOCKET, SO_LINGER, (const char *)&lg, sizeof(lg));
		}
		close_sock(h->s);
		h->s = BAD_SOCK;
	}
	h->dead = 1;
}

struct tcp_pcb *tcp_new(void)
{
	struct tcp_pcb_host *h = tcp_alloc();
	return (h != NULL) ? &h->pub : NULL;
}

err_t tcp_bind(struct tcp_pcb *pcb, const ip_addr_t *ipaddr, u16_t port)
{
	(void)ipaddr;
	host_of(pcb)->bind_port = real_port(port);
	return ERR_OK;
}

struct tcp_pcb *tcp_listen(struct tcp_pcb *pcb)
{
	struct tcp_pcb_host *h = host_of(pcb);
	struct sockaddr_in sa;
	int one = 1;

	h->s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
	/* As fake_board.py did: a restart right after a session must not wait out
	 * TIME_WAIT on the port. */
	setsockopt(h->s, SOL_SOCKET, SO_REUSEADDR, (const char *)&one, sizeof(one));
	memset(&sa, 0, sizeof(sa));
	sa.sin_family = AF_INET;
	sa.sin_port = htons(h->bind_port);
	sa.sin_addr.s_addr = htonl(INADDR_ANY);
	if ((bind(h->s, (struct sockaddr *)&sa, sizeof(sa)) != 0) || (listen(h->s, 4) != 0)) {
		fprintf(stderr, "[stand-in] TCP listen %u failed\n", (unsigned)h->bind_port);
		return NULL;
	}
	h->kind = T_LISTEN;
	return pcb;
}

void tcp_accept(struct tcp_pcb *pcb, tcp_accept_fn accept) { host_of(pcb)->accept = accept; }
void tcp_arg(struct tcp_pcb *pcb, void *arg) { host_of(pcb)->arg = arg; }
void tcp_recv(struct tcp_pcb *pcb, tcp_recv_fn recv) { host_of(pcb)->recv = recv; }
void tcp_sent(struct tcp_pcb *pcb, tcp_sent_fn sent) { (void)pcb; (void)sent; }
void tcp_err(struct tcp_pcb *pcb, tcp_err_fn err) { host_of(pcb)->err = err; }

void tcp_poll(struct tcp_pcb *pcb, tcp_poll_fn poll, u8_t interval)
{
	struct tcp_pcb_host *h = host_of(pcb);
	h->poll = poll;
	h->poll_interval = interval;
	h->next_poll = now_ms() + (uint64_t)interval * TCP_SLOW_MS;
}

err_t tcp_close(struct tcp_pcb *pcb)
{
	tcp_kill(host_of(pcb), 0);
	return ERR_OK;
}

void tcp_abort(struct tcp_pcb *pcb)
{
	tcp_kill(host_of(pcb), 1);
}

err_t tcp_write(struct tcp_pcb *pcb, const void *dataptr, u16_t len, u8_t apiflags)
{
	(void)apiflags;
	struct tcp_pcb_host *h = host_of(pcb);
	if (h->dead || (h->s == BAD_SOCK)) {
		return ERR_VAL;
	}
	const char *p = (const char *)dataptr;
	int left = len;
	while (left > 0) {
		int n = send(h->s, p, left, 0);
		if (n <= 0) {
			return ERR_VAL;
		}
		p += n;
		left -= n;
	}
	return ERR_OK;
}

err_t tcp_output(struct tcp_pcb *pcb) { (void)pcb; return ERR_OK; }
void tcp_recved(struct tcp_pcb *pcb, u16_t len) { (void)pcb; (void)len; }

void bridge_tcp_reply(struct tcp_pcb *pcb, const char *msg)
{
	(void)tcp_write(pcb, msg, (u16_t)strlen(msg), TCP_WRITE_FLAG_COPY);
}

/* --- the loop ------------------------------------------------------------------ */

static void on_udp(struct udp_pcb *pcb, sock_t s)
{
	char buf[2048];
	struct sockaddr_in from;
	socklen_t fl = sizeof(from);
	int n = recvfrom(s, buf, sizeof(buf), 0, (struct sockaddr *)&from, &fl);
	if (n <= 0) {
		return;
	}
	struct pbuf *p = pbuf_alloc(PBUF_TRANSPORT, (u16_t)n, PBUF_RAM);
	memcpy(p->payload, buf, (size_t)n);
	ip_addr_t addr = { from.sin_addr.s_addr };
	pcb->reply_via = s;
	if (pcb->fn != NULL) {
		pcb->fn(pcb->arg, pcb, p, &addr, ntohs(from.sin_port));   /* the callee frees p */
	} else {
		pbuf_free(p);
	}
}

static void on_accept(struct tcp_pcb_host *l)
{
	struct sockaddr_in from;
	socklen_t fl = sizeof(from);
	sock_t c = accept(l->s, (struct sockaddr *)&from, &fl);
	if (c == BAD_SOCK) {
		return;
	}
	struct tcp_pcb_host *h = tcp_alloc();
	if (h == NULL) {
		close_sock(c);
		return;
	}
	h->kind = T_CLIENT;
	h->s = c;
	h->pub.remote_ip.addr = from.sin_addr.s_addr;
	err_t r = (l->accept != NULL) ? l->accept(l->arg, &h->pub, ERR_OK) : ERR_VAL;
	if (r != ERR_OK) {
		/* lwIP aborts a connection its accept callback refused. */
		tcp_kill(h, 1);
	}
}

static void on_client(struct tcp_pcb_host *h)
{
	char buf[SEGMENT];
	int n = recv(h->s, buf, sizeof(buf), 0);
	if (n < 0) {
		/* lwIP frees the pcb before telling the firmware. */
		tcp_err_fn err = h->err;
		void *arg = h->arg;
		tcp_kill(h, 1);
		if (err != NULL) {
			err(arg, ERR_RST);
		}
		return;
	}
	if (n == 0) {
		if (h->recv != NULL) {
			h->recv(h->arg, &h->pub, NULL, ERR_OK);
		}
		if (!h->dead) {
			tcp_kill(h, 0);
		}
		return;
	}
	if ((s_filter != NULL) && s_filter(&h->pub, (const uint8_t *)buf, n)) {
		return;
	}
	struct pbuf *p = pbuf_alloc(PBUF_TRANSPORT, (u16_t)n, PBUF_RAM);
	memcpy(p->payload, buf, (size_t)n);
	if (h->recv != NULL) {
		h->recv(h->arg, &h->pub, p, ERR_OK);
	} else {
		pbuf_free(p);
	}
}

static void sweep(void)
{
	for (int i = 0; i < MAX_TCP; i++) {
		if ((s_tcp[i].kind != T_FREE) && s_tcp[i].dead) {
			s_tcp[i].kind = T_FREE;
		}
	}
}

void bridge_poll(int timeout_ms)
{
	fd_set rd;
	sock_t maxs = 0;
	struct timeval tv = { timeout_ms / 1000, (timeout_ms % 1000) * 1000 };

	FD_ZERO(&rd);
	for (int i = 0; i < MAX_UDP; i++) {
		for (int k = 0; s_udp[i].used && (k < s_udp[i].ns); k++) {
			FD_SET(s_udp[i].s[k], &rd);
			if (s_udp[i].s[k] > maxs) maxs = s_udp[i].s[k];
		}
	}
	for (int i = 0; i < MAX_TCP; i++) {
		struct tcp_pcb_host *h = &s_tcp[i];
		if (((h->kind == T_LISTEN) || (h->kind == T_CLIENT)) && !h->dead && (h->s != BAD_SOCK)) {
			FD_SET(h->s, &rd);
			if (h->s > maxs) maxs = h->s;
		}
	}
	if (select((int)maxs + 1, &rd, NULL, NULL, &tv) > 0) {
		for (int i = 0; i < MAX_UDP; i++) {
			for (int k = 0; s_udp[i].used && (k < s_udp[i].ns); k++) {
				if (FD_ISSET(s_udp[i].s[k], &rd)) {
					on_udp(&s_udp[i], s_udp[i].s[k]);
				}
			}
		}
		for (int i = 0; i < MAX_TCP; i++) {
			struct tcp_pcb_host *h = &s_tcp[i];
			if (h->dead || (h->s == BAD_SOCK) || !FD_ISSET(h->s, &rd)) {
				continue;
			}
			if (h->kind == T_LISTEN) {
				on_accept(h);
			} else if (h->kind == T_CLIENT) {
				on_client(h);
			}
		}
	}

	uint64_t t = now_ms();
	for (int i = 0; i < MAX_TCP; i++) {
		struct tcp_pcb_host *h = &s_tcp[i];
		if ((h->kind == T_CLIENT) && !h->dead && (h->poll != NULL) && (h->poll_interval > 0U)
				&& (t >= h->next_poll)) {
			h->next_poll = t + (uint64_t)h->poll_interval * TCP_SLOW_MS;
			if (h->poll(h->arg, &h->pub) == ERR_ABRT) {
				continue;
			}
		}
	}
	sweep();
}
