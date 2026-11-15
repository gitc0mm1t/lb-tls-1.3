"""Крошечный тестовый бэкенд, чтобы было на что балансировать."""
import argparse
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default="127.0.0.1:9001")
    ap.add_argument("--name", default="b1", help="имя, которое бэкенд пишет в ответе")
    args = ap.parse_args()
    host, _, port = args.addr.rpartition(":")
    state = {"healthy": True}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code, text):
            data = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/healthz":  # эндпоинт для health-check'ов балансировщика
                if state["healthy"]:
                    self._send(200, "ok\n")
                else:
                    self._send(503, "болею\n")
            elif path == "/toggle-health":  # переключает "здоров/болен" — смотреть, как LB выкидывает бэкенд
                state["healthy"] = not state["healthy"]
                self._send(200, f"{args.name} healthy={state['healthy']}\n")
            elif path == "/slow":  # медленный ответ — чтобы создать нагрузку
                time.sleep(3)
                self._send(200, f"медленный ответ от {args.name}\n")
            else:
                self._send(200, f"привет от {args.name}\n")

        def do_HEAD(self):  # как GET, но без тела (длину сообщаем)
            data = f"привет от {args.name}\n".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()

        def do_POST(self):  # эхо: сообщает, сколько байт тела получил
            n = int(self.headers.get("Content-Length") or 0)
            got = len(self.rfile.read(n)) if n else 0
            self._send(200, f"{args.name} получил {got} байт; X-Forwarded-For={self.headers.get('X-Forwarded-For')}\n")

        def log_message(self, format, *a):
            pass

    srv = ThreadingHTTPServer((host, int(port)), Handler)
    srv.daemon_threads = True
    print(f"{args.name} слушает на {args.addr}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
