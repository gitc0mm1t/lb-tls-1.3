"""Файл состояния: то, что поменяли через admin API, должно пережить перезапуск.

В файле лежат список бэкендов и настройки — всё, что admin API умеет менять,
кроме сертификата (у него свои файлы, --cert и --key). Формат — JSON:

    {
      "version": 1,
      "backends": [{"url": "http://127.0.0.1:9001", "state": "active"}],
      "settings": {"max_conns": 100, "health_path": "/healthz", "health_interval": 2.0}
    }

Здесь же живут проверки значений: одни и те же правила нужны и admin API
(проверить запрос), и загрузке файла при старте (вдруг его правили руками).
"""
import json
import math
import os
import re

VERSION = 1
BACKEND_STATES = ("active", "draining")
BACKEND_URL = re.compile(r"^http://[^/?#]+$")
HEALTH_PATH = re.compile(r"^/[!-~]*$")  # начинается с "/", дальше печатные ASCII без пробелов
SETTINGS_KEYS = ("max_conns", "health_path", "health_interval")


class PersistError(Exception):
    """Не удалось записать изменение на диск."""


# --- проверки: возвращают список (поле, что не так); пустой список = всё хорошо ---

def _unknown(data: dict, allowed) -> list:
    return [(k, "Неизвестное поле.") for k in data if k not in allowed]


def _finite(v) -> bool:
    try:
        return math.isfinite(v)
    except OverflowError:  # целое настолько большое, что не помещается во float
        return False


def check_backend(data: dict) -> list:
    errors = _unknown(data, ("url", "state"))
    url = data.get("url")
    if url is None:
        errors.append(("url", "Обязательное поле."))
    elif not isinstance(url, str) or not BACKEND_URL.match(url):
        errors.append(("url", "Нужен вид http://host:port, без пути. Схема https не поддерживается."))
    if "state" in data and data["state"] not in BACKEND_STATES:
        errors.append(("state", "Допустимые значения: active, draining."))
    return errors


def check_settings(data: dict, partial: bool) -> list:
    """partial=True — это PATCH: поля необязательны, но хотя бы одно быть должно."""
    errors = _unknown(data, SETTINGS_KEYS)
    if partial and not data:
        errors.append(("", "Нужно хотя бы одно поле: max_conns, health_path или health_interval."))
    for key in SETTINGS_KEYS:
        if key not in data:
            if not partial:
                errors.append((key, "Обязательное поле."))
            continue
        v = data[key]
        # bool в Python — подкласс int, поэтому True без этой проверки сошло бы за число 1.
        is_num = isinstance(v, (int, float)) and not isinstance(v, bool)
        if key == "max_conns" and not (is_num and isinstance(v, int) and v >= 1):
            errors.append((key, "Нужно целое число не меньше 1."))
        elif key == "health_path" and not (isinstance(v, str) and HEALTH_PATH.match(v)):
            errors.append((key, "Нужна строка, которая начинается с / и не содержит пробелов."))
        elif key == "health_interval" and not (is_num and _finite(v) and v >= 0.1):
            errors.append((key, "Нужно число не меньше 0.1 (секунды)."))
    return errors


# --- снимок текущего состояния ---

def snapshot(pool) -> dict:
    return {
        "version": VERSION,
        "backends": [{"url": b.url, "state": b.state} for b in pool.backends],
        "settings": {
            "max_conns": pool.max_conns,
            "health_path": pool.health_path,
            "health_interval": pool.health_interval,
        },
    }


# --- чтение и запись ---

def load(path: str):
    """Прочитать файл состояния. None — файла ещё нет. ValueError — файл испорчен."""
    try:
        f = open(path, encoding="utf-8")
    except FileNotFoundError:
        return None
    with f:
        data = json.load(f)  # битый JSON даст ValueError
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise ValueError(f"ожидается объект с version={VERSION}")
    backends, settings = data.get("backends"), data.get("settings")
    if not isinstance(backends, list) or not isinstance(settings, dict):
        raise ValueError("нужны поля backends (список) и settings (объект)")
    problems = check_settings(settings, partial=False)
    for i, entry in enumerate(backends):
        if not isinstance(entry, dict):
            raise ValueError(f"backends[{i}]: ожидается объект")
        problems += [(f"backends[{i}].{field}", msg) for field, msg in check_backend(entry)]
    if problems:
        raise ValueError("; ".join(f"{field}: {msg}" for field, msg in problems))
    for entry in backends:
        entry.setdefault("state", "active")
    return data


def save(path: str, data: dict) -> None:
    """Записать состояние атомарно.

    Пишем во временный файл рядом и переименовываем его поверх старого.
    Переименование в пределах одной файловой системы атомарно: при сбое
    питания на диске окажется либо старый файл целиком, либо новый целиком,
    но не половина нового.
    """
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())  # дожидаемся, что данные действительно на диске
        os.replace(tmp, path)
    except OSError as e:
        raise PersistError(f"Не удалось записать файл состояния {path}: {e.strerror or e}") from e
