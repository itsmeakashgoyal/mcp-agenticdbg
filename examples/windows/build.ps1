# build.ps1 — Compile all crash examples with full debug symbols (PDB).
#
# Usage:
#   cd examples\windows
#   .\build.ps1
#
# Prerequisites:
#   Run from a "Developer Command Prompt for Visual Studio" (or
#   "Developer PowerShell") so that cl.exe and link.exe are on PATH.
#
# Output layout:
#   build\           — intermediate .obj files
#   build\out\       — .exe + .pdb files (ready for WinDbg)

$ErrorActionPreference = "Stop"

$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$commonDir  = Join-Path (Split-Path -Parent $scriptDir) "common"
$buildDir   = Join-Path $scriptDir "build"
$outDir     = Join-Path $buildDir  "out"

New-Item -ItemType Directory -Path $buildDir -Force | Out-Null
New-Item -ItemType Directory -Path $outDir   -Force | Out-Null

$sources = Get-ChildItem -Path $commonDir -Filter "*.cpp"
if ($sources.Count -eq 0) {
    Write-Warning "No .cpp files found in $commonDir"
    exit 1
}

foreach ($src in $sources) {
    $base = $src.BaseName
    $obj  = Join-Path $buildDir "$base.obj"
    $exe  = Join-Path $outDir   "$base.exe"
    $pdb  = Join-Path $outDir   "$base.pdb"

    Write-Host "Building $($src.Name) -> $exe" -ForegroundColor Cyan

    # /Zi   — generate debug info (compiler PDB)
    # /Od   — disable optimisation (cleaner stack traces for demos)
    # /MT   — static CRT (self-contained exe)
    # /EHsc — standard C++ exception handling
    # /GS-  — disable buffer security checks so overruns show raw corruption
    # /I    — include path for crashdump.h in common/
    # /link /DEBUG          — linker: emit full PDB
    # /link /INCREMENTAL:NO — linker: no incremental linking
    # /link /PDB:<path>     — linker: place PDB next to exe
    cl.exe /nologo /Zi /Od /MT /EHsc /GS- /I"$commonDir" `
        /Fo:$obj /Fe:$exe $src.FullName `
        /link /DEBUG /INCREMENTAL:NO /PDB:$pdb

    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to build $($src.Name)"
    }
}

Write-Host ""
Write-Host "Build complete.  Executables + PDBs in: $outDir" -ForegroundColor Green
Write-Host "PDB files ensure WinDbg can map offsets to function names and source lines."
