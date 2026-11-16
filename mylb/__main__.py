"""Запуск: python3 -m mylb --help"""
import argparse
import os
import signal
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from . import state
from .admin import AdminServer
from .backend import Backend
from .logs import log
from .pool import Pool
from .proxy import MetricsHandler, ProxyServer
from .state import PersistError
from .tlsconf import CertError, CertStore


def parse_addr(s: str):
    host, _, port = s.rpartition(":")
    return host, int(port)


def main() -> None:
    ap = argparse.ArgumentParser(prog="mylb", description="Учебный балансировщик нагрузки")
    ap.add_argument("--listen", default="0.0.0.0:8443", help="адрес HTTPS-листенера")
    ap.add_argument("--metrics", default="127.0.0.1:9100", help="адрес для /metrics (Prometheus)")
    ap.add_argument("--admin", default="127.0.0.1:9101",
                    help="адрес admin API; включается переменной окружения MYLB_ADMIN_TOKEN")
    ap.add_argument("--state-file", default="mylb-state.json",
                    help="файл, в который admin API сохраняет бэкенды и настройки")
    ap.add_argument("--cert", default="certs/server.crt", help="TLS-сертификат")
    ap.add_argument("--key", default="certs/server.key", help="приватный ключ")
    # Списка бэкендов по умолчанию нет: адреса серверов не зашиты ни в код, ни в конфиг.
    # Основной способ — admin API (POST /v1/backends). Флаг оставлен для запуска без него.
    ap.add_argument("--backends", default="",
                    help="необязательно: бэкенды через запятую для запуска без admin API; "
                         "с admin API бэкенды добавляют и удаляют запросами, а хранит их файл состояния")
    ap.add_argument("--health-path", default="/healthz", help="путь health-check'а на бэкендах")
    ap.add_argument("--health-interval", type=float, default=2.0, help="как часто проверять бэкенды, с")
    ap.add_argument("--max-conns", type=int, default=100,
                    help="макс. одновременных запросов на один бэкенд (порог 'загруженности')")
    args = ap.parse_args()

    # Токен берём из окружения, а не из аргументов: аргументы любого процесса
    # видны всем пользователям машины через `ps`.
    token = os.environ.get("MYLB_ADMIN_TOKEN", "")

    # Откуда берём бэкенды и настройки. С admin API главный источник — файл состояния:
    # в него попадает всё, что добавили и удалили запросами, поэтому изменения переживают
    # перезапуск. При первом запуске файла ещё нет, и пул пуст (если не задан --backends).
    # Без admin API остаются только флаги.
    entries = [{"url": u.strip(), "state": "active"} for u in args.backends.split(",") if u.strip()]
    settings = {"max_conns": args.max_conns, "health_path": args.health_path,
                "health_interval": args.health_interval}
    saved = None
    if token:
        try:
            saved = state.load(args.state_file)
        except (OSError, ValueError) as e:
            log("error", "файл состояния не читается; исправьте или удалите его", file=args.state_file, err=str(e))
            sys.exit(1)
        if saved is not None:
            entries, settings = saved["backends"], saved["settings"]
            log("info", "состояние загружено из файла; флаги --backends, --max-conns, "
                        "--health-path и --health-interval не используются", file=args.state_file)

    try:
        pool = Pool([], settings["max_conns"], settings["health_path"], settings["health_interval"])
        for e in entries:
            b = Backend(e["url"])
            b.state = e["state"]
            pool.add(b)
        if not pool.backends and not token:  # с admin API пустой пул допустим: бэкенды добавят позже
            raise ValueError("нет ни одного бэкенда. Включите admin API (переменная окружения "
                             "MYLB_ADMIN_TOKEN) и добавьте бэкенды запросом POST /v1/backends, "
                             "либо передайте список флагом --backends")
    except ValueError as e:
        log("error", "не удалось создать пул", err=str(e))
        sys.exit(1)

    if token and saved is None:
        # Первый запуск с admin API: создаём файл сразу. Если писать в него нельзя,
        # лучше узнать об этом сейчас, чем при первом изменении.
        try:
            state.save(args.state_file, state.snapshot(pool))
        except PersistError as e:
            log("error", "не удалось запуститься", err=str(e))
            sys.exit(1)

    # Первую проверку делаем синхронно, чтобы не принимать трафик вслепую.
    pool.check_all()
    stop = threading.Event()
    threading.Thread(target=pool.health_loop, args=(stop,), daemon=True).start()

    # TLS 1.3 и сертификат: см. tlsconf.py. CertStore умеет менять сертификат на лету.
    try:
        certs = CertStore(args.cert, args.key)
    except CertError as e:
        log("error", "не удалось запуститься: сертификат не загружен", reason=e.code, err=e.detail)
        sys.exit(1)
    try:
        server = ProxyServer(parse_addr(args.listen), pool, certs)
        msrv = ThreadingHTTPServer(parse_addr(args.metrics), MetricsHandler)
        asrv = AdminServer(parse_addr(args.admin), pool, certs, args.state_file, token) if token else None
    except (OSError, ValueError) as e:
        log("error", "не удалось запуститься", err=str(e))
        sys.exit(1)
    msrv.daemon_threads = True
    threading.Thread(target=msrv.serve_forever, daemon=True).start()
    if asrv:
        threading.Thread(target=asrv.serve_forever, daemon=True).start()
        if parse_addr(args.admin)[0] not in ("127.0.0.1", "localhost", "::1"):
            # Внутри docker-сети это нормально, в обычной сети — нет.
            log("warn", "admin API слушает не на loopback: трафик не зашифрован, "
                        "не открывайте этот порт в обычную сеть", admin=args.admin)
    else:
        log("info", "admin API выключен: не задана переменная окружения MYLB_ADMIN_TOKEN")

    def on_signal(signum, frame):
        log("info", "останавливаемся...")
        # shutdown() нельзя звать из потока, где крутится serve_forever (это главный) — зависнет.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, on_signal)    # Ctrl+C
    signal.signal(signal.SIGTERM, on_signal)   # так останавливают контейнеры и systemd

    log("info", "балансировщик запущен", listen=args.listen, metrics=args.metrics,
        admin=args.admin if asrv else "off", backends=len(pool.backends), max_conns=pool.max_conns)
    if not pool.backends:
        log("warn", "пул пуст: клиенты будут получать 503, пока вы не добавите бэкенд "
                    "запросом POST /v1/backends", admin=args.admin)
    server.serve_forever()
    server.server_close()  # новых соединений больше не принимаем

    # Graceful shutdown: даём текущим запросам до 15 секунд спокойно доработать.
    deadline = time.monotonic() + 15
    while server.inflight > 0 and time.monotonic() < deadline:
        time.sleep(0.1)
    stop.set()
    msrv.shutdown()
    if asrv:
        asrv.shutdown()
    log("info", "остановлен")


if __name__ == "__main__":
    main()
