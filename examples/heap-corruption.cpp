/*
 * heap-corruption.cpp
 *
 * Crash type : Heap corruption detected by ntdll heap manager
 * Mechanism  : After a normal malloc, the program writes 16 bytes past the
 *              end of the allocated block, stomping on the next heap chunk's
 *              metadata.  When free() is called, the heap manager validates
 *              the surrounding metadata and crashes because it is no longer
 *              consistent.
 *
 * What to look for in WinDbg:
 *   - Exception inside ntdll!RtlpFreeHeap or ntdll!RtlpValidateHeapEntry
 *   - !heap -a showing corrupt block headers
 *   - Bytes 0xBA 0xAD 0xF0 0x0D visible past the allocation boundary
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "crashdump.h"

int main(void)
{
    EnableCrashDumps();

    printf("=== Heap Corruption Demo ===\n\n");

    const size_t ALLOC_SIZE = 48;

    char *block = (char *)malloc(ALLOC_SIZE);
    if (!block) return 1;

    printf("  allocated %zu bytes at %p\n", ALLOC_SIZE, block);
    memset(block, 'Z', ALLOC_SIZE);

    /* BUG: write 16 bytes past the allocation boundary.
       This corrupts the heap chunk header of the *next* block
       (or the heap segment metadata). */
    printf("  writing 16 bytes past allocation boundary...\n");
    unsigned char poison[] = { 0xBA, 0xAD, 0xF0, 0x0D,
                               0xBA, 0xAD, 0xF0, 0x0D,
                               0xBA, 0xAD, 0xF0, 0x0D,
                               0xBA, 0xAD, 0xF0, 0x0D };
    memcpy(block + ALLOC_SIZE, poison, sizeof(poison));

    /* The free below walks heap metadata that we just corrupted.
       On Windows 10+ (segment heap or NT heap with validation),
       this reliably triggers a crash. */
    printf("  freeing block (heap manager will validate metadata)...\n");
    free(block);

    /* If free survived, do more heap work to surface the damage. */
    printf("  churning heap to surface corruption...\n");
    for (int i = 0; i < 5000; i++)
    {
        void *p = malloc(ALLOC_SIZE);
        memset(p, 0xFF, ALLOC_SIZE);
        free(p);
    }

    printf("  (heap corruption was not detected on this run)\n");
    return 0;
}
