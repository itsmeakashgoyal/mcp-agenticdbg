/*
 * lock-order-inversion-deadlock.cpp
 *
 * Crash type  : SIGABRT -- a watchdog aborts the process after detecting a
 *               deadlock (not a memory-safety bug; the "crash" is a
 *               deliberate post-mortem trigger, the same technique
 *               production services use: fire abort()/SIGQUIT on a hung
 *               process so the hang produces an analyzable dump instead of
 *               hanging forever)
 * Mechanism   : transfer_funds() always locks the *source* account before
 *               the *destination* account. Two concurrent transfers in
 *               opposite directions (A->B and B->A) therefore lock in
 *               opposite orders: one thread holds A and waits for B, the
 *               other holds B and waits for A. Neither can proceed --
 *               classic lock-order-inversion deadlock. A short sleep while
 *               holding the first lock guarantees both threads reach that
 *               state before either attempts the second lock, so the
 *               deadlock is deterministic, not a race.
 *
 * Complexity  : There is no faulting instruction anywhere -- both worker
 *               threads are correctly blocked in pthread_mutex_lock.
 *               Diagnosing this requires reading BOTH thread stacks
 *               together and cross-referencing which mutex each one holds
 *               vs. is waiting for; neither stack alone explains anything.
 *
 * What to look for in GDB:
 *   - info threads           -- two worker threads still "running" (really
 *                                blocked in a futex/mutex wait)
 *   - thread apply all bt    -- both stuck inside transfer_funds(), in the
 *                                second std::lock_guard construction
 *   - `print account_a.locked_by` / `print account_b.locked_by` -- each
 *     shows the *other* thread's tag as the current holder -- the smoking
 *     gun for lock-order inversion
 *   - Root cause: transfer_funds() locks in caller-supplied order instead
 *     of a globally consistent order
 *
 * Fix hint:
 *   - Always acquire locks in a fixed global order (e.g. sort by account
 *     id/address before locking), or use std::lock(mu_a, mu_b) /
 *     std::scoped_lock(mu_a, mu_b), which acquire both atomically without
 *     the possibility of deadlocking against a reversed-order acquirer.
 */
#include <atomic>
#include <chrono>
#include <cstdio>
#include <mutex>
#include <thread>
#include "crashdump.h"

struct Account {
    int id;
    double balance;
    std::mutex mu;
    std::atomic<int> locked_by{-1};   // diagnostic aid: who currently holds mu
};

// BUG: always locks `from` before `to`, whatever order the caller passes
// in. A concurrent transfer in the opposite direction locks in the
// opposite order -- lock-order inversion.
static void transfer_funds(Account &from, Account &to, double amount, int thread_tag) {
    std::lock_guard<std::mutex> lock_from(from.mu);
    from.locked_by.store(thread_tag, std::memory_order_relaxed);
    printf("  [thread %d] locked account %d, waiting for account %d...\n", thread_tag, from.id,
           to.id);

    std::this_thread::sleep_for(std::chrono::milliseconds(50));   // widen the window

    std::lock_guard<std::mutex> lock_to(to.mu);   // <-- deadlocks here
    to.locked_by.store(thread_tag, std::memory_order_relaxed);

    from.balance -= amount;
    to.balance += amount;
    printf("  [thread %d] transfer complete\n", thread_tag);
}

int main() {
    EnableCrashDumps();
    printf("=== Lock-Order-Inversion Deadlock Demo ===\n\n");

    Account account_a{1, 500.0};
    Account account_b{2, 500.0};

    std::atomic<bool> done_ab{false};
    std::atomic<bool> done_ba{false};

    std::thread worker_ab([&] {
        transfer_funds(account_a, account_b, 100.0, 1);   // locks A then B
        done_ab.store(true, std::memory_order_release);
    });
    std::thread worker_ba([&] {
        transfer_funds(account_b, account_a, 50.0, 2);   // locks B then A
        done_ba.store(true, std::memory_order_release);
    });

    // Watchdog: give the transfers generous time to finish under normal
    // (non-deadlocked) conditions, then abort so the hang is analyzable.
    const int kDeadlineMs = 2000;
    const int kPollMs = 50;
    int waited = 0;
    while (waited < kDeadlineMs) {
        if (done_ab.load() && done_ba.load()) {
            printf("[main] both transfers completed -- no deadlock this run\n");
            worker_ab.join();
            worker_ba.join();
            return 0;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(kPollMs));
        waited += kPollMs;
    }

    fprintf(stderr,
            "[watchdog] deadlock detected: transfer A->B done=%d, B->A done=%d "
            "after %dms -- aborting for post-mortem analysis\n",
            done_ab.load(), done_ba.load(), waited);
    abort();
}
