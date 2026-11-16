#!/usr/bin/env bash
# Добавляет и убирает тестовые бэкенды в стенде Docker, не трогая docker-compose.yml.
#
#   bash scripts/backend.sh add b5              # запустить сервер b5 и добавить его в балансировщик
#   bash scripts/backend.sh add b5 b6 shop      # сразу несколько, имена любые
#   bash scripts/backend.sh remove b5           # убрать из балансировщика и удалить контейнер
#   bash scripts/backend.sh list                # что сейчас в пуле балансировщика
#
# "Добавить бэкенд" — это два действия, и скрипт делает оба по очереди:
#   1. docker run: запускает контейнер с сервером. Имя контейнера = его адрес в docker-сети.
#   2. POST /v1/backends: сообщает балансировщику адрес http://ИМЯ:9000.
# При удалении порядок обратный: сначала DELETE в балансировщике, потом контейнер,
# чтобы клиенты не попадали на уже остановленный сервер.
#
# Стенд должен быть запущен (docker compose up -d). Токен берётся из переменной
# MYLB_ADMIN_TOKEN, а если её нет — из файла .env в папке проекта.
set -euo pipefail
cd "$(dirname "$0")/.."

ADMIN="${MYLB_ADMIN_URL:-http://127.0.0.1:9101}/v1"
LABEL="mylb.extra-backend=1"     # метка на контейнерах, созданных этим скриптом

die() { echo "ошибка: $*" >&2; exit 1; }

usage() { sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

load_token() {
  if [[ -z "${MYLB_ADMIN_TOKEN:-}" && -f .env ]]; then
    MYLB_ADMIN_TOKEN="$(sed -n 's/^MYLB_ADMIN_TOKEN=//p' .env | tail -n 1)"
  fi
  [[ -n "${MYLB_ADMIN_TOKEN:-}" ]] || die "нет токена: задайте MYLB_ADMIN_TOKEN или создайте .env (см. .env.example)"
  export MYLB_ADMIN_TOKEN        # нужен и docker compose: без него compose-файл не читается
}

# api МЕТОД ПУТЬ [ТЕЛО] — печатает тело ответа, а последней строкой HTTP-код.
api() {
  local args=(-sS -X "$1" -H "Authorization: Bearer $MYLB_ADMIN_TOKEN" -w '\n%{http_code}')
  [[ $# -ge 3 ]] && args+=(-H "Content-Type: application/json" -d "$3")
  curl "${args[@]}" "$ADMIN$2" || die "admin API не отвечает по адресу $ADMIN (стенд запущен?)"
}

check_name() {
  # Имя станет DNS-именем контейнера, поэтому только строчные латинские буквы, цифры и дефис.
  [[ "$1" =~ ^[a-z][a-z0-9-]{0,39}$ ]] || die "имя '$1' не подходит: нужны строчные латинские буквы, цифры и дефис, первая — буква"
}

# id бэкенда в балансировщике по его адресу (пусто, если такого нет)
backend_id() {
  local out; out="$(api GET /backends)"
  [[ "$(tail -n 1 <<<"$out")" == 200 ]] || die "не удалось получить список бэкендов: $(sed '$d' <<<"$out")"
  sed '$d' <<<"$out" | python3 -c '
import json, sys
for b in json.load(sys.stdin)["items"]:
    if b["url"] == sys.argv[1]:
        print(b["id"])' "$1"
}

add() {
  local name="$1" url="http://$1:9000" lb net image out code
  check_name "$name"
  # Сервисы из docker-compose.yml (b1, lb, grafana...) уже заняли свои имена в сети.
  if docker compose config --services | grep -qx "$name"; then
    die "'$name' — это сервис из docker-compose.yml, он уже запущен. Добавьте его запросом: POST /v1/backends {\"url\": \"$url\"}"
  fi
  lb="$(docker compose ps -q lb)"
  [[ -n "$lb" ]] || die "балансировщик не запущен: сначала docker compose up -d"
  # Сеть и образ берём у работающего балансировщика, а не записываем их имена здесь.
  net="$(docker inspect -f '{{range $k, $_ := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' "$lb" | head -n 1)"
  image="$(docker inspect -f '{{.Config.Image}}' "$lb")"

  if docker container inspect "$name" >/dev/null 2>&1; then
    [[ "$(docker inspect -f '{{index .Config.Labels "mylb.extra-backend"}}' "$name")" == 1 ]] \
      || die "контейнер '$name' уже есть и создан не этим скриптом; выберите другое имя"
    docker start "$name" >/dev/null       # уже был создан раньше — просто убеждаемся, что работает
    echo "$name: контейнер уже есть"
  else
    docker run -d --name "$name" --network "$net" --label "$LABEL" --init --restart unless-stopped \
      --entrypoint python "$image" demo_backend.py --addr 0.0.0.0:9000 --name "$name" >/dev/null
    echo "$name: контейнер запущен (сеть $net)"
  fi

  # Ждём, пока сервер начнёт отвечать: иначе балансировщик добавит его как "alive": false.
  for _ in $(seq 50); do
    docker exec "$name" python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/healthz', timeout=1)" 2>/dev/null && break
    sleep 0.2
  done

  out="$(api POST /backends "{\"url\": \"$url\"}")"
  code="$(tail -n 1 <<<"$out")"
  case "$code" in
    201) echo "$name: добавлен в балансировщик: $(sed '$d' <<<"$out")" ;;
    409) echo "$name: в балансировщике уже есть ($url)" ;;
    *)   die "$name: балансировщик ответил $code: $(sed '$d' <<<"$out")" ;;
  esac
}

remove() {
  local name="$1" url="http://$1:9000" id out code
  check_name "$name"
  id="$(backend_id "$url")"
  if [[ -n "$id" ]]; then
    out="$(api DELETE "/backends/$id")"
    code="$(tail -n 1 <<<"$out")"
    [[ "$code" == 204 ]] || die "$name: балансировщик ответил $code: $(sed '$d' <<<"$out")"
    echo "$name: удалён из балансировщика"
  else
    echo "$name: в балансировщике его и не было"
  fi
  # Контейнер удаляем, только если его создал этот скрипт: b1-b4 из compose не трогаем.
  if [[ "$(docker inspect -f '{{index .Config.Labels "mylb.extra-backend"}}' "$name" 2>/dev/null)" == 1 ]]; then
    docker rm -f "$name" >/dev/null
    echo "$name: контейнер удалён"
  else
    echo "$name: контейнер не тронут (он создан не этим скриптом)"
  fi
}

cmd="${1:-}"; shift || true
case "$cmd" in
  add|remove)
    [[ $# -ge 1 ]] || usage
    load_token
    for name in "$@"; do "$cmd" "$name"; done ;;
  list)
    load_token
    out="$(api GET /backends)"
    [[ "$(tail -n 1 <<<"$out")" == 200 ]] || die "балансировщик ответил $(tail -n 1 <<<"$out"): $(sed '$d' <<<"$out")"
    sed '$d' <<<"$out" | python3 -c '
import json, sys
for b in json.load(sys.stdin)["items"]:
    print(b["id"], b["url"], b["state"], "alive" if b["alive"] else "DOWN")' ;;
  *) usage ;;
esac
