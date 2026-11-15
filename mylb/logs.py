"""Структурированные логи в JSON: одна строка = одно событие.

Такие логи легко парсить и отправлять в Loki / ELK. Позже можно заменить
на стандартный модуль logging с JSON-форматтером — суть не изменится.
"""
import json
import sys
import threading
from datetime import datetime, timezone

_lock = threading.Lock()  # чтобы строки от разных потоков не перемешивались


def log(level: str, msg: str, **fields) -> None:
    rec = {
        "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level,
        "msg": msg,
    }
    rec.update(fields)
    line = json.dumps(rec, ensure_ascii=False)
    with _lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
