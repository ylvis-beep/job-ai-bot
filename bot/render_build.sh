#!/usr/bin/env bash
# Скрипт билда для Render
set -o errexit

echo "=== Render build script started ==="

APP_DIR=$(pwd)
echo "App dir: $APP_DIR"

# Папка, где будем хранить Chrome между билдами
STORAGE_DIR=/opt/render/project/.render

# --- Установка Chrome без apt-get (через dpkg -x) ---
if [[ ! -d "$STORAGE_DIR/chrome" ]]; then
  echo "...Downloading Chrome"
  mkdir -p "$STORAGE_DIR/chrome"
  cd "$STORAGE_DIR/chrome"

  # Скачиваем deb-пакет Chrome
  wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb

  # Распаковываем содержимое (НЕ system-wide установка)
  dpkg -x ./google-chrome-stable_current_amd64.deb "$STORAGE_DIR/chrome"

  # Удаляем deb, он больше не нужен
  rm ./google-chrome-stable_current_amd64.deb

  cd "$APP_DIR"
else
  echo "...Using Chrome from cache"
fi

# --- Python-зависимости ---
echo "...Upgrading pip"
python -m pip install --upgrade pip

echo "...Installing undetected-chromedriver explicitly"
python -m pip install "undetected-chromedriver==3.5.5"

echo "...Installing dependencies from requirements.txt"
python -m pip install -r requirements.txt

echo "=== Render build script finished ==="
