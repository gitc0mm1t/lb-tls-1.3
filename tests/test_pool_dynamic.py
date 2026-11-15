"""Пул, который меняется на лету: добавление, удаление, вывод из ротации."""
import threading
import unittest

from mylb import metrics
from mylb.backend import Backend
from mylb.pool import DuplicateBackend, Pool


def alive(url):
    b = Backend(url)
    b.alive = True
    return b


class DynamicPoolTests(unittest.TestCase):
    def test_empty_pool_gives_no_backend(self):
        pool = Pool([], 10)
        self.assertEqual(pool.acquire(), (None, False))
        self.assertEqual(pool.acquire("deadbeef"), (None, False))

    def test_add_then_acquire(self):
        pool = Pool([], 10)
        b = alive("http://127.0.0.1:9001")
        pool.add(b)
        got, _ = pool.acquire()
        self.assertIs(got, b)

    def test_duplicate_is_rejected(self):
        pool = Pool(["http://127.0.0.1:9001"], 10)
        with self.assertRaises(DuplicateBackend) as cm:
            pool.add(Backend("http://127.0.0.1:9001"))
        self.assertIs(cm.exception.existing, pool.backends[0])
        self.assertEqual(len(pool.backends), 1)

    def test_remove(self):
        pool = Pool(["http://127.0.0.1:9001", "http://127.0.0.1:9002"], 10)
        for b in pool.backends:
            b.alive = True
        gone = pool.backends[0]
        self.assertIs(pool.remove(gone.id), gone)
        self.assertIsNone(pool.remove(gone.id))  # второй раз удалять нечего
        for _ in range(10):
            b, _ = pool.acquire()
            self.assertIsNot(b, gone)
            b.release()

    def test_removed_backend_finishes_inflight_request(self):
        pool = Pool(["http://127.0.0.1:9001"], 10)
        pool.backends[0].alive = True
        b, _ = pool.acquire()  # запрос начался
        pool.remove(b.id)      # бэкенд удалили посреди запроса
        b.release()            # запрос спокойно завершается
        self.assertEqual(b.active, 0)

    def test_sticky_cookie_of_removed_backend_falls_back(self):
        pool = Pool(["http://127.0.0.1:9001", "http://127.0.0.1:9002"], 10)
        for b in pool.backends:
            b.alive = True
        gone = pool.backends[0]
        pool.remove(gone.id)
        b, sticky = pool.acquire(gone.id)
        self.assertIsNotNone(b)
        self.assertFalse(sticky)  # клиенту выдадут новую cookie

    def test_draining_backend_gets_no_new_requests(self):
        pool = Pool(["http://127.0.0.1:9001", "http://127.0.0.1:9002"], 10)
        for b in pool.backends:
            b.alive = True
        drained = pool.backends[0]
        drained.state = "draining"
        for _ in range(10):
            b, _ = pool.acquire()
            self.assertIsNot(b, drained)
            b.release()
        b, sticky = pool.acquire(drained.id)  # и по sticky-cookie тоже
        self.assertIsNot(b, drained)
        self.assertFalse(sticky)

    def test_max_conns_change_applies_to_next_request(self):
        pool = Pool(["http://127.0.0.1:9001"], 1)
        pool.backends[0].alive = True
        self.assertIsNotNone(pool.acquire()[0])
        self.assertIsNone(pool.acquire()[0])  # лимит 1 исчерпан
        pool.max_conns = 2
        self.assertIsNotNone(pool.acquire()[0])

    def test_metrics_series_disappears_after_remove(self):
        pool = Pool(["http://127.0.0.1:9777"], 10)
        self.assertIn('lb_backend_up{backend="127.0.0.1:9777"}', metrics.render())
        pool.remove(pool.backends[0].id)
        self.assertNotIn('lb_backend_up{backend="127.0.0.1:9777"}', metrics.render())
        self.assertNotIn('lb_backend_active_connections{backend="127.0.0.1:9777"}', metrics.render())

    def test_concurrent_acquire_while_pool_changes(self):
        """Потоки берут бэкенды, пока другой поток добавляет и удаляет: ничего не падает."""
        pool = Pool(["http://127.0.0.1:9001"], 1000)
        pool.backends[0].alive = True
        errors, stop = [], threading.Event()

        def worker():
            try:
                while not stop.is_set():
                    b, _ = pool.acquire()
                    if b:
                        b.release()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for i in range(300):
            b = alive(f"http://127.0.0.1:{10000 + i}")
            pool.add(b)
            pool.remove(b.id)
        stop.set()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(pool.backends), 1)


if __name__ == "__main__":
    unittest.main()
