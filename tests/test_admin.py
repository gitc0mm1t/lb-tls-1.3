"""Admin API целиком: настоящий HTTP-сервер на свободном порту и настоящие запросы.

Каждый тест поднимает свой балансировщик "в миниатюре": пул с одним
бэкендом-заглушкой, сертификат во временной папке, файл состояния и admin-сервер.
"""
import hashlib
import http.client
import json
import os
import shutil
import ssl
import tempfile
import threading
import unittest

from mylb.admin import MAX_BODY, AdminServer
from mylb.pool import Pool
from mylb.proxy import ProxyServer
from mylb.tlsconf import CertStore

from . import support

TOKEN = "test-token-0123456789"


@unittest.skipUnless(support.HAVE_OPENSSL, "для тестов нужен openssl (генерация сертификатов)")
class AdminTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.logs = support.CapturedLogs()
        cls.logs.__enter__()
        cls.addClassCleanup(cls.logs.__exit__)
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixtures.cleanup)
        d = cls.fixtures.name
        cls.cert_a = support.make_cert(d, "a", cn="first")    # с ним балансировщик стартует
        cls.cert_b = support.make_cert(d, "b", cn="second")   # на него будем менять
        cls.enc_key = support.encrypt_key(cls.cert_b[1], f"{d}/b.enc.key")
        cls.stub1 = support.start_stub_backend()
        cls.stub2 = support.start_stub_backend()
        cls.addClassCleanup(cls.stub1.shutdown)
        cls.addClassCleanup(cls.stub2.shutdown)
        cls.url1 = f"http://127.0.0.1:{cls.stub1.server_port}"
        cls.url2 = f"http://127.0.0.1:{cls.stub2.server_port}"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        d = self.dir.name
        self.cert_path, self.key_path = f"{d}/server.crt", f"{d}/server.key"
        shutil.copy(self.cert_a[0], self.cert_path)
        shutil.copy(self.cert_a[1], self.key_path)
        self.state_path = f"{d}/state.json"

        self.pool = Pool([self.url1], 100)
        self.pool.check_all()
        self.certs = CertStore(self.cert_path, self.key_path)
        self.admin = AdminServer(("127.0.0.1", 0), self.pool, self.certs, self.state_path, TOKEN)
        threading.Thread(target=self.admin.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.admin.server_close)
        self.addCleanup(self.admin.shutdown)

    # --- помощники ---

    def call(self, method, path, body=None, token=TOKEN, headers=None, raw=None):
        """Один запрос к admin API. Возвращает (код, заголовки, разобранный JSON или None)."""
        h = dict(headers or {})
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        conn = http.client.HTTPConnection("127.0.0.1", self.admin.server_port, timeout=10)
        try:
            conn.request(method, path, body=data, headers=h)
            resp = conn.getresponse()
            text = resp.read().decode("utf-8")
            return resp.status, resp.headers, (json.loads(text) if text else None)
        finally:
            conn.close()

    def saved(self):
        with open(self.state_path, encoding="utf-8") as f:
            return json.load(f)

    def assertProblem(self, result, status, code):
        got_status, headers, body = result
        self.assertEqual(got_status, status, body)
        self.assertEqual(headers.get_content_type(), "application/problem+json")
        self.assertEqual(body["status"], status)
        self.assertEqual(body["code"], code)
        self.assertEqual(body["type"], "about:blank")
        self.assertTrue(body["title"])
        return body


class AuthAndRoutingTests(AdminTestCase):
    def test_no_token(self):
        result = self.call("GET", "/v1/backends", token=None)
        self.assertProblem(result, 401, "unauthorized")
        self.assertEqual(result[1]["WWW-Authenticate"], "Bearer")

    def test_wrong_token(self):
        self.assertProblem(self.call("GET", "/v1/backends", token="wrong"), 401, "unauthorized")
        self.assertProblem(self.call("GET", "/v1/backends", token=TOKEN + "x"), 401, "unauthorized")
        self.assertProblem(self.call("GET", "/v1/backends", token=None,
                                     headers={"Authorization": f"Basic {TOKEN}"}), 401, "unauthorized")

    def test_unknown_path_without_token_is_401_not_404(self):
        """Без токена нельзя даже выяснить, какие пути существуют."""
        self.assertProblem(self.call("GET", "/v1/nope", token=None), 401, "unauthorized")

    def test_no_change_without_token(self):
        self.call("DELETE", f"/v1/backends/{self.pool.backends[0].id}", token=None)
        self.call("POST", "/v1/backends", {"url": self.url2}, token="wrong")
        self.assertEqual(len(self.pool.backends), 1)

    def test_unknown_path(self):
        self.assertProblem(self.call("GET", "/v1/nope"), 404, "not_found")
        self.assertProblem(self.call("GET", "/backends"), 404, "not_found")  # без /v1
        self.assertProblem(self.call("GET", "/v1/backends/"), 404, "not_found")

    def test_wrong_method(self):
        result = self.call("DELETE", "/v1/settings")
        self.assertProblem(result, 405, "method_not_allowed")
        self.assertEqual(result[1]["Allow"], "GET, PATCH")
        self.assertProblem(self.call("PUT", "/v1/backends", {"url": self.url2}), 405, "method_not_allowed")

    def test_body_must_be_json(self):
        path = "/v1/backends"
        self.assertProblem(self.call("POST", path, raw=b'{"url": "http://x:1"}'), 415, "unsupported_media_type")
        ct = {"Content-Type": "application/json"}
        self.assertProblem(self.call("POST", path, raw=b"{oops", headers=ct), 400, "invalid_json")
        self.assertProblem(self.call("POST", path, raw=b'["http://x:1"]', headers=ct), 400, "invalid_json")
        self.assertProblem(self.call("POST", path, raw=b"", headers=ct), 400, "invalid_json")
        self.assertProblem(self.call("PATCH", "/v1/settings", raw=b'{"health_interval": NaN}', headers=ct),
                           400, "invalid_json")

    def test_body_too_large(self):
        big = b'{"url": "' + b"a" * MAX_BODY + b'"}'
        try:
            result = self.call("POST", "/v1/backends", raw=big, headers={"Content-Type": "application/json"})
        except OSError:
            return  # сервер закрыл соединение, не дочитав гигантское тело, — тоже допустимый отказ
        self.assertProblem(result, 413, "payload_too_large")


class BackendTests(AdminTestCase):
    def test_list(self):
        status, headers, body = self.call("GET", "/v1/backends")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "application/json")
        b = self.pool.backends[0]
        self.assertEqual(body, {"items": [{"id": b.id, "url": self.url1, "name": b.name, "state": "active",
                                           "alive": True, "active_requests": 0}]})

    def test_create(self):
        status, headers, body = self.call("POST", "/v1/backends", {"url": self.url2})
        self.assertEqual(status, 201)
        self.assertEqual(headers["Location"], f"/v1/backends/{body['id']}")
        self.assertEqual(body["url"], self.url2)
        self.assertEqual(body["state"], "active")
        self.assertTrue(body["alive"])  # заглушка отвечает на проверку здоровья
        self.assertEqual([b.url for b in self.pool.backends], [self.url1, self.url2])
        self.assertEqual(self.saved()["backends"][-1], {"url": self.url2, "state": "active"})
        self.assertEqual(self.call("GET", headers["Location"])[2], body)

    def test_new_backend_really_gets_traffic(self):
        self.call("POST", "/v1/backends", {"url": self.url2})
        seen = set()
        for _ in range(4):
            b, _ = self.pool.acquire()
            seen.add(b.url)
            b.release()
        self.assertEqual(seen, {self.url1, self.url2})

    def test_create_dead_backend_is_added_but_not_alive(self):
        url = f"http://127.0.0.1:{support.free_port()}"
        status, _, body = self.call("POST", "/v1/backends", {"url": url})
        self.assertEqual(status, 201)
        self.assertFalse(body["alive"])
        for _ in range(5):  # трафик на него не идёт
            b, _ = self.pool.acquire()
            self.assertEqual(b.url, self.url1)
            b.release()

    def test_create_draining(self):
        status, _, body = self.call("POST", "/v1/backends", {"url": self.url2, "state": "draining"})
        self.assertEqual(status, 201)
        self.assertEqual(body["state"], "draining")
        self.assertEqual(self.saved()["backends"][-1]["state"], "draining")

    def test_create_duplicate(self):
        result = self.call("POST", "/v1/backends", {"url": self.url1})
        self.assertProblem(result, 409, "backend_exists")
        self.assertEqual(result[1]["Location"], f"/v1/backends/{self.pool.backends[0].id}")
        self.assertEqual(len(self.pool.backends), 1)

    def test_create_invalid(self):
        for bad in ({}, {"url": "https://b:9000"}, {"url": "http://b:9000/path"}, {"url": "http://b:port"},
                    {"url": "http://b:99999"}, {"url": 42}, {"url": self.url2, "state": "off"},
                    {"url": self.url2, "weight": 5}):
            body = self.assertProblem(self.call("POST", "/v1/backends", bad), 422, "validation_failed")
            self.assertTrue(body["errors"], bad)
            self.assertEqual(set(body["errors"][0]), {"field", "message"})
        self.assertEqual(len(self.pool.backends), 1)
        self.assertFalse(os.path.exists(self.state_path))  # и на диск ничего не записано

    def test_get_unknown(self):
        self.assertProblem(self.call("GET", "/v1/backends/00000000"), 404, "not_found")
        self.assertProblem(self.call("GET", "/v1/backends/NOT-AN-ID"), 404, "not_found")

    def test_drain_and_return(self):
        self.call("POST", "/v1/backends", {"url": self.url2})
        first = self.pool.backends[0]
        status, _, body = self.call("PATCH", f"/v1/backends/{first.id}", {"state": "draining"})
        self.assertEqual((status, body["state"]), (200, "draining"))
        self.assertTrue(body["alive"])  # здоровье и решение оператора — разные вещи
        self.assertEqual(self.saved()["backends"][0]["state"], "draining")
        for _ in range(6):
            b, _ = self.pool.acquire()
            self.assertEqual(b.url, self.url2)
            b.release()
        # повтор того же запроса даёт тот же результат (идемпотентность)
        self.assertEqual(self.call("PATCH", f"/v1/backends/{first.id}", {"state": "draining"})[2], body)
        status, _, body = self.call("PATCH", f"/v1/backends/{first.id}", {"state": "active"})
        self.assertEqual((status, body["state"]), (200, "active"))
        self.assertTrue(first.usable)

    def test_patch_invalid(self):
        path = f"/v1/backends/{self.pool.backends[0].id}"
        for bad in ({}, {"state": "off"}, {"state": "draining", "url": "http://x:1"}):
            self.assertProblem(self.call("PATCH", path, bad), 422, "validation_failed")
        self.assertEqual(self.pool.backends[0].state, "active")
        self.assertProblem(self.call("PATCH", "/v1/backends/00000000", {"state": "draining"}), 404, "not_found")

    def test_delete(self):
        self.call("POST", "/v1/backends", {"url": self.url2})
        first = self.pool.backends[0]
        status, headers, body = self.call("DELETE", f"/v1/backends/{first.id}")
        self.assertEqual((status, body), (204, None))
        self.assertEqual([b.url for b in self.pool.backends], [self.url2])
        self.assertEqual(self.saved()["backends"], [{"url": self.url2, "state": "active"}])
        self.assertProblem(self.call("DELETE", f"/v1/backends/{first.id}"), 404, "not_found")

    def test_delete_last_backend_is_allowed(self):
        self.assertEqual(self.call("DELETE", f"/v1/backends/{self.pool.backends[0].id}")[0], 204)
        self.assertEqual(self.call("GET", "/v1/backends")[2], {"items": []})
        self.assertEqual(self.pool.acquire(), (None, False))  # клиенты получат 503
        self.assertEqual(self.call("POST", "/v1/backends", {"url": self.url1})[0], 201)  # и можно вернуть
        self.assertIsNotNone(self.pool.acquire()[0])

    def test_change_not_applied_when_disk_write_fails(self):
        self.admin.state_path = f"{self.dir.name}/нет-такой-папки/state.json"
        first = self.pool.backends[0]
        self.assertProblem(self.call("POST", "/v1/backends", {"url": self.url2}), 500, "persist_failed")
        self.assertProblem(self.call("PATCH", f"/v1/backends/{first.id}", {"state": "draining"}),
                           500, "persist_failed")
        self.assertProblem(self.call("DELETE", f"/v1/backends/{first.id}"), 500, "persist_failed")
        self.assertProblem(self.call("PATCH", "/v1/settings", {"max_conns": 5}), 500, "persist_failed")
        self.assertEqual([(b.url, b.state) for b in self.pool.backends], [(self.url1, "active")])
        self.assertEqual(self.pool.max_conns, 100)


class SettingsTests(AdminTestCase):
    def test_get(self):
        self.assertEqual(self.call("GET", "/v1/settings")[2],
                         {"max_conns": 100, "health_path": "/healthz", "health_interval": 2.0})

    def test_patch_one_field_keeps_the_rest(self):
        status, _, body = self.call("PATCH", "/v1/settings", {"max_conns": 5})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"max_conns": 5, "health_path": "/healthz", "health_interval": 2.0})
        self.assertEqual(self.pool.max_conns, 5)
        self.assertEqual(self.saved()["settings"], body)

    def test_patch_all_fields(self):
        new = {"max_conns": 7, "health_path": "/ping", "health_interval": 5}
        status, _, body = self.call("PATCH", "/v1/settings", new)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"max_conns": 7, "health_path": "/ping", "health_interval": 5.0})
        self.assertEqual((self.pool.health_path, self.pool.health_interval), ("/ping", 5.0))

    def test_invalid_patch_changes_nothing(self):
        for bad in ({}, {"max_conns": 0}, {"max_conns": "5"}, {"max_conns": True}, {"max_conns": 2.5},
                    {"health_path": "ping"}, {"health_interval": 0}, {"listen": "0.0.0.0:1"},
                    {"max_conns": 5, "health_interval": -1}):  # одно поле верное, второе нет: не меняется ни одно
            self.assertProblem(self.call("PATCH", "/v1/settings", bad), 422, "validation_failed")
        self.assertEqual(self.call("GET", "/v1/settings")[2],
                         {"max_conns": 100, "health_path": "/healthz", "health_interval": 2.0})
        self.assertFalse(os.path.exists(self.state_path))


class HostInfoTests(AdminTestCase):
    """GET /v1/system/interfaces отдаёт файл, который пишет scripts/host-info.sh."""

    GOOD = {"collected_at": "2026-11-16T22:40:00Z", "hostname": "srv-1", "source": "ip",
            "items": [{"name": "lo", "addresses": [{"address": "127.0.0.1", "prefix": 8}]},
                      {"name": "eth0", "addresses": [{"address": "10.0.1.15", "prefix": 24},
                                                     {"address": "10.0.1.16", "prefix": 24}]}]}

    def setUp(self):
        super().setUp()
        self.info_path = f"{self.dir.name}/host.json"
        self.admin.host_info_path = self.info_path

    def write(self, text):
        with open(self.info_path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_returns_collected_info(self):
        self.write(json.dumps(self.GOOD))
        status, headers, body = self.call("GET", "/v1/system/interfaces")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "application/json")
        self.assertEqual(body, self.GOOD)

    def test_file_is_reread_on_every_request(self):
        self.write(json.dumps(self.GOOD))
        self.assertEqual(self.call("GET", "/v1/system/interfaces")[2]["hostname"], "srv-1")
        self.write(json.dumps({**self.GOOD, "hostname": "srv-2", "items": []}))    # скрипт запустили заново
        status, _, body = self.call("GET", "/v1/system/interfaces")
        self.assertEqual((status, body["hostname"], body["items"]), (200, "srv-2", []))

    def test_extra_fields_from_file_do_not_leak(self):
        self.write(json.dumps({**self.GOOD, "token": "secret-from-file"}))
        status, _, body = self.call("GET", "/v1/system/interfaces")
        self.assertEqual(status, 200)
        self.assertNotIn("token", body)

    def test_not_collected_is_404(self):
        body = self.assertProblem(self.call("GET", "/v1/system/interfaces"), 404, "host_info_missing")
        self.assertIn("host-info.sh", body["detail"])        # в ошибке сказано, что делать

    def test_broken_file_is_500(self):
        self.write("{ это не json")
        self.assertProblem(self.call("GET", "/v1/system/interfaces"), 500, "host_info_invalid")
        self.write(json.dumps({**self.GOOD, "items": [{"name": "eth0", "addresses": [{"address": "x", "prefix": 1}]}]}))
        self.assertProblem(self.call("GET", "/v1/system/interfaces"), 500, "host_info_invalid")

    def test_needs_token_and_is_read_only(self):
        self.write(json.dumps(self.GOOD))
        self.assertProblem(self.call("GET", "/v1/system/interfaces", token=None), 401, "unauthorized")
        result = self.call("POST", "/v1/system/interfaces", body={})
        self.assertProblem(result, 405, "method_not_allowed")
        self.assertEqual(result[1]["Allow"], "GET")


class CertificateTests(AdminTestCase):
    def fingerprint(self, cert_path):
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(support.read(cert_path).strip())).hexdigest()

    def upload(self, cert_path, key_path):
        return self.call("PUT", "/v1/tls/certificate",
                         {"cert_pem": support.read(cert_path), "key_pem": support.read(key_path)})

    def test_get(self):
        status, _, body = self.call("GET", "/v1/tls/certificate")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"subject", "issuer", "sans", "not_before", "not_after",
                                     "fingerprint_sha256", "loaded_at", "source", "warnings"})
        self.assertEqual(body["subject"], "CN=first")
        self.assertEqual(body["sans"], ["localhost", "127.0.0.1"])
        self.assertEqual(body["fingerprint_sha256"], self.fingerprint(self.cert_a[0]))
        self.assertEqual(body["source"], "startup")
        self.assertEqual(body["warnings"], [])
        self.assertRegex(body["not_after"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_upload(self):
        old_ctx = self.certs.ctx
        status, _, body = self.upload(*self.cert_b)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["subject"], "CN=second")
        self.assertEqual(body["source"], "upload")
        self.assertEqual(body["fingerprint_sha256"], self.fingerprint(self.cert_b[0]))
        self.assertNotIn("key_pem", body)
        self.assertNotIn("PRIVATE KEY", json.dumps(body))
        self.assertIsNot(self.certs.ctx, old_ctx)  # новые соединения получат новый контекст
        # файлы на диске заменены: после перезапуска балансировщик поднимется с новым сертификатом
        self.assertEqual(support.read(self.cert_path), support.read(self.cert_b[0]))
        self.assertEqual(support.read(self.key_path), support.read(self.cert_b[1]))
        self.assertEqual(os.stat(self.key_path).st_mode & 0o777, 0o600)
        self.assertEqual(sorted(os.listdir(self.dir.name)), ["server.crt", "server.key"])  # без временных файлов
        self.assertEqual(self.call("GET", "/v1/tls/certificate")[2], body)

    def test_upload_twice_gives_same_result(self):
        first = self.upload(*self.cert_b)[2]
        second = self.upload(*self.cert_b)[2]
        self.assertEqual(first["fingerprint_sha256"], second["fingerprint_sha256"])

    def assertRejected(self, result, status, code):
        """Отказ: нужный код ошибки, а активный сертификат и файлы остались прежними."""
        body = self.assertProblem(result, status, code)
        self.assertEqual(self.call("GET", "/v1/tls/certificate")[2]["fingerprint_sha256"],
                         self.fingerprint(self.cert_a[0]))
        return body

    def assertFilesUntouched(self):
        self.assertEqual(support.read(self.cert_path), support.read(self.cert_a[0]))
        self.assertEqual(support.read(self.key_path), support.read(self.cert_a[1]))
        self.assertEqual(sorted(os.listdir(self.dir.name)), ["server.crt", "server.key"])

    def test_upload_mismatched_pair(self):
        self.assertRejected(self.upload(self.cert_b[0], self.cert_a[1]), 422, "cert_key_mismatch")
        self.assertFilesUntouched()

    def test_upload_broken_cert(self):
        result = self.call("PUT", "/v1/tls/certificate",
                           {"cert_pem": "это не сертификат", "key_pem": support.read(self.cert_b[1])})
        self.assertRejected(result, 422, "cert_invalid")
        half = support.read(self.cert_b[0])[:400] + "\n-----END CERTIFICATE-----\n"
        result = self.call("PUT", "/v1/tls/certificate", {"cert_pem": half, "key_pem": support.read(self.cert_b[1])})
        self.assertRejected(result, 422, "cert_invalid")
        self.assertFilesUntouched()

    def test_upload_broken_key(self):
        result = self.call("PUT", "/v1/tls/certificate",
                           {"cert_pem": support.read(self.cert_b[0]), "key_pem": "это не ключ"})
        self.assertRejected(result, 422, "key_invalid")
        self.assertFilesUntouched()

    def test_upload_encrypted_key(self):
        self.assertRejected(self.upload(self.cert_b[0], self.enc_key), 422, "key_encrypted")
        self.assertFilesUntouched()

    def test_upload_missing_fields(self):
        for bad in ({}, {"cert_pem": support.read(self.cert_b[0])}, {"cert_pem": "", "key_pem": ""},
                    {"cert_pem": 1, "key_pem": 2},
                    {"cert_pem": "x", "key_pem": "y", "password": "z"}):
            self.assertRejected(self.call("PUT", "/v1/tls/certificate", bad), 422, "validation_failed")
        self.assertFilesUntouched()

    def test_upload_fails_when_cert_dir_is_read_only(self):
        if os.geteuid() == 0:
            self.skipTest("root пишет и в каталог без прав на запись")
        os.chmod(self.dir.name, 0o500)
        self.addCleanup(os.chmod, self.dir.name, 0o700)
        self.assertRejected(self.upload(*self.cert_b), 500, "persist_failed")

    def test_reload_from_disk(self):
        shutil.copy(self.cert_b[0], self.cert_path)  # файлы заменил "внешний инструмент"
        shutil.copy(self.cert_b[1], self.key_path)
        self.assertEqual(self.call("GET", "/v1/tls/certificate")[2]["subject"], "CN=first")  # пока старый
        status, _, body = self.call("POST", "/v1/tls/certificate/reload")
        self.assertEqual(status, 200, body)
        self.assertEqual((body["subject"], body["source"]), ("CN=second", "manual"))

    def test_reload_when_files_are_bad(self):
        os.unlink(self.cert_path)
        body = self.assertRejected(self.call("POST", "/v1/tls/certificate/reload"), 409, "cert_file_missing")
        self.assertIn("прежний", body["detail"])
        shutil.copy(self.cert_b[0], self.cert_path)  # сертификат B рядом с ключом A
        self.assertRejected(self.call("POST", "/v1/tls/certificate/reload"), 409, "cert_key_mismatch")
        shutil.copy(self.cert_b[1], self.key_path)   # теперь пара целая — следующая попытка проходит
        self.assertEqual(self.call("POST", "/v1/tls/certificate/reload")[0], 200)

    def test_secrets_never_reach_the_log(self):
        self.upload(*self.cert_b)
        self.upload(self.cert_b[0], self.cert_a[1])
        self.call("GET", "/v1/backends", token="wrong-token-value")
        logs = self.logs.text
        self.assertIn("certificate.replace", logs)  # само событие в журнале есть
        key_line = support.read(self.cert_b[1]).splitlines()[1]
        self.assertNotIn(key_line, logs)
        self.assertNotIn("PRIVATE KEY", logs)
        self.assertNotIn(TOKEN, logs)
        self.assertNotIn("wrong-token-value", logs)


class TlsSwapTests(AdminTestCase):
    """Самое важное свойство: сертификат меняется, а соединения не рвутся."""

    def setUp(self):
        super().setUp()
        self.proxy = ProxyServer(("127.0.0.1", 0), self.pool, self.certs)
        threading.Thread(target=self.proxy.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.proxy.server_close)
        self.addCleanup(self.proxy.shutdown)

    def connect(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # сертификат самоподписанный; нам нужен только его отпечаток
        conn = http.client.HTTPSConnection("127.0.0.1", self.proxy.server_port, context=ctx, timeout=10)
        conn.connect()
        self.addCleanup(conn.close)
        return conn

    def served_fingerprint(self, conn):
        return hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()

    def get(self, conn):
        conn.request("GET", "/")
        resp = conn.getresponse()
        resp.read()
        return resp.status

    def test_old_connection_survives_and_new_one_gets_new_cert(self):
        fp_a = self.call("GET", "/v1/tls/certificate")[2]["fingerprint_sha256"]
        old = self.connect()
        self.assertEqual(old.sock.version(), "TLSv1.3")
        self.assertEqual(self.served_fingerprint(old), fp_a)
        self.assertEqual(self.get(old), 200)

        status, _, body = self.call("PUT", "/v1/tls/certificate", {
            "cert_pem": support.read(self.cert_b[0]), "key_pem": support.read(self.cert_b[1])})
        self.assertEqual(status, 200)
        fp_b = body["fingerprint_sha256"]
        self.assertNotEqual(fp_a, fp_b)

        self.assertEqual(self.get(old), 200)                    # старое соединение живо...
        self.assertEqual(self.served_fingerprint(old), fp_a)    # ...и работает со старым сертификатом
        new = self.connect()
        self.assertEqual(self.served_fingerprint(new), fp_b)    # новое получило новый
        self.assertEqual(self.get(new), 200)

    def test_rejected_upload_does_not_disturb_clients(self):
        fp_a = self.call("GET", "/v1/tls/certificate")[2]["fingerprint_sha256"]
        self.call("PUT", "/v1/tls/certificate", {
            "cert_pem": support.read(self.cert_b[0]), "key_pem": support.read(self.cert_a[1])})
        conn = self.connect()
        self.assertEqual(self.served_fingerprint(conn), fp_a)
        self.assertEqual(self.get(conn), 200)

    def test_tls12_is_still_rejected_after_swap(self):
        self.call("PUT", "/v1/tls/certificate", {
            "cert_pem": support.read(self.cert_b[0]), "key_pem": support.read(self.cert_b[1])})
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        conn = http.client.HTTPSConnection("127.0.0.1", self.proxy.server_port, context=ctx, timeout=10)
        with self.assertRaises((ssl.SSLError, OSError)):
            conn.connect()
        conn.close()


if __name__ == "__main__":
    unittest.main()
