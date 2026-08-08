#!/bin/bash
BOLD=$(tput bold); RESET=$(tput sgr0)
CYAN=$(tput setaf 6); MAGENTA=$(tput setaf 5); DIM=$(tput dim)

echo ""
echo "${BOLD}${CYAN}you${RESET}"
echo "Prod service keeps aborting under load. Here's a core dump — can"
echo "you find the bug?"
sleep 2.4

echo ""
echo "${BOLD}${MAGENTA}assistant${RESET}  ${DIM}via TriagePilot MCP${RESET}"
echo "${DIM}→ analyze_dump(dump_path=\"core.double-free\", image_path=\"double-free\")${RESET}"
sleep 1.8

glow -w 84 -s auto analysis.md
sleep 1.4

echo "${BOLD}${MAGENTA}assistant${RESET}"
echo "The abort chain (abort → malloc_zone_error → free_tiny_botch) is"
echo "libmalloc catching corrupted heap metadata — but the actual bug"
echo "isn't line 59, it's line 46: \`data\` gets freed twice. The second"
echo "free() silently corrupted the heap; it only surfaced once the"
echo "churn loop happened to walk into the poisoned free-list entry."
sleep 3.8
