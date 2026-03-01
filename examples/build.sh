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

SOURCES=(
    stack-overflow.cpp
    use-after-free.cpp
    double-free.cpp
    vtable-corruption.cpp
    stack-buffer-overrun.cpp
    heap-corruption.cpp
)

for src in "${SOURCES[@]}"; do
    base="${src%.cpp}"
    echo "  Compiling $src -> $OUTDIR/$base"
    "$CXX" "${BASE_FLAGS[@]}" "${EXTRA_CXXFLAGS[@]}" -o "$OUTDIR/$base" "$src"
done

echo ""
echo "Build complete. Executables with debug symbols in $OUTDIR/"
echo ""
echo "To generate core dumps, ensure core dumps are enabled:"
echo "  ulimit -c unlimited"
echo ""
echo "Then run an example:"
echo "  ./$OUTDIR/stack-overflow"
echo ""
echo "Core dumps will be written to the current directory or /var/crash/ depending on your system."
