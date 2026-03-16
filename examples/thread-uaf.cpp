/*
 * thread-uaf.cpp
 *
 * Crash type  : SIGSEGV — write to freed heap memory from a worker thread
 * Mechanism   : A "RequestHandler" owns a Session object.  A "watchdog"
 *               thread detects that the session has timed out and frees it.
 *               A "worker" thread that holds a raw pointer to the same
 *               session then calls session->record() — writing into freed
 *               (and recycled) memory.
 *
 * Complexity  : Two threads; the crash appears in a completely different
 *               thread from the one that called delete.  The corrupted
 *               write dereferences garbage pointers several levels deep.
 *
 * What to look for in GDB:
 *   - `info threads` shows 2 threads; the crashing one is the worker
 *   - `thread apply all bt` reveals both stacks side-by-side
 *   - Worker stack: main → run_workers → process_request → session->record
 *   - Watchdog stack: already returned from timeout_session / is in pthread_join
 *   - `frame N; print *session` in the worker frame shows corrupted fields
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
#include <unistd.h>
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

    void record(const char *metric_name, double value) {
        // BUG: called after the Session has been freed; fields are garbage
        if (metric_count >= 8) return;
        Metric *m = &metrics[metric_count++];           // crash here — object freed
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
        delete s;                   // <-- BUG: worker still holds raw pointer
    }
};

// ---------------------------------------------------------------------------
// Worker and watchdog threads
// ---------------------------------------------------------------------------

struct WorkerCtx {
    Session      *session;          // raw pointer — will dangle after watchdog fires
    SessionPool  *pool;
    pthread_mutex_t *start_mutex;
    pthread_cond_t  *start_cond;
    bool            *ready;
};

static void process_request(Session *s, const char *path, const char *metric) {
    s->log_request(path);
    // simulate some work
    for (volatile int i = 0; i < 100000; i++) {}
    s->record(metric, 1.0);         // crash lands here after watchdog fires
}

static void run_workers(Session *s) {
    const char *routes[] = {
        "/api/health", "/api/v1/users", "/api/v1/orders", "/api/v1/products", nullptr
    };
    const char *metrics[] = {
        "latency_ms", "latency_ms", "db_queries", "cache_hits", nullptr
    };
    for (int i = 0; routes[i]; i++) {
        usleep(20000);              // 20 ms between requests
        process_request(s, routes[i], metrics[i]);
    }
}

void *worker_thread(void *arg) {
    WorkerCtx *ctx = (WorkerCtx *)arg;

    // Signal that we have started and are using the session
    pthread_mutex_lock(ctx->start_mutex);
    *ctx->ready = true;
    pthread_cond_signal(ctx->start_cond);
    pthread_mutex_unlock(ctx->start_mutex);

    run_workers(ctx->session);      // uses session for ~100 ms total
    return nullptr;
}

void *watchdog_thread(void *arg) {
    WorkerCtx *ctx = (WorkerCtx *)arg;

    // Wait until worker has started
    pthread_mutex_lock(ctx->start_mutex);
    while (!(*ctx->ready))
        pthread_cond_wait(ctx->start_cond, ctx->start_mutex);
    pthread_mutex_unlock(ctx->start_mutex);

    // Fire mid-way through the worker's loop (after 2nd request)
    usleep(55000);                  // 55 ms — lands between 3rd and 4th request
    ctx->pool->close(ctx->session); // session deleted; worker's raw ptr now dangles
    return nullptr;
}

// ---------------------------------------------------------------------------

int main(void) {
    EnableCrashDumps();
    printf("=== Thread Use-After-Free Demo ===\n\n");

    SessionPool pool;
    Session *s = pool.open("10.0.0.42");

    pthread_mutex_t mu  = PTHREAD_MUTEX_INITIALIZER;
    pthread_cond_t  cv  = PTHREAD_COND_INITIALIZER;
    bool            rdy = false;

    WorkerCtx ctx = { s, &pool, &mu, &cv, &rdy };

    pthread_t wdog, worker;
    pthread_create(&worker, nullptr, worker_thread,   &ctx);
    pthread_create(&wdog,   nullptr, watchdog_thread, &ctx);

    pthread_join(worker, nullptr);
    pthread_join(wdog,   nullptr);

    printf("[main] done\n");
    return 0;
}
