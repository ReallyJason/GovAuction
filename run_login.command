#!/usr/bin/env bash
# GovAuctions Automated Login Launcher (macOS Double-Clickable Command)

# Ensure execution starts from the script's directory
cd "$(dirname "$0")" || exit 1

# Execute the main runner script with any passed arguments
./run_login.sh "$@"
EXIT_CODE=$?

# Pause when running interactively in Terminal so window doesn't close abruptly
if [ -t 0 ]; then
    echo ""
    read -p "Press [Enter] to exit..."
fi

exit $EXIT_CODE
