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
#include <stdio.h>
#include <stdlib.h>
#include <sys/resource.h>

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
    setrlimit(RLIMIT_CORE, &rl);

    signal(SIGSEGV, _crashdump_signal_handler);
    signal(SIGABRT, _crashdump_signal_handler);
    signal(SIGBUS,  _crashdump_signal_handler);
    signal(SIGFPE,  _crashdump_signal_handler);

    fprintf(stderr, "[crashdump] Core dump handlers installed (ensure `ulimit -c unlimited`)\n");
}

#endif /* _WIN32 */
