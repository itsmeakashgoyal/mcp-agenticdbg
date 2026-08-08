#!/bin/bash
BOLD=$(tput bold); RESET=$(tput sgr0)
CYAN=$(tput setaf 6); MAGENTA=$(tput setaf 5); DIM=$(tput dim)

echo ""
echo "${BOLD}${CYAN}you${RESET}"
echo "This one only crashes under concurrency, never in a single-"
echo "threaded repro. Core dump attached — what's racing?"
sleep 2.6

echo ""
echo "${BOLD}${MAGENTA}assistant${RESET}  ${DIM}via TriagePilot MCP${RESET}"
echo "${DIM}→ analyze_dump(dump_path=\"core.thread-uaf\", image_path=\"thread-uaf\")${RESET}"
sleep 1.8

glow -w 84 -s auto analysis.md
sleep 1.4

echo "${BOLD}${MAGENTA}assistant${RESET}"
echo "Two threads, and the crash is only meaningful next to both stacks:"
echo "the watchdog thread already ran SessionPool::close(), which"
echo "deletes the Session — but the worker thread is still holding a"
echo "raw pointer to it and touches it right after, in log_request()."
echo "Classic cross-thread use-after-free with no ownership handoff."
sleep 4
