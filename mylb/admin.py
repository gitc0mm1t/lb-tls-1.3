"""Admin API: управление балансировщиком без перезапуска.

Контракт (что принимает и что возвращает каждая операция) описан в
docs/admin-api.openapi.yaml. Этот файл — его реализация.

Правила, общие для всех операций:
  * отдельный порт (--admin), никогда не клиентский :8443;
  * каждый запрос несёт Authorization: Bearer <токен из MYLB_ADMIN_TOKEN>;
  * сначала проверяем запрос целиком, потом записываем изменение на диск,
    и только потом применяем его. Сорвался любой шаг — не меняется ничего;
  * ошибки отдаём в формате application/problem+json (RFC 9457) с полем code;
  * приватный ключ и токен никогда не попадают ни в ответы, ни в лог.
"""
import hmac
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import state
from .backend import Backend
from .logs import log
from .state import PersistError
from .tlsconf import CertError

MAX_BODY = 1024 * 1024  # сертификат с ключом занимает несколько килобайт; 1 МБ хватит с запасом
BACKEND_ID = re.compile(r"^[0-9a-f]{8}$")

TITLES = {400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 405: "Method Not Allowed",
          409: "Conflict", 413: "Content Too Large", 415: "Unsupported Media Type",
          422: "Unprocessable Content", 500: "Internal Server Error"}

# Путь -> {метод: имя метода-обработчика}. Вся маршрутизация видна в одном месте.
ROUTES = [
    (re.compile(r"^/v1/backends$"), {"GET": "list_backends", "POST": "create_backend"}),
    (re.compile(r"^/v1/backends/([^/]+)$"),
     {"GET": "get_backend", "PATCH": "update_backend", "DELETE": "delete_backend"}),
    (re.compile(r"^/v1/tls/certificate$"), {"GET": "get_certificate", "PUT": "replace_certificate"}),
    (re.compile(r"^/v1/tls/certificate/reload$"), {"POST": "reload_certificate"}),
    (re.compile(r"^/v1/settings$"), {"GET": "get_settings", "PATCH": "update_settings"}),
]


class ApiError(Exception):
    """Ошибка, которую нужно вернуть клиенту. Обработчики просто бросают её."""

    def __init__(self, status, code, detail=None, errors=None, headers=None):
        super().__init__(code)
        self.status, self.code, self.detail = status, code, detail
        self.errors = errors or []      # [(поле, сообщение)] для validation_failed
        self.headers = headers or {}

    def body(self) -> dict:
        out = {"type": "about:blank", "title": TITLES.get(self.status, "Error"),
               "status": self.status, "code": self.code}
        if self.detail:
            out["detail"] = self.detail
        if self.errors:
            out["errors"] = [{"field": f, "message": m} for f, m in self.errors]
        return out


def _invalid(errors) -> ApiError:
    return ApiError(422, "validation_failed", "Запрос не прошёл проверку. Ничего не изменено.", errors=errors)


def _reject_constant(name):
    # json.loads по умолчанию принимает NaN и Infinity, хотя в стандарте JSON их нет.
    raise ValueError(f"недопустимое значение {name}")


def _backend_json(b: Backend) -> dict:
    return {"id": b.id, "url": b.url, "name": b.name, "state": b.state,
            "alive": b.alive, "active_requests": b.active}


def _settings_json(pool) -> dict:
    return state.snapshot(pool)["settings"]


class AdminServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, pool, certs, state_path: str, token: str):
        if not token:
            raise ValueError("admin API нельзя запускать без токена")
        self.pool = pool
        self.certs = certs
        self.state_path = state_path
        self.token = token
        # Изменения выполняются строго по одному. Их мало, а с одним локом не нужно
        # думать, что будет, если два запроса одновременно правят файл состояния.
        self.lock = threading.Lock()
        super().__init__(addr, AdminHandler)


class AdminHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mylb-admin"
    sys_version = ""
    timeout = 10  # медленного или зависшего клиента отключаем через 10 с

    def log_message(self, format, *args):  # стандартный лог в stderr отключаем — у нас свой
        pass

    # --- вход: один метод на все HTTP-методы ---

    def _handle(self):
        self.audit = {}  # обработчик кладёт сюда, что именно он изменил
        try:
            status, body, headers = self._dispatch()
        except ApiError as e:
            status, body, headers = e.status, e.body(), e.headers
        except PersistError as e:
            err = ApiError(500, "persist_failed", f"{e} Изменение не применено.")
            status, body, headers = err.status, err.body(), {}
        except Exception as e:  # noqa: BLE001 — последняя линия обороны
            log("error", "admin: необработанная ошибка", err=repr(e))
            err = ApiError(500, "internal_error", "Внутренняя ошибка, подробности в логе балансировщика.")
            status, body, headers = err.status, err.body(), {}
        self._send(status, body, headers)
        # Журнал: кто, что и с каким результатом. Тело запроса сюда не попадает никогда.
        log("warn" if status == 401 else "info", "admin",
            method=self.command, path=urlsplit(self.path).path, status=status,
            remote=self.client_address[0], **self.audit)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle

    def _dispatch(self):
        self._check_token()  # до маршрутизации: без токена нельзя даже узнать, какие пути существуют
        path = urlsplit(self.path).path
        for pattern, methods in ROUTES:
            m = pattern.match(path)
            if not m:
                continue
            name = methods.get(self.command)
            if name is None:
                allow = ", ".join(sorted(methods))
                raise ApiError(405, "method_not_allowed", f"Для этого пути допустимы методы: {allow}.",
                               headers={"Allow": allow})
            return getattr(self, name)(*m.groups())
        raise ApiError(404, "not_found", f"Пути {path} нет в этом API.")

    def _check_token(self) -> None:
        scheme, _, value = (self.headers.get("Authorization") or "").partition(" ")
        # compare_digest сравнивает за одинаковое время независимо от того, на каком
        # символе строки расходятся. Обычное == позволило бы подбирать токен по
        # времени ответа, символ за символом.
        ok = scheme.lower() == "bearer" and hmac.compare_digest(
            value.strip().encode(), self.server.token.encode())
        if not ok:
            raise ApiError(401, "unauthorized", "Нужен заголовок Authorization: Bearer <токен>.",
                           headers={"WWW-Authenticate": "Bearer"})

    def _json_body(self) -> dict:
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(400, "invalid_json", "Transfer-Encoding не поддерживается, нужен Content-Length.")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError
        except ValueError:
            raise ApiError(400, "invalid_json", "Некорректный Content-Length.") from None
        if length > MAX_BODY:
            raise ApiError(413, "payload_too_large", f"Тело запроса больше {MAX_BODY} байт.")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "unsupported_media_type",
                           "Нужен заголовок Content-Type: application/json (в curl: -H 'Content-Type: application/json').")
        raw = self.rfile.read(length)
        try:
            if len(raw) != length:
                raise ValueError("тело короче, чем обещано в Content-Length")
            data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            raise ApiError(400, "invalid_json", "Тело запроса не разобрано как JSON.") from None
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "Нужен JSON-объект: {...}.")
        return data

    def _send(self, status, body, headers) -> None:
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        # Одно соединение — один запрос. Так проще и надёжнее: если мы ответили
        # ошибкой, не дочитав тело, остаток тела не будет принят за следующий запрос.
        self.close_connection = True
        try:
            self.send_response(status)
            if body is not None:
                ctype = "application/problem+json" if status >= 400 else "application/json"
                self.send_header("Content-Type", f"{ctype}; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            if body is not None and self.command != "HEAD":
                self.wfile.write(data)
        except OSError:  # клиент ушёл, не дождавшись ответа
            pass

    def _find_backend(self, backend_id: str) -> Backend:
        b = self.server.pool.by_id.get(backend_id) if BACKEND_ID.match(backend_id) else None
        if b is None:
            raise ApiError(404, "not_found", f"Бэкенда {backend_id} нет в пуле.")
        return b

    # --- бэкенды ---

    def list_backends(self):
        return 200, {"items": [_backend_json(b) for b in self.server.pool.backends]}, {}

    def get_backend(self, backend_id):
        return 200, _backend_json(self._find_backend(backend_id)), {}

    def create_backend(self):
        srv = self.server
        data = self._json_body()
        errors = state.check_backend(data)
        if errors:
            raise _invalid(errors)
        try:
            b = Backend(data["url"])
        except ValueError:  # например, порт не число
            raise _invalid([("url", "Нужен вид http://host:port.")]) from None
        b.state = data.get("state", "active")

        with srv.lock:
            existing = srv.pool.by_id.get(b.id)
            if existing is not None:
                raise ApiError(409, "backend_exists", f"Бэкенд {existing.name} уже есть в пуле.",
                               headers={"Location": f"/v1/backends/{existing.id}"})
            # Первая проверка здоровья — до того, как бэкенд попадёт в ротацию:
            # вслепую трафик на него не пойдёт.
            srv.pool.check_one(b)
            snap = state.snapshot(srv.pool)
            snap["backends"].append({"url": b.url, "state": b.state})
            state.save(srv.state_path, snap)  # сначала диск...
            srv.pool.add(b)                   # ...потом память
        self.audit = {"action": "backend.create", "backend": b.name, "alive": b.alive}
        return 201, _backend_json(b), {"Location": f"/v1/backends/{b.id}"}

    def update_backend(self, backend_id):
        srv = self.server
        self._find_backend(backend_id)  # 404 важнее, чем ошибки в теле
        data = self._json_body()
        errors = [(k, "Неизвестное поле.") for k in data if k != "state"]
        if "state" not in data:
            errors.append(("state", "Обязательное поле."))
        elif data["state"] not in state.BACKEND_STATES:
            errors.append(("state", "Допустимые значения: active, draining."))
        if errors:
            raise _invalid(errors)

        with srv.lock:
            b = self._find_backend(backend_id)  # под локом ещё раз: его могли удалить только что
            snap = state.snapshot(srv.pool)
            for entry in snap["backends"]:
                if entry["url"] == b.url:
                    entry["state"] = data["state"]
            state.save(srv.state_path, snap)
            b.state = data["state"]
        self.audit = {"action": "backend.update", "backend": b.name, "state": b.state}
        return 200, _backend_json(b), {}

    def delete_backend(self, backend_id):
        srv = self.server
        with srv.lock:
            b = self._find_backend(backend_id)
            snap = state.snapshot(srv.pool)
            snap["backends"] = [e for e in snap["backends"] if e["url"] != b.url]
            state.save(srv.state_path, snap)
            srv.pool.remove(b.id)
        self.audit = {"action": "backend.delete", "backend": b.name}
        return 204, None, {}

    # --- TLS-сертификат ---

    def get_certificate(self):
        return 200, self.server.certs.info(), {}

    def replace_certificate(self):
        data = self._json_body()
        errors = [(k, "Неизвестное поле.") for k in data if k not in ("cert_pem", "key_pem")]
        for key in ("cert_pem", "key_pem"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                errors.append((key, "Обязательное поле: непустая строка в формате PEM."))
        if errors:
            raise _invalid(errors)
        with self.server.lock:
            try:
                info = self.server.certs.upload(data["cert_pem"], data["key_pem"])
            except CertError as e:
                raise ApiError(422, e.code, f"{e.detail} Активным остаётся прежний сертификат.") from None
        self.audit = {"action": "certificate.replace", "fingerprint": info["fingerprint_sha256"][:16]}
        return 200, info, {}

    def reload_certificate(self):
        with self.server.lock:
            try:
                info = self.server.certs.reload("manual")
            except CertError as e:
                raise ApiError(409, e.code, f"{e.detail} Активным остаётся прежний сертификат.") from None
        self.audit = {"action": "certificate.reload", "fingerprint": info["fingerprint_sha256"][:16]}
        return 200, info, {}

    # --- настройки ---

    def get_settings(self):
        return 200, _settings_json(self.server.pool), {}

    def update_settings(self):
        srv = self.server
        data = self._json_body()
        errors = state.check_settings(data, partial=True)
        if errors:
            raise _invalid(errors)
        if "health_interval" in data:
            data["health_interval"] = float(data["health_interval"])

        with srv.lock:
            snap = state.snapshot(srv.pool)
            snap["settings"].update(data)
            state.save(srv.state_path, snap)
            pool = srv.pool
            pool.max_conns = snap["settings"]["max_conns"]
            pool.health_path = snap["settings"]["health_path"]
            pool.health_interval = snap["settings"]["health_interval"]
        self.audit = {"action": "settings.update", "changed": sorted(data)}
        return 200, _settings_json(srv.pool), {}
