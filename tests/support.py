"""Общие помощники для тестов: сертификаты, бэкенд-заглушка, перехват логов."""
import io
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HAVE_OPENSSL = shutil.which("openssl") is not None


def make_cert(directory, name, cn="localhost"):
    """Самоподписанная пара <name>.crt / <name>.key. Возвращает (путь к сертификату, путь к ключу)."""
    crt, key = f"{directory}/{name}.crt", f"{directory}/{name}.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt,
         "-days", "30", "-subj", f"/CN={cn}", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        check=True, capture_output=True)
    return crt, key


def encrypt_key(key_path, out_path):
    subprocess.run(["openssl", "pkcs8", "-topk8", "-in", key_path, "-passout", "pass:secret", "-out", out_path],
                   check=True, capture_output=True)
    return out_path


def read(path):
    with open(path, encoding="ascii") as f:
        return f.read()


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        data = f"ответ от {self.server.server_port}\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


def start_stub_backend():
    """Бэкенд-заглушка на свободном порту: на любой GET отвечает 200."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def free_port():
    """Порт, на котором сейчас никто не слушает (для проверки "мёртвого" бэкенда)."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CapturedLogs:
    """Балансировщик пишет JSON-логи в stdout. В тестах забираем их себе:
    и вывод тестов чище, и можно проверить, что в лог не утёк секрет."""

    def __enter__(self):
        self._old = sys.stdout
        sys.stdout = self.buf = io.StringIO()
        return self

    def __exit__(self, *exc):
        sys.stdout = self._old

    @property
    def text(self):
        return self.buf.getvalue()
