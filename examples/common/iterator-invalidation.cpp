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
 *               calls after the pointer was cached. Each `Sample` is
 *               deliberately oversized so the backing array crosses
 *               glibc's mmap threshold: the freed array is truly unmapped,
 *               so the stale pointer faults immediately and deterministically
 *               instead of silently succeeding on a still-mapped free list
 *               entry.
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
#include <vector>
#include "crashdump.h"

// Padded well past glibc's default mmap threshold (128 KiB) so that the
// vector's backing allocation is mmap-backed, and freeing it on growth
// truly unmaps the memory rather than returning it to a reusable free list.
struct Sample {
    double value;
    int    flags;
    char   label[32];
    char   _pad[192 * 1024];
};

class MetricsBuffer {
public:
    Sample *record_sample(double v) {
        samples_.push_back(Sample{v, 0, {0}, {0}});
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
    std::vector<Sample> samples_;
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
