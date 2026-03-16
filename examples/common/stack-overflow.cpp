/*
 * stack-overflow.cpp
 *
 * Crash type : STATUS_STACK_OVERFLOW (0xC00000FD)
 * Mechanism  : Unbounded recursion with a large local frame on each call
 *              exhausts the thread stack and hits the guard page.
 *
 * What to look for in WinDbg:
 *   - Exception code 0xC00000FD
 *   - Hundreds of identical frames in the call stack
 *   - RSP deep into the stack commitment limit
 */
#include <stdio.h>
#include <string.h>
#include "crashdump.h"

volatile int g_depth = 0;

void recursive_descent(int n)
{
    /* Large local buffer consumes ~4 KB per frame, burning
       through the default 1 MB stack in ~250 frames. */
    char frame_payload[4096];
    memset(frame_payload, (char)(n & 0xFF), sizeof(frame_payload));

    g_depth++;
    if (g_depth % 50 == 0)
        printf("  depth = %d  (rsp approx %p)\n", g_depth, (void *)frame_payload);

    recursive_descent(n + 1);
}

int main(void)
{
    EnableCrashDumps();

    printf("=== Stack Overflow Demo ===\n");
    printf("Starting unbounded recursion (4 KB per frame)...\n\n");

    recursive_descent(0);

    return 0;
}
