# run-all.ps1 — Execute every crash example and collect the dumps.
#
# Runs each example under cdb.exe (-g -G -hd -c "g;.dump /ma <path>;q")
# rather than launching it directly and waiting for crashdump.h's own
# SetUnhandledExceptionFilter handler to write a dump: double-free,
# heap-corruption, concurrent-vector-race, exception-in-destructor-
# terminate, and lock-order-inversion-deadlock all crash via a Windows
# __fastfail code (STATUS_HEAP_CORRUPTION / STATUS_STACK_BUFFER_OVERRUN),
# which by design bypasses normal SEH dispatch -- including a handler
# registered in the target process itself -- unless a debugger is already
# attached. A debugger *is* notified of __fastfail exceptions first-chance,
# so cdb can write the dump itself before the process would otherwise
# terminate unseen. See eval/README.md's Windows section for the full
# investigation (the same fix eval/run_eval.py's _run_until_crash_windows
# uses).
#
# Usage:
#   .\run-all.ps1            — run all examples
#   .\run-all.ps1 -Name foo  — run only examples whose name matches *foo*

param(
    [string]$Name = "*"
)

$ErrorActionPreference = "Continue"

$examplesDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$outDir      = Join-Path $examplesDir "build\out"
$dumpDir     = Join-Path $outDir      "dumps"

if (-not (Test-Path $outDir)) {
    Write-Error "Build output not found at $outDir — run build.ps1 first."
    exit 1
}

function Find-Cdb {
    $cmd = Get-Command cdb.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    # Same search order as CDBSession.find_debugger_executable() in
    # backends/cdb.py, so this script finds the same cdb.exe the MCP server
    # (and eval/run_eval.py) would.
    $candidates = @(
        "C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe",
        "C:\Program Files (x86)\Windows Kits\10\Debuggers\x86\cdb.exe",
        "C:\Program Files\Debugging Tools for Windows (x64)\cdb.exe",
        "C:\Program Files\Debugging Tools for Windows (x86)\cdb.exe",
        (Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\cdbX64.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\cdbX86.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\cdbARM64.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

$cdb = Find-Cdb
if (-not $cdb) {
    Write-Error ("cdb.exe not found -- install WinDbg (winget install " +
        "Microsoft.WinDbg) or run from a Developer Command Prompt with " +
        "Debugging Tools for Windows on PATH.")
    exit 1
}

New-Item -ItemType Directory -Path $dumpDir -Force | Out-Null

$exes = Get-ChildItem -Path $outDir -Filter "*.exe" |
        Where-Object { $_.BaseName -like $Name }

if ($exes.Count -eq 0) {
    Write-Warning "No executables matching '$Name' found in $outDir"
    exit 1
}

$results = @()

foreach ($exe in $exes) {
    Write-Host "`n========================================" -ForegroundColor Yellow
    Write-Host " Running: $($exe.Name)" -ForegroundColor Yellow
    Write-Host "========================================" -ForegroundColor Yellow

    $dumpPath = Join-Path $dumpDir "$($exe.BaseName).dmp"
    if (Test-Path $dumpPath) {
        Remove-Item $dumpPath -Force
    }

    & $cdb -g -G -hd -c "g;.dump /ma `"$dumpPath`";q" $exe.FullName 2>&1 |
        ForEach-Object { Write-Host "  $_" }

    $found = Test-Path $dumpPath
    $dumpFile = if ($found) { Split-Path $dumpPath -Leaf } else { "(none)" }

    $results += [PSCustomObject]@{
        Example  = $exe.BaseName
        DumpFile = $dumpFile
    }

    if ($found) {
        Write-Host "  -> Dump: $dumpPath" -ForegroundColor Green
    } else {
        Write-Host "  -> No dump file created" -ForegroundColor Red
    }
}

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host " Summary" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
$results | Format-Table -AutoSize

Write-Host "Dump directory: $dumpDir"
Write-Host "PDB  directory: $outDir"
Write-Host ""
Write-Host "To analyze a dump with TriagePilot, use:" -ForegroundColor Cyan
Write-Host "  analyze_dump      <path-to-.dmp>"
Write-Host "  run_debugger_cmd  .ecxr; kv"
