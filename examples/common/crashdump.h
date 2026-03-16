/*
 * crashdump.h - Cross-platform crash dump generation.
 *
 * Include this header and call EnableCrashDumps() at the top of main().
 *
 * On Windows:
 *   Installs an unhandled-exception filter that writes a full MiniDump
 *   (.dmp) to a "dumps" folder next to the executable.
 *
 * On Linux / macOS:
 *   Installs signal handlers for SIGSEGV, SIGABRT, SIGBUS, and SIGFPE
 *   that re-raise the signal with default handling so the OS generates
 *   a core dump (requires `ulimit -c unlimited`).
 */
#pragma once

#ifdef _WIN32
/* ======================================================================
 * Windows implementation -- MiniDump via dbghelp
 * ====================================================================== */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <dbghelp.h>
#include <stdio.h>
#include <string.h>

#pragma comment(lib, "dbghelp.lib")

static LONG WINAPI CrashDumpHandler(EXCEPTION_POINTERS *pExInfo)
{
    char exePath[MAX_PATH];
    GetModuleFileNameA(NULL, exePath, MAX_PATH);

    char exeDir[MAX_PATH];
    strcpy_s(exeDir, MAX_PATH, exePath);
    char *lastSlash = strrchr(exeDir, '\\');
    if (lastSlash)
        *(lastSlash + 1) = '\0';

    const char *baseName = lastSlash ? lastSlash + 1 : exePath;

    char dumpDir[MAX_PATH];
    sprintf_s(dumpDir, MAX_PATH, "%sdumps", exeDir);
    CreateDirectoryA(dumpDir, NULL);

    char dumpPath[MAX_PATH];
    sprintf_s(dumpPath, MAX_PATH, "%s\\%s.%lu.dmp",
              dumpDir, baseName, GetCurrentProcessId());

    HANDLE hFile = CreateFileA(dumpPath, GENERIC_WRITE, 0, NULL,
                               CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (hFile != INVALID_HANDLE_VALUE)
    {
        MINIDUMP_EXCEPTION_INFORMATION mei;
        mei.ThreadId          = GetCurrentThreadId();
        mei.ExceptionPointers = pExInfo;
        mei.ClientPointers    = FALSE;

        BOOL ok = MiniDumpWriteDump(
            GetCurrentProcess(),
            GetCurrentProcessId(),
            hFile,
            (MINIDUMP_TYPE)(MiniDumpWithFullMemory |
                            MiniDumpWithHandleData |
                            MiniDumpWithThreadInfo),
            &mei, NULL, NULL);

        CloseHandle(hFile);

        if (ok)
            fprintf(stderr, "[crashdump] Dump written: %s\n", dumpPath);
        else
            fprintf(stderr, "[crashdump] MiniDumpWriteDump failed (err %lu)\n",
                    GetLastError());
    }
    else
    {
        fprintf(stderr, "[crashdump] Could not create %s (err %lu)\n",
                dumpPath, GetLastError());
    }

    return EXCEPTION_EXECUTE_HANDLER;
}

static void EnableCrashDumps(void)
{
    SetUnhandledExceptionFilter(CrashDumpHandler);
}

#else
/* ======================================================================
 * POSIX (Linux / macOS) -- signal handlers for core dump generation
 * ====================================================================== */
#include <signal.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/resource.h>

/* 65536 bytes (64 KiB): glibc 2.34+ makes SIGSTKSZ a sysconf() result, so
 * it is no longer usable as a compile-time array size. 64 KiB is well above
 * the POSIX-mandated minimum and sufficient on all supported platforms. */
#define _CRASHDUMP_ALTSTACK_SIZE 65536
static unsigned char _crashdump_altstack_mem[_CRASHDUMP_ALTSTACK_SIZE];

static void _crashdump_signal_handler(int sig)
{
    const char *name = "UNKNOWN";
    switch (sig) {
        case SIGSEGV: name = "SIGSEGV"; break;
        case SIGABRT: name = "SIGABRT"; break;
        case SIGBUS:  name = "SIGBUS";  break;
        case SIGFPE:  name = "SIGFPE";  break;
    }
    fprintf(stderr, "[crashdump] Caught signal %d (%s), generating core dump...\n", sig, name);

    signal(sig, SIG_DFL);
    raise(sig);
}

static void EnableCrashDumps(void)
{
    struct rlimit rl;
    rl.rlim_cur = RLIM_INFINITY;
    rl.rlim_max = RLIM_INFINITY;
    if (setrlimit(RLIMIT_CORE, &rl) != 0)
        fprintf(stderr, "[crashdump] WARNING: setrlimit(RLIMIT_CORE) failed.\n");

    stack_t altstack;
    memset(&altstack, 0, sizeof(altstack));
    altstack.ss_sp = _crashdump_altstack_mem;
    altstack.ss_size = sizeof(_crashdump_altstack_mem);
    if (sigaltstack(&altstack, NULL) != 0)
        fprintf(stderr, "[crashdump] WARNING: sigaltstack setup failed.\n");

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = _crashdump_signal_handler;
    sa.sa_flags = SA_RESETHAND | SA_ONSTACK;
    sigemptyset(&sa.sa_mask);

    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGABRT, &sa, NULL);
    sigaction(SIGBUS,  &sa, NULL);
    sigaction(SIGFPE,  &sa, NULL);

    fprintf(stderr, "[crashdump] Core dump handlers installed (ensure `ulimit -c unlimited`).\n");
#ifdef __APPLE__
    fprintf(stderr, "[crashdump] macOS tip: ensure `launchctl limit core unlimited unlimited` and writable `/cores`.\n");
#endif
}

#endif /* _WIN32 */
