/*
 * concurrent-vector-race.cpp
 *
 * Crash type  : SIGSEGV or SIGABRT -- heap corruption from two or more
 *               threads mutating the same std::vector without a lock
 * Mechanism   : Four worker threads all call push_back() on a shared
 *               std::vector<Reading> with no mutex protecting it. When a
 *               reallocation on one thread races with a concurrent
 *               read/write on another, the threads end up operating on
 *               inconsistent views of the backing array -- one is still
 *               writing through a pointer to the array another thread's
 *               push_back() just freed and replaced.
 *
 * Complexity  : There is no single "bug line" the way there is in
 *               thread-uaf.cpp -- every call site (worker()) is
 *               individually correct C++; the defect is the *absence* of
 *               a mutex around a container that is not safe for
 *               concurrent writers. `bt` on the crashing thread shows an
 *               ordinary vector growth path with nothing wrong-looking in
 *               isolation -- the fix requires recognizing that multiple
 *               threads reach that code path at once. The exact signal
 *               (SIGSEGV vs. SIGABRT) and exact iteration count before it
 *               fires both vary run to run, since this is a genuine data
 *               race, not a scripted sequence.
 *
 * What to look for in GDB:
 *   - info threads          -- multiple worker threads, several inside
 *                               std::vector<Reading>::push_back /
 *                               operator new
 *   - thread apply all bt   -- compare stacks; more than one thread is
 *                               mid-growth on the *same* vector
 *   - Root cause: SensorLog::record() has no synchronisation at all
 *
 * Fix hint:
 *   - Guard `readings_` with a std::mutex, give each thread its own buffer
 *     and merge under a lock, or use a container/queue designed for
 *     multi-producer access (e.g. a lock-free MPSC queue).
 */
#include <atomic>
#include <cstdio>
#include <thread>
#include <vector>
#include "crashdump.h"

struct Reading {
    int    sensor_id;
    double value;
    char   tag[16];
};

class SensorLog {
public:
    // BUG: no mutex around readings_ -- concurrent push_back from multiple
    // threads is undefined behaviour (racing reallocation/copy/move).
    void record(int sensor_id, double value, const char *tag) {
        Reading r{};
        r.sensor_id = sensor_id;
        r.value = value;
        snprintf(r.tag, sizeof(r.tag), "%s", tag);
        readings_.push_back(r);
    }

    size_t size() const { return readings_.size(); }

private:
    std::vector<Reading> readings_;
};

static std::atomic<bool> g_start{false};

static void worker(SensorLog *log, int sensor_id, const char *tag, int iterations) {
    while (!g_start.load(std::memory_order_acquire)) {
        std::this_thread::yield();   // spin until every thread is ready
    }
    for (int i = 0; i < iterations; i++) {
        log->record(sensor_id, 20.0 + (i % 10) * 0.1, tag);
    }
}

int main() {
    EnableCrashDumps();
    printf("=== Concurrent Vector Race Demo ===\n\n");

    SensorLog log;
    const int kIterations = 300000;

    std::thread ta(worker, &log, 1, "temp-a", kIterations);
    std::thread tb(worker, &log, 2, "temp-b", kIterations);
    std::thread tc(worker, &log, 3, "temp-c", kIterations);
    std::thread td(worker, &log, 4, "temp-d", kIterations);

    printf("[main] releasing worker threads simultaneously...\n");
    g_start.store(true, std::memory_order_release);

    ta.join();
    tb.join();
    tc.join();
    td.join();

    printf("[main] done -- recorded %zu readings (unreachable if the race fired)\n", log.size());
    return 0;
}
