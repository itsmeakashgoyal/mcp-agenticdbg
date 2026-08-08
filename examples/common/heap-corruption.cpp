/*
 * heap-corruption.cpp
 *
 * Crash type : Heap corruption detected by the allocator's own metadata
 *              validation (glibc: SIGABRT via malloc_printerr; Windows:
 *              ntdll heap manager on free()).
 * Mechanism  : Two same-size blocks are allocated back-to-back on a fresh
 *              heap (adjacent, since a bump/wilderness allocator places
 *              consecutive same-size requests contiguously before any
 *              fragmentation happens). The program then writes past the
 *              end of the first block, directly into the heap chunk header
 *              that describes the second block. Freeing the first block
 *              (whose own header is untouched) succeeds normally; the
 *              corruption only surfaces later, when the second block is
 *              freed and the allocator tries to interpret its now-bogus
 *              size field -- a classic "delayed" heap-overflow bug where
 *              the crash happens far from (and after) the actual mistake.
 *
 * Reliability note: a *plausible-looking* small overflow that merely pokes
 * a few "poison" bytes past an allocation boundary is not actually
 * guaranteed to land on anything the allocator validates -- small
 * allocations are often served from a per-thread cache (glibc's tcache)
 * whose free() path does minimal validation, so the corruption can go
 * completely undetected. To make this demo reproduce deterministically on
 * glibc, the overflow below targets the exact offset where the next
 * chunk's own size field lives (`ptr + malloc_usable_size(ptr)`) and
 * overwrites it with a value no allocator would ever consider a valid
 * chunk size, so free() of *that* chunk always fails its sanity checks.
 *
 * What to look for in GDB:
 *   - SIGABRT inside malloc_printerr / __libc_message
 *   - `bt` shows the abort originating from the second free() in main()
 *   - `p *(size_t*)(a + malloc_usable_size(a))` shows the stomped header
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "crashdump.h"

#if defined(__linux__) && defined(__GLIBC__)
#include <malloc.h>
#define HEAP_CORRUPTION_GLIBC_TECHNIQUE 1
#endif

int main(void)
{
    EnableCrashDumps();

    printf("=== Heap Corruption Demo ===\n\n");

#ifdef HEAP_CORRUPTION_GLIBC_TECHNIQUE
    const size_t ALLOC_SIZE = 64;

    /* Two identically-sized, back-to-back allocations on a fresh heap are
       adjacent in memory -- nothing has been freed yet, so the allocator
       has no choice but to extend the wilderness chunk contiguously. */
    char *a = (char *)malloc(ALLOC_SIZE);
    char *b = (char *)malloc(ALLOC_SIZE);
    if (!a || !b) return 1;

    printf("  allocated chunk a=%p and chunk b=%p (adjacent)\n", (void *)a, (void *)b);
    memset(a, 'A', ALLOC_SIZE);
    memset(b, 'B', ALLOC_SIZE);

    /* BUG: write past the end of `a`. The 8 bytes immediately following a
       chunk's usable region are the *next* chunk's own size header --
       stomping them with an unmistakably invalid value corrupts chunk b's
       metadata, even though chunk b itself hasn't been touched otherwise. */
    printf("  writing past end of chunk a into chunk b's size header...\n");
    size_t *b_size_field = (size_t *)(a + malloc_usable_size(a));
    *b_size_field = (size_t)0xDEADBEEFDEADBEEFULL;

    printf("  freeing chunk a (unaffected -- its own header is intact)...\n");
    free(a);
    printf("  chunk a freed cleanly; corruption is still latent in chunk b\n");

    printf("  freeing chunk b (allocator now reads its corrupted header)...\n");
    free(b);   /* <-- SIGABRT: allocator rejects b's bogus chunk size */

    printf("  (heap corruption was not detected on this run)\n");
    return 0;
#else
    /* Portable fallback for non-glibc allocators (macOS libmalloc, Windows
       CRT/NT heap): a straightforward out-of-bounds write past a heap
       block, validated (or not) by whatever allocator is in use. */
    const size_t ALLOC_SIZE = 48;

    char *block = (char *)malloc(ALLOC_SIZE);
    if (!block) return 1;

    printf("  allocated %zu bytes at %p\n", ALLOC_SIZE, block);
    memset(block, 'Z', ALLOC_SIZE);

    printf("  writing 16 bytes past allocation boundary...\n");
    unsigned char poison[] = { 0xBA, 0xAD, 0xF0, 0x0D,
                               0xBA, 0xAD, 0xF0, 0x0D,
                               0xBA, 0xAD, 0xF0, 0x0D,
                               0xBA, 0xAD, 0xF0, 0x0D };
    memcpy(block + ALLOC_SIZE, poison, sizeof(poison));

    printf("  freeing block (heap manager will validate metadata)...\n");
    free(block);

    printf("  churning heap to surface corruption...\n");
    for (int i = 0; i < 5000; i++)
    {
        void *p = malloc(ALLOC_SIZE);
        memset(p, 0xFF, ALLOC_SIZE);
        free(p);
    }

    printf("  (heap corruption was not detected on this run)\n");
    return 0;
#endif
}
