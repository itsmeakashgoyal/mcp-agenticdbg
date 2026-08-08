/*
 * format-string-crash.cpp
 *
 * Crash type  : SIGSEGV -- printf() dereferences a garbage vararg as a
 *               pointer (classic CWE-134 format string vulnerability)
 * Mechanism   : `log_unsafe()` passes an externally-supplied string
 *               directly as printf's *format* argument instead of using a
 *               fixed format ("%s", msg). When that string contains
 *               conversion specifiers (%s, %n, ...), printf walks past the
 *               (nonexistent) variadic arguments the caller actually
 *               supplied and reads whatever happens to be sitting in the
 *               next argument-passing registers/stack slots, treating that
 *               garbage as a pointer.
 *
 * Complexity  : The crash frame is deep inside glibc's vfprintf/strlen
 *               internals, not in user code at all. The root cause -- a
 *               user-controlled string reaching printf() as the format
 *               argument -- is two frames up the stack in log_unsafe(),
 *               and is only obvious once you read the *content* of the
 *               offending string, not just the backtrace.
 *
 * What to look for in GDB:
 *   - bt              -- top frames inside __strlen_avx2 / __vfprintf_internal
 *   - up; up           -- log_unsafe(const char *user_supplied_message)
 *   - `print user_supplied_message` -- reveals the "%s %s %s %s %s %n"
 *     payload; that pattern is the signature of a format-string bug
 *   - Root cause: printf(user_supplied_message) instead of
 *     printf("%s", user_supplied_message)
 *
 * Fix hint:
 *   - Never pass externally-influenced data as a format string. Always use
 *     a literal format ("%s") and pass the data as an argument. Compile
 *     with -Wformat -Wformat-security (and treat warnings as errors) to
 *     catch this class of bug at build time.
 */
#include <cstdio>
#include "crashdump.h"

// BUG: `user_supplied_message` is used directly as the *format string*.
// Any % conversion specifiers embedded in it are interpreted by printf.
static void log_unsafe(const char *user_supplied_message) {
    printf(user_supplied_message);   // <-- should be: printf("%s", user_supplied_message)
    printf("\n");
}

static void handle_client_note(const char *client_note) {
    printf("  [handler] forwarding client note to logger...\n");
    log_unsafe(client_note);
}

int main() {
    EnableCrashDumps();
    printf("=== Format String Crash Demo ===\n\n");

    // A "note" field from an untrusted client, forwarded to the log
    // verbatim. No conversion specifiers were ever intended by whoever
    // wrote this client -- but nothing stops one from arriving.
    const char *malicious_note =
        "client reported: %s %s %s %s %s %s %n";

    printf("[main] client_note = \"%s\"\n", malicious_note);
    handle_client_note(malicious_note);   // crashes inside printf()

    printf("[main] done\n");
    return 0;
}
