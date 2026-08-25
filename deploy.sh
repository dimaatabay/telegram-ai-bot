#!/usr/bin/env bash

# Stop immediately if a command fails, an unset variable is used, or a pipeline fails.
set -euo pipefail

PROJECT_DIR="$HOME/telegram-ai-bot"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python"

# Open the bot project directory.
cd "$PROJECT_DIR"

# Update the checked-out main branch without creating a merge commit.
git pull --ff-only origin main

# Install dependencies using the project's virtual environment.
"$VENV_PYTHON" -m pip install --upgrade -r requirements.txt

# Restart the bot only after the update and dependency installation succeed.
sudo systemctl restart stix-bot

# Display the current service state.
sudo systemctl status stix-bot --no-pager
