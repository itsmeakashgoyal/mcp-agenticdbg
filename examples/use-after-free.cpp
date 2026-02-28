/*
 * use-after-free.cpp
 *
 * Crash type : Access violation (0xC0000005) — write to freed heap memory
 * Mechanism  : A "Connection" struct is freed, its memory is reclaimed by a
 *              new allocation that fills it with 0xAB bytes, then the stale
 *              pointer is used to dereference the (now-garbage) recv_buffer
 *              field, writing to address 0xABABABABABABABAB.
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
    strcpy_s(c->hostname, sizeof(c->hostname), host);
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

    /* Reallocate the same region and fill with poison bytes.
       malloc is very likely to hand back the same block. */
    void *reuse = malloc(sizeof(Connection));
    memset(reuse, 0xAB, sizeof(Connection));

    printf("\n  Accessing freed connection through dangling pointer...\n");

    /* dangling->recv_buffer now reads 0xABABABABABABABAB.
       Dereferencing that as a pointer -> ACCESS VIOLATION. */
    dangling->recv_buffer[0] = 999;

    free(reuse);
    return 0;
}
