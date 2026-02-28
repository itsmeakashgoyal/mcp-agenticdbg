/*
 * double-free.cpp
 *
 * Crash type : Heap corruption / Access violation
 * Mechanism  : A heap block is freed, another allocation reuses it, then
 *              the original pointer is freed a second time.  This corrupts
 *              the allocator's internal free-list.  Subsequent heap
 *              operations (malloc/free loop) surface the corruption as a
 *              hard crash inside ntdll!RtlpFreeHeap or similar.
 *
 * What to look for in WinDbg:
 *   - !analyze showing heap corruption
 *   - Exception inside ntdll heap routines
 *   - !heap -s / !heap -a showing inconsistent state
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "crashdump.h"

int main(void)
{
    EnableCrashDumps();

    printf("=== Double Free Demo ===\n\n");

    const size_t BLOCK_SIZE = 128;

    /* First allocation */
    char *data = (char *)malloc(BLOCK_SIZE);
    memset(data, 'Q', BLOCK_SIZE);
    printf("  allocated  data = %p\n", data);

    /* Legitimate free */
    free(data);
    printf("  freed      data = %p  (first time — ok)\n", data);

    /* Another allocation likely reuses the same block */
    char *other = (char *)malloc(BLOCK_SIZE);
    memset(other, 'R', BLOCK_SIZE);
    printf("  allocated  other = %p  (likely same block)\n", other);

    /* BUG: free the original pointer again.
       This puts the same block on the free list twice. */
    printf("  freeing    data = %p  (SECOND TIME — bug!)\n", data);
    free(data);

    /* Provoke the corrupted heap by churning allocations.
       The allocator will eventually follow a poisoned free-list
       entry and crash. */
    printf("\n  Churning heap to surface corruption...\n");
    for (int i = 0; i < 10000; i++)
    {
        void *p = malloc(BLOCK_SIZE);
        memset(p, 0xFF, BLOCK_SIZE);
        free(p);
    }

    free(other);
    printf("  (survived — heap corruption was silent on this run)\n");
    return 0;
}
