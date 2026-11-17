"""Сведения о сети машины, на которой запущен балансировщик.

Балансировщик не собирает их сам: в контейнере он видит только сеть контейнера,
а в стандартной библиотеке Python нет способа перечислить интерфейсы с адресами.
Поэтому сведения собирает скрипт scripts/host-info.sh — на самой машине и ДО запуска —
и кладёт в JSON-файл. Балансировщик этот файл только читает и отдаёт через admin API
(GET /v1/system/interfaces).

Это снимок на момент сбора (поле collected_at), а не живые данные.

Формат файла:
    {
      "collected_at": "2026-11-16T22:40:00Z",
      "hostname": "my-server",
      "source": "ip",                      # чем собрано: ip или ifconfig
      "items": [
        {"name": "eth0", "addresses": [{"address": "10.0.1.15", "prefix": 24}]}
      ]
    }
"""
import ipaddress
import json
import re

MAX_SIZE = 256 * 1024          # у машины с сотней интерфейсов файл занимает несколько килобайт
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_NAME = re.compile(r"^[A-Za-z0-9._:@-]{1,64}$")
_HOST = re.compile(r"^[A-Za-z0-9._-]{0,255}$")


class HostInfoMissing(Exception):
    """Файла нет: скрипт сбора не запускали."""


class HostInfoInvalid(Exception):
    """Файл есть, но это не то, что пишет scripts/host-info.sh."""


def _bad(what):
    raise HostInfoInvalid(what)


def load(path: str) -> dict:
    """Читает и проверяет файл. Возвращает только известные поля: что бы ни лежало
    в файле ещё, в ответ API оно не попадёт."""
    if not path:
        raise HostInfoMissing("путь к файлу не задан")
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_SIZE + 1)
    except FileNotFoundError:
        raise HostInfoMissing(path) from None
    except OSError as e:
        raise HostInfoInvalid(f"файл не читается: {e.strerror}") from None
    if len(raw) > MAX_SIZE:
        _bad("файл слишком большой")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        _bad("в файле не JSON")
    if not isinstance(data, dict):
        _bad("ожидался JSON-объект")

    collected_at, hostname, source = data.get("collected_at"), data.get("hostname", ""), data.get("source")
    if not isinstance(collected_at, str) or not _TIME.match(collected_at):
        _bad("нет поля collected_at вида 2026-11-16T22:40:00Z")
    if not isinstance(hostname, str) or not _HOST.match(hostname):
        _bad("неверное поле hostname")
    if source not in ("ip", "ifconfig"):
        _bad("неверное поле source")
    if not isinstance(data.get("items"), list) or len(data["items"]) > 1000:
        _bad("нет списка items")

    items = []
    for it in data["items"]:
        if not isinstance(it, dict) or not isinstance(it.get("name"), str) or not _NAME.match(it["name"]):
            _bad("неверное имя интерфейса")
        if not isinstance(it.get("addresses"), list) or len(it["addresses"]) > 1000:
            _bad(f"у интерфейса {it['name']} нет списка addresses")
        addresses = []
        for a in it["addresses"]:
            if not isinstance(a, dict):
                _bad(f"неверный адрес у интерфейса {it['name']}")
            addr, prefix = a.get("address"), a.get("prefix")
            try:
                ipaddress.IPv4Address(addr)
            except ValueError:
                _bad(f"неверный IPv4-адрес у интерфейса {it['name']}")
            if isinstance(prefix, bool) or not isinstance(prefix, int) or not 0 <= prefix <= 32:
                _bad(f"неверная длина префикса у интерфейса {it['name']}")
            addresses.append({"address": addr, "prefix": prefix})
        items.append({"name": it["name"], "addresses": addresses})
    return {"collected_at": collected_at, "hostname": hostname, "source": source, "items": items}
