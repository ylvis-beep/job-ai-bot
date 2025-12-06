#!/usr/bin/env bash
# exit on error
set -o errexit

echo "=== Render build script started ==="

# текущая папка приложения (bot)
APP_DIR=$(pwd)
echo "App dir: $APP_DIR"

# Папка для кеша Chrome на Render
STORAGE_DIR=/opt/render/project/.render

# --- Установка Chrome (без apt-get) ---
if [[ ! -d "$STORAGE_DIR/chrome" ]]; then
  echo "...Downloading Chrome"
  mkdir -p "$STORAGE_DIR/chrome"
  cd "$STORAGE_DIR/chrome"

  # Скачиваем deb-пакет Chrome
  wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb

  # Распаковываем его в STORAGE_DIR/chrome
  dpkg -x ./google-chrome-stable_current_amd64.deb "$STORAGE_DIR/chrome"

  # Удаляем deb, он больше не нужен
  rm ./google-chrome-stable_current_amd64.deb

  # Возвращаемся в директорию приложения
  cd "$APP_DIR"
else
  echo "...Using Chrome from cache"
fi

# --- Установка Python-зависимостей ---
echo "...Installing Python dependencies"
pip install --upgrade pip
pip install -r requirements.txt

echo "=== Render build script finished ==="

