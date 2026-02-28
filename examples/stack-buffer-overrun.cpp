/*
 * stack-buffer-overrun.cpp
 *
 * Crash type : Access violation (0xC0000005) — call to corrupted address
 * Mechanism  : A struct on the stack contains a 32-byte char buffer followed
 *              by a function pointer.  An unbounded strcpy overflows the
 *              buffer and overwrites the function pointer with 0x41 ('A')
 *              bytes.  When the program calls the pointer, it jumps to
 *              0x4141414141414141 — an unmapped address.
 *
 * What to look for in WinDbg:
 *   - AV trying to execute address 0x4141414141414141
 *   - Stack frame showing the overflowed buffer full of 'A' characters
 *   - Corrupted function pointer visible via dps on the stack
 *
 * Note: built with /GS- to disable MSVC stack cookies so the raw
 *       overwrite is visible (rather than __fastfail termination).
 */
#pragma warning(disable : 4996) /* allow strcpy for demo purposes */

#include <stdio.h>
#include <string.h>
#include "crashdump.h"

typedef void (*CommandHandler)(const char *);

struct CommandContext
{
    char           input[32];     /* 32-byte buffer */
    CommandHandler handler;       /* function pointer right after buffer */
};

static void safe_handler(const char *msg)
{
    printf("  handler called with: %s\n", msg);
}

static void process_command(const char *raw_input)
{
    CommandContext ctx;
    ctx.handler = safe_handler;

    printf("  handler before copy : %p\n", (void *)(uintptr_t)ctx.handler);

    /* BUG: raw_input is 80 bytes, buffer is only 32.
       The overflow writes past input[] and overwrites ctx.handler. */
    strcpy(ctx.input, raw_input);

    printf("  handler after copy  : %p\n", (void *)(uintptr_t)ctx.handler);
    printf("  calling handler...\n");

    ctx.handler(ctx.input);   /* CRASH: 0x4141414141414141 */
}

int main(void)
{
    EnableCrashDumps();

    printf("=== Stack Buffer Overrun Demo ===\n\n");

    /* Craft a payload longer than the 32-byte input buffer.
       'A' = 0x41, so the function pointer becomes 0x4141414141414141. */
    char payload[80];
    memset(payload, 'A', sizeof(payload) - 1);
    payload[sizeof(payload) - 1] = '\0';

    process_command(payload);

    return 0;
}
