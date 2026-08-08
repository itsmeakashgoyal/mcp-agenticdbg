/*
 * iterator-invalidation.cpp
 *
 * Crash type  : SIGSEGV -- write through a pointer into memory freed by
 *               std::vector's internal reallocation
 * Mechanism   : `MetricsBuffer` keeps a std::vector<Sample> and hands out a
 *               raw pointer to a just-recorded sample via record_sample()
 *               so a caller can fill it in once more context is available
 *               ("open-ended writer" pattern). A later record_sample()
 *               call grows the vector past its capacity; std::vector
 *               allocates a new backing array, moves elements into it, and
 *               frees (in this case munmaps -- see below) the old one. The
 *               caller's cached pointer still refers to the old array.
 *
 * Complexity  : There is no explicit free()/delete anywhere near the crash
 *               -- the "free" is std::vector's internal reallocation, many
 *               calls after the pointer was cached. `samples_` uses
 *               DirectMapAllocator (below) instead of the default
 *               std::allocator, so each reallocation's old backing array is
 *               released via a direct mmap/munmap (or VirtualAlloc/
 *               VirtualFree on Windows) rather than the platform malloc.
 *               Relying on malloc's own large-allocation heuristics (an
 *               earlier version of this demo padded Sample past glibc's
 *               mmap threshold) only produces a real, immediate unmap on
 *               glibc -- confirmed empirically that macOS's libmalloc keeps
 *               freed allocations mapped and reusable regardless of size,
 *               so the stale pointer just silently succeeds there instead
 *               of faulting.
 *
 * What to look for in GDB:
 *   - bt               -- crash inside finalize_sample(), writing to *s
 *   - up; print *this  -- samples_.capacity() has grown since `pending`
 *                          was cached; capacity() * sizeof(Sample) shows
 *                          multiple reallocations happened
 *   - Root cause: record_sample() returns a pointer into samples_ that is
 *     only valid until the next reallocation
 *
 * Fix hint:
 *   - Return an index (or a stable handle) instead of a raw pointer, call
 *     reserve() up front for the final expected size, or use a container
 *     with reference stability (std::deque, std::list) for elements that
 *     must outlive further growth.
 */
#include <cstdio>
#include <new>
#include <vector>
#include "crashdump.h"

#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#else
#include <sys/mman.h>
#endif

struct Sample {
    double value;
    int    flags;
    char   label[32];
};

// Allocator that maps/unmaps memory directly via the OS instead of going
// through the platform malloc -- see the file header's "Complexity" note
// for why relying on malloc's own large-allocation heuristics (an earlier
// version of this demo padded Sample and depended on glibc's mmap
// threshold) isn't portable. Mirrors thread-uaf.cpp's custom operator
// new/delete, generalized to a container's internal (re)allocations.
template <class T>
struct DirectMapAllocator {
    using value_type = T;

    DirectMapAllocator() = default;
    template <class U> DirectMapAllocator(const DirectMapAllocator<U> &) {}

    T *allocate(size_t n) {
        size_t bytes = n * sizeof(T);
#if defined(_WIN32)
        void *p = VirtualAlloc(nullptr, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        if (!p) throw std::bad_alloc();
#else
        void *p = mmap(nullptr, bytes, PROT_READ | PROT_WRITE,
                        MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (p == MAP_FAILED) throw std::bad_alloc();
#endif
        return static_cast<T *>(p);
    }

    void deallocate(T *p, size_t n) noexcept {
        // A plain munmap()/VirtualFree(..., MEM_RELEASE) releases the
        // address back to the OS -- which then happily hands that exact
        // range right back out for the *next* similarly-sized allocation
        // (confirmed empirically: the freed buffer's address was the very
        // next one mmap() returned a couple of growths later), silently
        // making a stale pointer valid again instead of faulting. Remapping
        // PROT_NONE / decommitting in place keeps the range reserved by
        // this process forever, so it can never be handed to a later
        // allocation and a stale access always faults.
#if defined(_WIN32)
        VirtualFree(p, n * sizeof(T), MEM_DECOMMIT);
#else
        mmap(p, n * sizeof(T), PROT_NONE, MAP_FIXED | MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
#endif
    }
};

template <class T, class U>
bool operator==(const DirectMapAllocator<T> &, const DirectMapAllocator<U> &) {
    return true;
}
template <class T, class U>
bool operator!=(const DirectMapAllocator<T> &, const DirectMapAllocator<U> &) {
    return false;
}

class MetricsBuffer {
public:
    Sample *record_sample(double v) {
        samples_.push_back(Sample{v, 0, {0}});
        return &samples_.back();   // BUG: valid only until the next growth
    }

    void finalize_sample(Sample *s, const char *label) {
        // BUG: `s` may point into a backing array that has already been
        // reallocated (and unmapped) if growth happened after it was cached.
        s->flags |= 1;
        snprintf(s->label, sizeof(s->label), "%s", label);   // <-- crash here
    }

    size_t count() const { return samples_.size(); }
    size_t capacity() const { return samples_.capacity(); }

private:
    std::vector<Sample, DirectMapAllocator<Sample>> samples_;
};

int main() {
    EnableCrashDumps();
    printf("=== Iterator/Reference Invalidation Demo ===\n\n");

    MetricsBuffer buf;

    buf.record_sample(1.0);
    printf("[main] after 1st sample: size=%zu capacity=%zu\n", buf.count(), buf.capacity());

    // Cache a pointer to the sample just recorded, to "finalize" later once
    // more context is available -- a realistic-looking API pattern.
    Sample *pending = buf.record_sample(2.0);
    printf("[main] after 2nd sample: size=%zu capacity=%zu (cached pointer=%p)\n",
           buf.count(), buf.capacity(), (void *)pending);

    // Each of these forces further growth, reallocating (and unmapping) the
    // backing store that `pending` still points into.
    for (int i = 0; i < 4; i++) {
        buf.record_sample(3.0 + i);
        printf("[main] after sample %d: size=%zu capacity=%zu\n", i + 3, buf.count(),
               buf.capacity());
    }

    printf("[main] finalizing pending sample via stale pointer...\n");
    buf.finalize_sample(pending, "checkout-latency");   // dangling -- SIGSEGV

    printf("[main] done\n");
    return 0;
}
