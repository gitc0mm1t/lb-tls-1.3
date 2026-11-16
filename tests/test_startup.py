"""Запуск настоящего процесса `python -m mylb`.

Проверяем главное правило: список бэкендов не зашит ни в код, ни в конфиг.
Балансировщик стартует с пустым пулом, а бэкенды добавляют и удаляют запросами к admin API.
"""
import http.client
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
import unittest

from . import support

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "startup-test-token-0123456789"


@unittest.skipUnless(support.HAVE_OPENSSL, "для тестов нужен openssl (генерация сертификатов)")
class StartupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.crt, self.key = support.make_cert(self.dir, "server")
        self.state_path = os.path.join(self.dir, "state.json")
        self.listen, self.metrics, self.admin = support.free_port(), support.free_port(), support.free_port()
        self.stub = support.start_stub_backend()
        self.addCleanup(self.stub.server_close)   # cleanup'ы идут в обратном порядке: сначала shutdown
        self.addCleanup(self.stub.shutdown)
        self.stub_url = f"http://127.0.0.1:{self.stub.server_port}"

    # --- помощники ---

    def start(self, token=TOKEN, extra=()):
        """Запускает балансировщик отдельным процессом. Про бэкенды в команде нет ни слова."""
        env = {k: v for k, v in os.environ.items() if k != "MYLB_ADMIN_TOKEN"}
        if token:
            env["MYLB_ADMIN_TOKEN"] = token
        self.log_path = os.path.join(self.dir, "lb.log")
        out = open(self.log_path, "w", encoding="utf-8")
        self.addCleanup(out.close)
        proc = subprocess.Popen(
            [sys.executable, "-m", "mylb",
             "--listen", f"127.0.0.1:{self.listen}", "--metrics", f"127.0.0.1:{self.metrics}",
             "--admin", f"127.0.0.1:{self.admin}", "--state-file", self.state_path,
             "--cert", self.crt, "--key", self.key, "--health-interval", "0.2", *extra],
            cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop, proc)
        return proc

    def stop(self, proc):
        if proc.poll() is None:
            proc.terminate()   # SIGTERM: балансировщик завершается сам
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def log_text(self):
        with open(self.log_path, encoding="utf-8") as f:
            return f.read()

    def api(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.admin, timeout=5)
        try:
            headers = {"Authorization": f"Bearer {TOKEN}"}
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            conn.request(method, "/v1" + path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
        finally:
            conn.close()

    def wait_admin(self, proc):
        """Ждём, пока admin API начнёт отвечать (процессу нужно время на старт)."""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.fail(f"балансировщик завершился с кодом {proc.returncode}:\n{self.log_text()}")
            try:
                return self.api("GET", "/backends")
            except OSError:
                time.sleep(0.05)
        self.fail(f"admin API не поднялся за 15 с:\n{self.log_text()}")

    def client_status(self):
        """Один запрос клиента через балансировщик (HTTPS). Возвращает код ответа."""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE      # сертификат самоподписанный
        conn = http.client.HTTPSConnection("127.0.0.1", self.listen, context=ctx, timeout=5)
        try:
            conn.request("GET", "/")
            resp = conn.getresponse()
            resp.read()
            return resp.status
        finally:
            conn.close()

    # --- тесты ---

    def test_starts_with_empty_pool_and_backends_come_from_api(self):
        proc = self.start()
        status, body = self.wait_admin(proc)
        self.assertEqual((status, body), (200, {"items": []}))    # ничего не зашито
        self.assertEqual(self.client_status(), 503)               # отдавать запросы некому
        self.assertIn("пул пуст", self.log_text())

        status, created = self.api("POST", "/backends", {"url": self.stub_url})
        self.assertEqual(status, 201)
        self.assertTrue(created["alive"])
        self.assertEqual(self.client_status(), 200)               # трафик пошёл без перезапуска

        status, _ = self.api("DELETE", f"/backends/{created['id']}")
        self.assertEqual(status, 204)
        self.assertEqual(self.client_status(), 503)
        self.assertEqual(self.api("GET", "/backends"), (200, {"items": []}))
        with open(self.state_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["backends"], [])

    def test_backend_added_via_api_survives_restart(self):
        proc = self.start()
        self.wait_admin(proc)
        self.assertEqual(self.api("POST", "/backends", {"url": self.stub_url})[0], 201)
        self.stop(proc)

        proc = self.start()                                       # та же команда, без списка бэкендов
        status, body = self.wait_admin(proc)
        self.assertEqual(status, 200)
        self.assertEqual([b["url"] for b in body["items"]], [self.stub_url])
        self.assertEqual(self.client_status(), 200)

    def test_without_admin_api_and_without_backends_refuses_to_start(self):
        proc = self.start(token="")
        self.assertEqual(proc.wait(timeout=15), 1)
        text = self.log_text()
        self.assertIn("MYLB_ADMIN_TOKEN", text)                   # в ошибке сказано, что делать
        self.assertIn("--backends", text)
        self.assertFalse(os.path.exists(self.state_path))

    def test_backends_flag_still_works_without_admin_api(self):
        proc = self.start(token="", extra=("--backends", self.stub_url))
        deadline = time.monotonic() + 15
        while True:
            if proc.poll() is not None:
                self.fail(f"балансировщик завершился с кодом {proc.returncode}:\n{self.log_text()}")
            try:
                status = self.client_status()
                break
            except OSError:
                if time.monotonic() > deadline:
                    self.fail(f"балансировщик не поднялся за 15 с:\n{self.log_text()}")
                time.sleep(0.05)
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
