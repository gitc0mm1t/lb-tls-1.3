"""Файл состояния и проверки значений."""
import json
import os
import tempfile
import unittest

from mylb import state
from mylb.pool import Pool


class CheckTests(unittest.TestCase):
    def test_backend_ok(self):
        self.assertEqual(state.check_backend({"url": "http://b1:9000"}), [])
        self.assertEqual(state.check_backend({"url": "http://b1:9000", "state": "draining"}), [])

    def test_backend_bad(self):
        for bad in ({}, {"url": "https://b1:9000"}, {"url": "http://b1:9000/path"}, {"url": 5},
                    {"url": "b1:9000"}, {"url": "http://b1:9000", "state": "off"},
                    {"url": "http://b1:9000", "weight": 3}):
            self.assertTrue(state.check_backend(bad), bad)

    def test_settings_ok(self):
        self.assertEqual(state.check_settings({"max_conns": 1}, partial=True), [])
        self.assertEqual(state.check_settings({"health_interval": 5}, partial=True), [])
        self.assertEqual(state.check_settings(
            {"max_conns": 100, "health_path": "/healthz", "health_interval": 0.1}, partial=False), [])

    def test_settings_bad(self):
        for bad in ({}, {"max_conns": 0}, {"max_conns": 1.5}, {"max_conns": True}, {"max_conns": "10"},
                    {"health_path": "healthz"}, {"health_path": "/a b"}, {"health_path": "/a\r\nX: y"},
                    {"health_interval": 0.05}, {"health_interval": float("inf")}, {"health_interval": 10 ** 400},
                    {"health_interval": False}, {"listen": "0.0.0.0:1"}):
            self.assertTrue(state.check_settings(bad, partial=True), bad)

    def test_full_settings_require_every_field(self):
        self.assertTrue(state.check_settings({"max_conns": 1}, partial=False))


class FileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "state.json")

    def test_missing_file_is_none(self):
        self.assertIsNone(state.load(self.path))

    def test_roundtrip(self):
        pool = Pool(["http://127.0.0.1:9001", "http://127.0.0.1:9002"], 7, "/ping", 1.5)
        pool.backends[1].state = "draining"
        state.save(self.path, state.snapshot(pool))
        data = state.load(self.path)
        self.assertEqual(data["backends"], [{"url": "http://127.0.0.1:9001", "state": "active"},
                                            {"url": "http://127.0.0.1:9002", "state": "draining"}])
        self.assertEqual(data["settings"], {"max_conns": 7, "health_path": "/ping", "health_interval": 1.5})
        self.assertFalse(os.path.exists(self.path + ".tmp"))  # временный файл за собой убрали

    def test_broken_file_is_an_error_not_silence(self):
        for text in ("{не json", "[]", '{"version": 99, "backends": [], "settings": {}}',
                     '{"version": 1, "backends": [{"url": "ftp://x"}], '
                     '"settings": {"max_conns": 1, "health_path": "/", "health_interval": 1}}',
                     '{"version": 1, "backends": [], "settings": {"max_conns": 0}}'):
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(text)
            with self.assertRaises(ValueError, msg=text):
                state.load(self.path)

    def test_hand_written_file_without_state_gets_default(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "backends": [{"url": "http://b1:9000"}],
                       "settings": {"max_conns": 1, "health_path": "/", "health_interval": 1}}, f)
        self.assertEqual(state.load(self.path)["backends"], [{"url": "http://b1:9000", "state": "active"}])

    def test_save_failure_is_persist_error(self):
        with self.assertRaises(state.PersistError):
            state.save(os.path.join(self.dir.name, "нет-такой-папки", "state.json"), {"version": 1})


if __name__ == "__main__":
    unittest.main()
