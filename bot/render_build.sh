#!/usr/bin/env bash
set -e

apt-get update
apt-get install -y wget gnupg

wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
apt-get install -y ./google-chrome-stable_current_amd64.deb
rm google-chrome-stable_current_amd64.deb

pip install -r requirements.txt

