#!/usr/bin/env bash
# Собирает сведения о сети ЭТОЙ машины (интерфейсы и их IPv4-адреса) и кладёт их в файл
# hostinfo/host.json. Балансировщик читает файл и отдаёт через admin API:
#
#   GET /v1/system/interfaces
#
# Зачем отдельный скрипт: балансировщик в контейнере видит только сеть контейнера,
# а этот скрипт работает на самой машине и до запуска. Файл — снимок на момент сбора;
# адреса поменялись — запустите скрипт ещё раз, перезапускать балансировщик не нужно.
#
#   bash scripts/host-info.sh              # записать hostinfo/host.json и показать найденное
#   bash scripts/host-info.sh /tmp/h.json  # записать в другой файл
#
# На Linux сведения берутся у команды ip, на macOS — у ifconfig.
# В stdout печатаются строки "интерфейс адрес/префикс", всё остальное идёт в stderr.
set -euo pipefail
cd "$(dirname "$0")/.."
out="${1:-hostinfo/host.json}"

# Шаг 1. Приводим вывод ip или ifconfig к одному виду: "имя адрес префикс" на строку.
if command -v ip >/dev/null 2>&1; then
  source_name=ip
  # Пример строки:  2: eth0    inet 10.0.1.15/24 brd 10.0.1.255 scope global eth0
  rows="$(ip -o -4 addr show | awk '{ split($4, a, "/"); print $2, a[1], a[2] }')"
elif command -v ifconfig >/dev/null 2>&1; then
  source_name=ifconfig
  # Пример:  en0: flags=8863<UP,...> mtu 1500
  #              inet 192.168.50.173 netmask 0xffffff00 broadcast 192.168.50.255
  rows="$(ifconfig | awk '
    # Маска бывает шестнадцатеричной (0xffffff00, macOS) и обычной (255.255.255.0).
    # Длина префикса = сколько в маске единичных битов.
    function bits(m,    n, i, c, parts, k, v) {
      n = 0
      if (m ~ /^0x/) {
        m = tolower(substr(m, 3))
        for (i = 1; i <= length(m); i++) {
          c = substr(m, i, 1)
          n += (c == "f") ? 4 : (c == "e") ? 3 : (c == "c") ? 2 : (c == "8") ? 1 : 0
        }
        return n
      }
      k = split(m, parts, ".")
      if (k != 4) return -1
      for (i = 1; i <= 4; i++) { v = parts[i] + 0; while (v > 0) { n += v % 2; v = int(v / 2) } }
      return n
    }
    /^[^ \t]/ { name = $1; sub(/:$/, "", name) }          # строка без отступа — начало интерфейса
    $1 == "inet" {
      addr = $2; sub(/^addr:/, "", addr); mask = ""
      for (i = 3; i <= NF; i++) {
        if ($i == "netmask") mask = $(i + 1)
        if ($i ~ /^Mask:/) mask = substr($i, 6)
      }
      p = bits(mask)
      if (p >= 0) print name, addr, p
    }')"
else
  echo "ошибка: не найдены ни ip, ни ifconfig — нечем узнать адреса" >&2
  exit 1
fi

# Шаг 2. Собираем JSON. Всё, что не похоже на имя интерфейса и IPv4-адрес, пропускаем.
host="$(hostname 2>/dev/null | tr -cd 'A-Za-z0-9._-' || true)"
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
json="$(awk -v host="$host" -v now="$now" -v src="$source_name" '
  $1 ~ /^[A-Za-z0-9._:@-]+$/ && $2 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ && $3 ~ /^[0-9]+$/ && $3 <= 32 {
    if (!($1 in seen)) { seen[$1] = 1; order[++n] = $1 }
    list[$1] = list[$1] (list[$1] == "" ? "" : ", ") "{\"address\": \"" $2 "\", \"prefix\": " ($3 + 0) "}"
  }
  END {
    printf "{\n  \"collected_at\": \"%s\",\n  \"hostname\": \"%s\",\n  \"source\": \"%s\",\n  \"items\": [", now, host, src
    for (i = 1; i <= n; i++)
      printf "%s\n    {\"name\": \"%s\", \"addresses\": [%s]}", (i > 1 ? "," : ""), order[i], list[order[i]]
    printf "%s]\n}\n", (n ? "\n  " : "")
  }' <<<"$rows")"

# Шаг 3. Пишем файл целиком: сначала во временный, потом переименовываем. Так балансировщик
# никогда не прочитает наполовину записанный файл.
mkdir -p "$(dirname "$out")"
tmp="$out.tmp.$$"
printf '%s\n' "$json" > "$tmp"
chmod 644 "$tmp"                 # в контейнере файл читает другой пользователь (app)
mv "$tmp" "$out"

echo "сведения о сети машины записаны в $out ($now, источник: $source_name)" >&2
awk 'NF == 3 { print $1, $2 "/" $3 }' <<<"$rows"
