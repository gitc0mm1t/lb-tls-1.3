#!/usr/bin/env bash
# Запускает 3 тестовых бэкенда на портах 9001-9003. Ctrl+C останавливает все.
set -euo pipefail
trap 'kill 0' EXIT
for i in 1 2 3; do
  python3 demo_backend.py --addr "127.0.0.1:900$i" --name "b$i" &
done
wait
