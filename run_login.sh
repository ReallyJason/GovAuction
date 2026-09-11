#!/usr/bin/env bash
# GovAuctions Automated Login Launcher (macOS / Linux)

# Change to the script's directory regardless of how it was launched
cd "$(dirname "$0")" || exit 1

VENV_DIR=".venv"
PYTHON_BIN="$VENV_DIR/bin/python"

# Check if the virtual environment exists; if not, create it and install requirements
if [ ! -f "$PYTHON_BIN" ]; then
    echo "================================================================="
    echo " GovAuctions: Initial Environment Setup"
    echo "================================================================="
    echo "[*] Virtual environment not found. Creating .venv..."

    # Detect python3 or python
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_CMD="python3"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_CMD="python"
    else
        echo "[-] Error: Python 3 is not installed or not in PATH."
        echo "    Please install Python 3 and retry."
        exit 1
    fi

    echo "[*] Using Python: $($PYTHON_CMD --version) ($($PYTHON_CMD -c 'import sys; print(sys.executable)'))"
    "$PYTHON_CMD" -m venv "$VENV_DIR"
    if [ $? -ne 0 ]; then
        echo "[-] Failed to create virtual environment."
        exit 1
    fi

    echo "[*] Installing dependencies from requirements.txt..."
    "$VENV_DIR/bin/pip" install --upgrade pip
    "$VENV_DIR/bin/pip" install -r requirements.txt
    if [ $? -ne 0 ]; then
        echo "[-] Failed to install requirements."
        exit 1
    fi

    # Ensure Playwright browser binaries are installed if playwright is present
    if [ -f "$VENV_DIR/bin/playwright" ]; then
        echo "[*] Ensuring Playwright browser binaries are ready..."
        "$VENV_DIR/bin/playwright" install chromium
    fi

    echo "[+] Virtual environment and dependencies ready!"
    echo "================================================================="
fi

# Run the GovAuctions monitor script with any passed arguments
"$PYTHON_BIN" govauction_login.py "$@"
