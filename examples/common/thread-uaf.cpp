/*
 * thread-uaf.cpp
 *
 * Crash type  : SIGSEGV — write to unmapped heap memory from a worker thread
 * Mechanism   : A "SessionPool" owns a Session object. A "watchdog" thread
 *               detects that the session has timed out and frees it. A
 *               "worker" thread that holds a raw pointer to the same
 *               session then calls session->record() — writing into memory
 *               that has since been unmapped.
 *
 * Reliability note: a plain small-object use-after-free frequently does
 * *not* crash -- the freed bytes are usually still mapped and untouched, so
 * a stale write "succeeds" silently. glibc will actually munmap() a large
 * enough allocation on free() (padding past its mmap threshold used to be
 * this demo's technique), but that's a glibc-specific heuristic: macOS's
 * libmalloc keeps freed allocations mapped and reusable regardless of size
 * (confirmed empirically -- even an 8 MiB malloc()/free() survives a stale
 * write untouched), so the padding trick doesn't reproduce there. Session
 * instead defines its own operator new/delete backed directly by
 * mmap()/munmap(), bypassing the platform allocator's heuristics entirely --
 * once `delete s` runs, the memory is unconditionally unmapped on every
 * POSIX platform.
 *
 * The watchdog and worker also synchronise through an explicit atomic
 * request counter (the same style concurrent-vector-race.cpp and
 * lock-order-inversion-deadlock.cpp use) instead of guessed usleep()
 * windows, so the watchdog always closes the session at the same point in
 * the worker's progress, regardless of scheduler timing.
 *
 * Complexity  : Two threads; the crash appears in a completely different
 *               thread from the one that called delete. The corrupted
 *               write dereferences a pointer to memory the OS has already
 *               unmapped.
 *
 * What to look for in GDB:
 *   - `info threads` shows 2 threads; the crashing one is the worker
 *   - `thread apply all bt` reveals both stacks side-by-side
 *   - Worker stack: main → run_workers → process_request → session->record
 *   - Watchdog stack: already returned from pool->close / is in pthread_join
 *   - `frame N; print s` in the worker frame shows the now-dangling pointer
 *   - Root cause: shared raw pointer with no ownership / no synchronisation
 *
 * Fix hint:
 *   - Use std::shared_ptr<Session> + std::weak_ptr, or protect access with
 *     a mutex and check a "closed" flag before every use.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <pthread.h>
#include <sched.h>
#include <sys/mman.h>
#include <atomic>
#include <new>
#include "crashdump.h"

// ---------------------------------------------------------------------------
// Domain model
// ---------------------------------------------------------------------------

struct Metric {
    char   name[32];
    double value;
    int    count;
};

struct Session {
    int    id;
    char   remote_addr[64];
    Metric metrics[8];
    int    metric_count;
    int    request_count;

    // Backed directly by mmap/munmap instead of the platform malloc, so
    // `delete s` unconditionally unmaps the memory -- see the file header's
    // "Reliability note" for why relying on malloc's own mmap-threshold
    // heuristic (as an earlier version of this demo did) isn't portable.
    static void *operator new(size_t size) {
        void *p = mmap(nullptr, size, PROT_READ | PROT_WRITE,
                        MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (p == MAP_FAILED) throw std::bad_alloc();
        return p;
    }
    static void operator delete(void *p, size_t size) {
        munmap(p, size);
    }

    void record(const char *metric_name, double value) {
        // BUG: called after the Session has been freed (and unmapped) --
        // this write faults.
        if (metric_count >= 8) return;
        Metric *m = &metrics[metric_count++];           // crash here — memory unmapped
        snprintf(m->name, sizeof(m->name), "%s", metric_name);
        m->value = value;
        m->count = 1;
        printf("[session %d] recorded %s=%.2f\n", id, metric_name, value);
    }

    void log_request(const char *path) {
        request_count++;
        printf("[session %d] %s  (req #%d)\n", id, path, request_count);
    }
};

// ---------------------------------------------------------------------------
// "Infrastructure" layer
// ---------------------------------------------------------------------------

struct SessionPool {
    Session *slots[32];
    int      next_id;

    SessionPool() : next_id(0) { memset(slots, 0, sizeof(slots)); }

    Session *open(const char *addr) {
        Session *s = new Session();
        memset(s, 0, sizeof(*s));
        s->id = next_id++;
        strncpy(s->remote_addr, addr, sizeof(s->remote_addr) - 1);
        int idx = s->id % 32;
        slots[idx] = s;
        printf("[pool] opened  session %d (%s) @ %p\n", s->id, addr, (void*)s);
        return s;
    }

    // Called by watchdog — does NOT null out the raw pointer held by the worker
    void close(Session *s) {
        printf("[pool] closing session %d @ %p\n", s->id, (void*)s);
        int idx = s->id % 32;
        slots[idx] = nullptr;
        delete s;                   // <-- BUG: worker still holds raw pointer; unmaps the memory
    }
};

// ---------------------------------------------------------------------------
// Worker and watchdog threads
// ---------------------------------------------------------------------------

struct WorkerCtx {
    Session          *session;
    SessionPool      *pool;
    std::atomic<int>  requests_done{0};   // explicit progress barrier -- no usleep guessing
    std::atomic<bool> session_closed{false};
};

static void process_request(Session *s, const char *path, const char *metric) {
    s->log_request(path);
    s->record(metric, 1.0);         // crash lands here once the session is freed
}

static void run_workers(Session *s, WorkerCtx *ctx) {
    const char *routes[] = {
        "/api/health", "/api/v1/users", "/api/v1/orders", "/api/v1/products", nullptr
    };
    const char *metrics[] = {
        "latency_ms", "latency_ms", "db_queries", "cache_hits", nullptr
    };
    for (int i = 0; routes[i]; i++) {
        process_request(s, routes[i], metrics[i]);
        int done = ctx->requests_done.fetch_add(1, std::memory_order_acq_rel) + 1;

        if (done == 2) {
            // Block here until the watchdog has actually closed the session
            // -- a real synchronisation point, not a guessed sleep -- so
            // request #3 always runs after the session is freed, on every
            // run, regardless of scheduler timing.
            while (!ctx->session_closed.load(std::memory_order_acquire)) {
                sched_yield();
            }
        }
    }
}

void *worker_thread(void *arg) {
    WorkerCtx *ctx = (WorkerCtx *)arg;
    run_workers(ctx->session, ctx);
    return nullptr;
}

void *watchdog_thread(void *arg) {
    WorkerCtx *ctx = (WorkerCtx *)arg;

    // Wait until exactly 2 requests have completed -- a real synchronisation
    // point instead of a guessed sleep duration, so the close always lands
    // between the 2nd and 3rd request regardless of scheduler timing.
    while (ctx->requests_done.load(std::memory_order_acquire) < 2) {
        sched_yield();
    }

    ctx->pool->close(ctx->session);  // session freed (and unmapped); worker's raw ptr now dangles
    ctx->session_closed.store(true, std::memory_order_release);  // release the worker
    return nullptr;
}

// ---------------------------------------------------------------------------

int main(void) {
    EnableCrashDumps();
    printf("=== Thread Use-After-Free Demo ===\n\n");

    SessionPool pool;
    Session *s = pool.open("10.0.0.42");

    WorkerCtx ctx;
    ctx.session = s;
    ctx.pool = &pool;

    pthread_t wdog, worker;
    pthread_create(&worker, nullptr, worker_thread,   &ctx);
    pthread_create(&wdog,   nullptr, watchdog_thread, &ctx);

    pthread_join(worker, nullptr);
    pthread_join(wdog,   nullptr);

    printf("[main] done\n");
    return 0;
}
