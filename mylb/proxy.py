"""HTTP-слой: принимает HTTPS-запрос, выбирает бэкенд, проксирует, пишет лог и метрики."""
import http.client
import ssl
import sys
import threading
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import metrics
from .logs import log

STICKY_COOKIE = "LB_BACKEND"
MAX_BODY = 10 * 1024 * 1024  # больше 10 МБ в теле запроса не принимаем

# Заголовки, которые относятся к одному TCP-соединению, а не к запросу целиком:
# прокси обязан их НЕ пересылать дальше (RFC 9110).
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "proxy-connection"}


def _conn_tokens(value) -> set:
    """Заголовок Connection может перечислять и другие заголовки, которые тоже нужно срезать."""
    return {t.strip().lower() for t in (value or "").split(",") if t.strip()}


class ProxyServer(ThreadingHTTPServer):
    """Многопоточный сервер: на каждое соединение — свой поток. Просто и надёжно
    для обучения; под большую нагрузку берут asyncio."""
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, addr, pool, certs):
        self.pool = pool
        self.certs = certs  # CertStore: в нём лежит TLS-контекст, который можно подменить на лету
        self.inflight = 0  # запросов в обработке — нужно для graceful shutdown
        self._inflight_lock = threading.Lock()
        super().__init__(addr, ProxyHandler)

    def inflight_add(self, n: int) -> None:
        with self._inflight_lock:
            self.inflight += n

    def get_request(self):
        sock, addr = self.socket.accept()
        # TLS-рукопожатие НЕ делаем здесь: этот метод крутится в главном потоке,
        # и один медленный клиент заблокировал бы приём всех остальных.
        # Рукопожатие пройдёт позже, в потоке соединения (см. ProxyHandler.setup).
        # Контекст читаем заново на каждое соединение: если сертификат только что
        # заменили, новое соединение получит уже новый. Старые соединения держат
        # ссылку на свой прежний контекст и спокойно дорабатывают с ним.
        tls = self.certs.ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return tls, addr

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ssl.SSLError, ConnectionError, TimeoutError)):
            # Клиент с TLS 1.2, мусор вместо TLS, обрыв связи — это норма жизни, а не авария.
            log("info", "соединение отклонено", remote=client_address[0], err=str(exc))
            return
        super().handle_error(request, client_address)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: одно соединение — много запросов
    server_version = "mylb"
    sys_version = ""
    timeout = 30  # простаивающее соединение закрываем через 30 с

    def setup(self):
        self.request.settimeout(10)  # на рукопожатие даём 10 с
        self.request.do_handshake()  # клиент с TLS < 1.3 упадёт именно здесь
        super().setup()

    def log_message(self, format, *args):  # отключаем стандартный лог в stderr — у нас свой
        pass

    # --- вход ---

    def _handle(self):
        srv = self.server
        start = time.monotonic()
        srv.inflight_add(1)
        status, sent, name, sticky = 500, 0, "none", False
        try:
            status, sent, name, sticky = self._route()
        except OSError:  # клиент пропал посреди запроса
            status = 499
            self.close_connection = True
        except Exception as e:  # noqa: BLE001 — последняя линия обороны
            log("error", "необработанная ошибка", err=repr(e))
            self.close_connection = True
        finally:
            srv.inflight_add(-1)

        dur = time.monotonic() - start
        metrics.requests_total.inc(name, str(status))
        metrics.request_duration.observe(dur, name)
        # Пишем только path, а не весь URL: в query-строке бывают токены и пароли.
        log("info", "request",
            method=self.command, host=self.headers.get("Host", ""), path=urlsplit(self.path).path,
            status=status, backend=name, sticky=sticky, bytes=sent,
            duration_ms=round(dur * 1000),
            remote=f"{self.client_address[0]}:{self.client_address[1]}",
            proto=self.request_version, tls=self.connection.version())

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _handle

    # --- логика ---

    def _route(self):
        """Возвращает (код, отправлено_байт, имя_бэкенда, sticky)."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            return (*self._simple(411, "chunked-тело запроса не поддерживается\n", close=True), "none", False)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError
        except ValueError:
            return (*self._simple(400, "некорректный Content-Length\n", close=True), "none", False)
        if length > MAX_BODY:
            return (*self._simple(413, "тело запроса слишком большое\n", close=True), "none", False)

        # Тело читаем ДО выбора бэкенда: медленная загрузка не должна занимать его слот.
        body = None
        if length:
            body = self.rfile.read(length)
            if len(body) != length:
                self.close_connection = True
                return 400, 0, "none", False

        backend, sticky = self.server.pool.acquire(self._sticky_id())
        if backend is None:
            metrics.rejected_total.inc()
            return (*self._simple(503, "нет доступных бэкендов\n"), "none", False)

        try:
            result = self._fetch(backend, body)
        finally:
            backend.release()  # слот освобождаем, как только бэкенд ответил

        if result is None:
            return (*self._simple(502, "ошибка бэкенда\n"), backend.name, sticky)
        status, sent = self._relay(*result, backend, sticky)
        return status, sent, backend.name, sticky

    def _sticky_id(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            morsel = SimpleCookie(raw).get(STICKY_COOKIE)
        except CookieError:
            return None
        return morsel.value if morsel else None

    def _forward_headers(self) -> dict:
        drop = HOP_BY_HOP | _conn_tokens(self.headers.get("Connection")) | {
            "host", "expect", "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"}
        out = {}
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in drop:
                continue
            if kl in out:  # повторяющиеся заголовки склеиваем
                out[kl] += ("; " if kl == "cookie" else ", ") + v
            else:
                out[kl] = v
        # Мы — граница доверия: чужому X-Forwarded-For не верим, ставим свои значения.
        out["x-forwarded-for"] = self.client_address[0]
        out["x-forwarded-proto"] = "https"
        if self.headers.get("Host"):
            out["x-forwarded-host"] = self.headers["Host"]
        return out

    def _fetch(self, backend, body):
        """Сходить на бэкенд. Возвращает (status, reason, headers, data) или None при ошибке."""
        conn = http.client.HTTPConnection(backend.host, backend.port, timeout=3)
        try:
            conn.connect()           # на подключение — 3 с
            conn.sock.settimeout(30)  # на ожидание ответа — 30 с
            conn.request(self.command, self.path, body=body, headers=self._forward_headers())
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, resp.reason, resp.getheaders(), data
        except (OSError, http.client.HTTPException) as e:
            log("warn", "ошибка проксирования", backend=backend.name, err=repr(e))
            backend.set_alive(False)  # пассивная проверка здоровья
            return None
        finally:
            conn.close()

    def _relay(self, status, reason, headers, data, backend, sticky):
        """Отдать клиенту ответ бэкенда. Возвращает (код, байт отправлено)."""
        drop = HOP_BY_HOP | {"content-length"} | _conn_tokens(
            next((v for k, v in headers if k.lower() == "connection"), ""))
        out = [(k, v) for k, v in headers if k.lower() not in drop]

        head = self.command == "HEAD"
        no_body = head or status in (204, 304) or status < 200
        if head:  # на HEAD тела нет, но длину бэкенд сообщает — передаём как есть
            cl = next((v for k, v in headers if k.lower() == "content-length"), None)
            if cl is not None:
                out.append(("Content-Length", cl))
        elif not no_body:
            out.append(("Content-Length", str(len(data))))
        if not sticky:
            out.append(("Set-Cookie",
                        f"{STICKY_COOKIE}={backend.id}; Path=/; HttpOnly; Secure; SameSite=Lax"))
        try:
            self.send_response_only(status, reason)
            for k, v in out:
                self.send_header(k, v)
            self.end_headers()
            if not no_body:
                self.wfile.write(data)
        except OSError:  # клиент ушёл, пока мы отвечали
            self.close_connection = True
            return 499, 0
        return status, 0 if no_body else len(data)

    def _simple(self, code, text, close=False):
        """Короткий ответ, сгенерированный самим балансировщиком (ошибки)."""
        data = text.encode()
        try:
            self.send_response_only(code)
            self.send_header("Date", self.date_time_string())
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            if close:
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except OSError:
            self.close_connection = True
            return 499, 0
        return code, len(data)


class MetricsHandler(BaseHTTPRequestHandler):
    """Отдаёт /metrics для Prometheus (обычный HTTP, отдельный порт, только localhost)."""

    def do_GET(self):
        if urlsplit(self.path).path != "/metrics":
            self.send_error(404)
            return
        data = metrics.render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass
