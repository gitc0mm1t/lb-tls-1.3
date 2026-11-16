#!/usr/bin/env bash
# Запускает N тестовых бэкендов на портах 9001, 9002, ... (по умолчанию 3). Ctrl+C останавливает все.
#   bash scripts/demo-backends.sh        # b1-b3 на портах 9001-9003
#   bash scripts/demo-backends.sh 5      # b1-b5 на портах 9001-9005
# Балансировщик о них ничего не знает: каждый нужно добавить через admin API (см. README).
set -euo pipefail
n="${1:-3}"
if ! [[ "$n" =~ ^[0-9]+$ ]] || (( n < 1 || n > 99 )); then
  echo "нужно число от 1 до 99, а передано: $n" >&2   # порт 9100 уже занят метриками балансировщика
  exit 2
fi
trap 'kill 0' EXIT
for i in $(seq "$n"); do
  python3 demo_backend.py --addr "127.0.0.1:$((9000 + i))" --name "b$i" &
done
wait
