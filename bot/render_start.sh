#!/usr/bin/env bash
# Скрипт старта для Render
set -o errexit

# Путь к распакованному Chrome (после dpkg -x)
export CHROME_BINARY_PATH="/opt/render/project/.render/chrome/opt/google/chrome/google-chrome"
export PATH="/opt/render/project/.render/chrome/opt/google/chrome:$PATH"

echo "Using Chrome binary at: $CHROME_BINARY_PATH"
echo "Current PATH: $PATH"

# На всякий случай говорим коду, что он на Render
export IS_RENDER="true"

# Запуск бота
python main.py
