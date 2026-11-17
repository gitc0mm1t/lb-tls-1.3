#!/usr/bin/env bash
# Запуск стенда в Docker "по-правильному":
#   1. собирает сведения о сети этой машины (scripts/host-info.sh → hostinfo/host.json);
#   2. проверяет LB_BIND: есть ли на машине такой адрес;
#   3. определяет основной адрес машины и передаёт его в контейнер переменной HOST_IP;
#   4. запускает docker compose up -d.
#
#   bash scripts/up.sh            # обычный запуск
#   bash scripts/up.sh --build    # аргументы передаются в docker compose up
#
# Простой `docker compose up -d` тоже работает, но тогда сведения о сети не обновятся,
# а ошибка в LB_BIND всплывёт уже внутри Docker, менее понятным сообщением.
set -euo pipefail
cd "$(dirname "$0")/.."

rows="$(bash scripts/host-info.sh)"
echo "сетевые интерфейсы этой машины:"
sed 's/^/  /' <<<"$rows"

# LB_BIND берём оттуда же, откуда его возьмёт compose: из окружения, а если там нет — из .env.
bind="${LB_BIND:-}"
if [[ -z "$bind" && -f .env ]]; then
  bind="$(sed -n 's/^LB_BIND=//p' .env | tail -n 1 | tr -d ' "\r'"'")"
fi
if [[ -z "$bind" ]]; then
  echo "LB_BIND не задан: балансировщик будет доступен только с этой машины (127.0.0.1:8443)"
elif [[ "$bind" == 0.0.0.0 ]]; then
  echo "LB_BIND=0.0.0.0: балансировщик будет доступен на всех интерфейсах, порт 8443"
elif awk -v ip="$bind" '{ split($2, a, "/"); if (a[1] == ip) found = 1 } END { exit !found }' <<<"$rows"; then
  echo "LB_BIND=$bind: такой адрес на машине есть"
else
  echo "ошибка: LB_BIND=$bind, но на этой машине такого адреса нет (список выше)." >&2
  echo "Исправьте LB_BIND в .env или уберите строку, чтобы слушать только на 127.0.0.1." >&2
  exit 1
fi

# Основной адрес машины для переменной HOST_IP в контейнере. "Основной" — тот, с которого
# машина ходит в сеть по умолчанию. Если HOST_IP уже задан (в окружении или в .env), не трогаем.
host_ip="${HOST_IP:-}"
if [[ -z "$host_ip" && -f .env ]]; then
  host_ip="$(sed -n 's/^HOST_IP=//p' .env | tail -n 1 | tr -d ' "\r'"'")"
fi
if [[ -z "$host_ip" ]]; then
  if command -v ip >/dev/null 2>&1; then
    # Linux:  1.1.1.1 via 10.0.1.1 dev eth0 src 10.0.1.15 uid 1000   → берём слово после src
    host_ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{ for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit } }' || true)"
  elif command -v route >/dev/null 2>&1 && command -v ipconfig >/dev/null 2>&1; then
    # macOS: узнаём интерфейс маршрута по умолчанию (обычно en0) и его адрес
    dev="$(route -n get default 2>/dev/null | awk '$1 == "interface:" { print $2 }' || true)"
    [[ -n "$dev" ]] && host_ip="$(ipconfig getifaddr "$dev" 2>/dev/null || true)"
  fi
  # Запасной вариант: первый адрес из списка, кроме 127.x.x.x
  if [[ -z "$host_ip" ]]; then
    host_ip="$(awk '{ split($2, a, "/"); if (a[1] !~ /^127\./) { print a[1]; exit } }' <<<"$rows")"
  fi
fi
if [[ "$host_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "HOST_IP=$host_ip: этот адрес увидит балансировщик в переменной окружения HOST_IP"
  export HOST_IP="$host_ip"
else
  echo "HOST_IP определить не удалось: в контейнере переменная будет пустой"
  export HOST_IP=""
fi

exec docker compose up -d "$@"
