/*
 * detached-thread-dangling-stack.cpp
 *
 * Crash type  : SIGSEGV (SIGBUS on some architectures -- see note below) --
 *               call through a function pointer corrupted by a detached
 *               thread writing into a stack slot it no longer owns
 * Mechanism   : handle_request() declares a small on-stack SlotData, spawns
 *               a std::thread that captures a *raw pointer* to it so it can
 *               log something once "deferred work" finishes, and detaches
 *               the thread instead of joining it. handle_request() returns
 *               immediately -- its stack frame, including that SlotData, is
 *               gone. process_next_request() then runs through the exact
 *               same leaf function (use_stack_slot()) that handle_request()
 *               used, so its own SlotData lands at the identical stack
 *               address (same function, same frame layout, every call).
 *               The detached thread wakes up and writes through its
 *               now-dangling pointer, landing squarely on
 *               process_next_request()'s live `payload` field.
 *
 * Complexity  : Nothing is wrong at the point of the crash --
 *               process_next_request() calls through a function pointer
 *               it set moments earlier to a real function. The corruption
 *               came from a *different, already-returned* call
 *               (handle_request()) that detached a thread that outlived
 *               it. `info threads` is essential to notice a second thread
 *               was ever involved; the offending thread may have already
 *               logged its output and be sitting idle by the time the
 *               crash is inspected.
 *
 * What to look for in GDB:
 *   - info threads        -- a second thread still exists, parked in
 *                             deferred_logger() (or already exited)
 *   - bt                   -- crash inside use_stack_slot() /
 *                             process_next_request(), calling through
 *                             `data.payload`
 *   - `print data.payload` -- 0xbadc0ffee0ddf00d, an obviously-poisoned,
 *     non-canonical address, not a real function -- the signature of
 *     "something else wrote into my stack slot"
 *   - Root cause: handle_request() detaches a thread holding a raw pointer
 *     into its own stack-local SlotData
 *
 * Fix hint:
 *   - Never let a detached thread hold a pointer/reference into a caller's
 *     stack frame. Capture by value, heap-allocate and capture a
 *     shared_ptr by value, or join() instead of detach() so the parent
 *     frame outlives the thread.
 *
 * Architecture note:
 *   - Jumping through the poisoned (non-canonical) pointer is a general
 *     protection fault -> SIGSEGV on x86_64. On aarch64 (observed directly
 *     on Ubuntu 22.04 aarch64), the stricter alignment-fault handling on
 *     indirect jumps to garbage addresses can surface as SIGBUS instead --
 *     the same architecture difference documented for
 *     stack-buffer-overrun.cpp.
 */
#include <atomic>
#include <cstdio>
#include <cstring>
#include <thread>
#include "crashdump.h"

struct SlotData {
    int    tag;
    char   _pad[4];
    void  *payload;
    char   label[64];
};

enum class Mode { kIncomingRequest, kFollowup };

static std::atomic<int> g_stage{0};   // 0=not ready, 1=slot reused, 2=written

// BUG: takes a raw pointer into the caller's stack-local SlotData and is
// run on a *detached* thread -- nothing guarantees that memory is still
// owned by the caller by the time this runs.
static void deferred_logger(SlotData *slot) {
    while (g_stage.load(std::memory_order_acquire) < 1) {
        std::this_thread::yield();   // wait until the slot has been reused
    }
    printf("  [deferred_logger] writing through dangling pointer @ %p\n", (void *)slot);
    slot->payload = reinterpret_cast<void *>(0xBADC0FFEE0DDF00DULL);
    g_stage.store(2, std::memory_order_release);
}

static void greet(const char *who) {
    printf("  [greet] hello, %s\n", who);
}

// Both handle_request() and process_next_request() bottom out in this same
// function, so `data` lands at the identical stack address both times --
// no guesswork about cross-function frame layout required.
static void use_stack_slot(Mode mode) {
    SlotData data{};

    if (mode == Mode::kIncomingRequest) {
        data.tag = 1001;
        std::strncpy(data.label, "client-42", sizeof(data.label) - 1);
        data.payload = nullptr;
        printf("[handle_request] data @ %p\n", (void *)&data);

        std::thread(deferred_logger, &data).detach();   // BUG: should join()
        // Returns immediately -- `data` goes out of scope, but
        // deferred_logger still holds its address.
    } else {
        data.tag = 7;
        std::strncpy(data.label, "followup", sizeof(data.label) - 1);
        data.payload = reinterpret_cast<void *>(&greet);
        printf("[process_next_request] data @ %p (same stack slot as handle_request's)\n",
               (void *)&data);

        // Tell the detached thread it is now safe, from a timing
        // perspective, to scribble into what used to be handle_request's
        // `data` -- i.e. this stack slot.
        g_stage.store(1, std::memory_order_release);
        while (g_stage.load(std::memory_order_acquire) < 2) {
            std::this_thread::yield();   // wait for the corruption to land
        }

        printf("[process_next_request] data.payload=%p\n", data.payload);

        // BUG surfaces here: data.payload was overwritten by
        // deferred_logger's write into the reused stack slot, so this
        // calls through a poisoned pointer instead of greet().
        auto fn = reinterpret_cast<void (*)(const char *)>(data.payload);
        fn("world");   // <-- crash
    }
}

static void handle_request() { use_stack_slot(Mode::kIncomingRequest); }
static void process_next_request() { use_stack_slot(Mode::kFollowup); }

int main() {
    EnableCrashDumps();
    printf("=== Detached Thread / Dangling Stack Reference Demo ===\n\n");

    handle_request();
    process_next_request();

    printf("[main] done (unreachable if the bug fired)\n");
    return 0;
}
