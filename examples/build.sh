#!/bin/bash
# Build crash examples on Linux / macOS.
#
# Usage:
#   cd examples
#   chmod +x build.sh
#   ./build.sh
#
# Requires: g++ or clang++ with debug info support.
# Output: build/out/<name>  (executable with DWARF debug info)

set -eo pipefail

OUTDIR="build/out"
mkdir -p "$OUTDIR"

# Detect compiler (or honor explicit override)
if [[ -n "${CXX:-}" ]]; then
    if ! command -v "$CXX" &>/dev/null; then
        echo "ERROR: CXX is set to '$CXX', but that compiler is not on PATH."
        exit 1
    fi
elif [[ "$(uname -s)" == "Darwin" ]] && command -v clang++ &>/dev/null; then
    # On macOS, prefer Apple's Clang toolchain.
    CXX=clang++
elif command -v g++ &>/dev/null; then
    CXX=g++
elif command -v clang++ &>/dev/null; then
    CXX=clang++
else
    echo "ERROR: Neither g++ nor clang++ found on PATH."
    exit 1
fi

BASE_FLAGS=(-g -O0 -std=c++17)
EXTRA_CXXFLAGS=()
if [[ -n "${CXXFLAGS:-}" ]]; then
    # Intentionally split user-provided compiler flags.
    # shellcheck disable=SC2206
    EXTRA_CXXFLAGS=(${CXXFLAGS})
fi

echo "Using compiler: $CXX"
echo "Output directory: $OUTDIR"
echo ""

# Standard single-file sources
SOURCES=(
    stack-overflow.cpp
    use-after-free.cpp
    double-free.cpp
    vtable-corruption.cpp
    stack-buffer-overrun.cpp
    heap-corruption.cpp
    deep-callchain-nullptr.cpp
    heap-metadata-corruption.cpp
    multi-inheritance-crash.cpp
)

for src in "${SOURCES[@]}"; do
    base="${src%.cpp}"
    echo "  Compiling $src -> $OUTDIR/$base"
    "$CXX" "${BASE_FLAGS[@]}" "${EXTRA_CXXFLAGS[@]}" -o "$OUTDIR/$base" "$src"
done

# Sources requiring extra link flags
THREADED_SOURCES=(
    thread-uaf.cpp
)

for src in "${THREADED_SOURCES[@]}"; do
    base="${src%.cpp}"
    echo "  Compiling $src -> $OUTDIR/$base  (+ -lpthread)"
    "$CXX" "${BASE_FLAGS[@]}" "${EXTRA_CXXFLAGS[@]}" -o "$OUTDIR/$base" "$src" -lpthread
done

echo ""
echo "Build complete. Executables with debug symbols in $OUTDIR/"
echo ""
echo "To generate core dumps:"
echo ""
echo "  Linux:"
echo "    ulimit -c unlimited"
echo "    ./$OUTDIR/use-after-free   # core written to current dir or /var/crash/"
echo ""
echo "  macOS (use gen_core_mac.sh — direct execution sends crashes to DiagnosticReports):"
echo "    chmod +x gen_core_mac.sh"
echo "    ./gen_core_mac.sh use-after-free   # core written to $OUTDIR/core.use-after-free"
echo ""
echo "  Windows:"
echo "    run-all.ps1   # dumps written to $OUTDIR\\dumps\\"
echo ""
echo "Complex examples:"
echo "  thread-uaf               — multi-threaded use-after-free (two threads)"
echo "  deep-callchain-nullptr   — null dereference 12+ frames deep in a recursive evaluator"
echo "  heap-metadata-corruption — off-by-one corrupts heap metadata; crash in free()"
echo "  multi-inheritance-crash  — wrong C-style cast across multiple inheritance → vtable crash"
