/*
 * exception-in-destructor-terminate.cpp
 *
 * Crash type  : SIGABRT via std::terminate() -- "terminate called after
 *               throwing an instance of ..." followed by abort()
 * Mechanism   : `ScopedTransaction` is an RAII guard that auto-commits work
 *               in its destructor and *throws* on commit failure -- a
 *               violation of "destructors must not throw". When
 *               validate_order() throws a ValidationError, the stack
 *               unwinds through a ScopedTransaction whose destructor also
 *               throws (CommitError) while the first exception is still
 *               active. The C++ runtime cannot propagate two simultaneous
 *               exceptions and calls std::terminate(), which calls abort().
 *
 * Complexity  : The top of the backtrace is entirely inside the C++
 *               runtime (abort -> __gnu_cxx::__verbose_terminate_handler ->
 *               std::terminate -> __cxa_throw) -- there is no user-code
 *               frame with a clean file:line at the very top. Finding the
 *               real bug means walking down to
 *               ScopedTransaction::~ScopedTransaction() and recognizing
 *               that it throws.
 *
 * What to look for in GDB:
 *   - bt        -- top frames are abort/raise/terminate-handler internals
 *   - bt full   -- several frames down: ScopedTransaction::~ScopedTransaction
 *   - stderr prints BOTH exception messages (ValidationError and
 *     CommitError) -- the strongest clue this is a double-exception, not a
 *     single unhandled one
 *   - Root cause: a destructor throwing while another exception unwinds
 *
 * Fix hint:
 *   - Never let a destructor throw. Wrap the destructor body in
 *     try/catch and log-and-swallow on failure, or expose an explicit
 *     commit()/rollback() method the caller must invoke before the guard
 *     leaves scope.
 */
#include <cstdio>
#include <stdexcept>
#include <string>
#include "crashdump.h"

struct ValidationError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

struct CommitError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

// RAII guard that "commits" a batch of work when it goes out of scope.
class ScopedTransaction {
public:
    explicit ScopedTransaction(std::string name) : name_(std::move(name)), committed_(false) {
        printf("  [txn] BEGIN %s\n", name_.c_str());
    }

    // BUG: throws on failure -- destructors must never throw, especially
    // while another exception is already unwinding the stack.
    ~ScopedTransaction() {
        if (!committed_) {
            printf("  [txn] auto-committing %s on scope exit...\n", name_.c_str());
            commit();   // <-- may throw CommitError
        }
    }

    void commit() {
        if (name_ == "orders-batch-42") {
            // Simulates a write-ahead-log fsync failure discovered only at
            // commit time -- by then it's too late to report cleanly if
            // we're already unwinding from another failure.
            throw CommitError("fsync failed while committing '" + name_ + "'");
        }
        committed_ = true;
        printf("  [txn] COMMIT %s (ok)\n", name_.c_str());
    }

private:
    std::string name_;
    bool committed_;
};

static void validate_order(int order_id) {
    if (order_id == 42) {
        throw ValidationError("order 42 failed schema validation");
    }
}

static void process_orders_batch() {
    ScopedTransaction txn("orders-batch-42");   // destructor will also throw

    for (int order_id = 40; order_id <= 44; order_id++) {
        printf("  [orders] validating order %d...\n", order_id);
        validate_order(order_id);   // throws ValidationError on order 42
        printf("  [orders] order %d OK\n", order_id);
    }
    // Never reached for order 42; txn goes out of scope while unwinding,
    // its destructor tries to auto-commit and throws CommitError --
    // two exceptions in flight at once => std::terminate().
}

int main() {
    EnableCrashDumps();
    printf("=== Exception-in-Destructor / std::terminate Demo ===\n\n");

    try {
        process_orders_batch();
    } catch (const std::exception &e) {
        // BUG: this handler is never reached -- std::terminate() fires
        // before the exception can be caught here, because txn's
        // destructor throws while ValidationError is still propagating.
        printf("[main] caught: %s\n", e.what());
        return 1;
    }

    printf("[main] done\n");
    return 0;
}
