/*
 * use-after-free.cpp
 *
 * Crash type : Access violation (0xC0000005) — write to freed heap memory
 * Mechanism  : A "Connection" struct is freed, its memory is poisoned with
 *              0xAB bytes, then the stale pointer is used to dereference the
 *              (now-garbage) recv_buffer field, writing to address
 *              0xABABABABABABABAB.
 *
 * Reliability note: whether a fresh allocation actually lands on a just-freed
 * block of the same size is allocator- and platform-dependent -- a safe bet
 * on glibc's small-bin freelist, far less so on Windows' heap manager
 * (especially with Segment Heap / LFH), where the block may simply not be
 * reused, leaving the stale write silently harmless instead of crashing. To
 * keep this demo deterministic across platforms, the freed block is poisoned
 * directly through the dangling pointer instead of hoping a new allocation
 * happens to land on the same address.
 *
 * What to look for in WinDbg:
 *   - Write AV at 0xABABABABABABABAB (or similar poison pattern)
 *   - !heap showing the freed block
 *   - Dangling pointer still referencing a recycled heap region
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "crashdump.h"

struct Connection
{
    int    socket_fd;
    char   hostname[64];
    int   *recv_buffer;
    size_t buffer_len;
};

static Connection *open_connection(const char *host, int fd)
{
    Connection *c = (Connection *)malloc(sizeof(Connection));
    if (!c) return NULL;

    c->socket_fd  = fd;
    snprintf(c->hostname, sizeof(c->hostname), "%s", host);
    c->buffer_len = 256;
    c->recv_buffer = (int *)malloc(c->buffer_len * sizeof(int));

    for (size_t i = 0; i < c->buffer_len; i++)
        c->recv_buffer[i] = (int)i;

    printf("  opened  connection %p  (fd=%d, host=%s)\n", c, fd, host);
    return c;
}

static void close_connection(Connection *c)
{
    printf("  closing connection %p  (fd=%d)\n", c, c->socket_fd);
    free(c->recv_buffer);
    free(c);
}

int main(void)
{
    EnableCrashDumps();

    printf("=== Use-After-Free Demo ===\n\n");

    Connection *conn = open_connection("crashdemo.local", 42);

    /* Keep a stale copy of the pointer */
    Connection *dangling = conn;

    /* Free the connection */
    close_connection(conn);
    conn = NULL;

    /* Poison the freed block directly through the dangling pointer -- see
       the reliability note above for why this doesn't rely on a fresh
       allocation happening to reuse the same address. */
    memset(dangling, 0xAB, sizeof(Connection));

    /* A genuinely new allocation may or may not land on the same freed
       block; either way it's no longer load-bearing for the crash above. */
    void *reuse = malloc(sizeof(Connection));

    printf("\n  Accessing freed connection through dangling pointer...\n");

    /* dangling->recv_buffer now reads 0xABABABABABABABAB.
       Dereferencing that as a pointer -> ACCESS VIOLATION. */
    dangling->recv_buffer[0] = 999;

    free(reuse);
    return 0;
}
