#!/bin/bash
BOLD=$(tput bold); RESET=$(tput sgr0)
CYAN=$(tput setaf 6); MAGENTA=$(tput setaf 5); DIM=$(tput dim)

echo ""
echo "${BOLD}${CYAN}you${RESET}"
echo "We got a customer crash report with a core dump attached."
echo "Can you tell me what happened?"
sleep 2.4

echo ""
echo "${BOLD}${MAGENTA}assistant${RESET}  ${DIM}via TriagePilot MCP${RESET}"
echo "${DIM}→ analyze_dump(dump_path=\"core.use-after-free\", image_path=\"use-after-free\")${RESET}"
sleep 1.8

glow -w 84 -s auto analysis.md
sleep 1.4

echo "${BOLD}${MAGENTA}assistant${RESET}"
echo "Classic use-after-free. \`dangling\` still points at the Connection"
echo "freed by close_connection() on line 73. By the time line 89 writes"
echo "through it, that memory has been poisoned — grounded in the real"
echo "debugger session, not a guess from the stack trace."
sleep 3.5
