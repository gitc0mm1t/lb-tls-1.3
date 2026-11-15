import unittest

from mylb.backend import Backend
from mylb.pool import Pool


def make_pool(n=3, max_conns=2):
    pool = Pool([f"http://127.0.0.1:{9000 + i}" for i in range(1, n + 1)], max_conns)
    for b in pool.backends:
        b.alive = True
    return pool


class AcquireTests(unittest.TestCase):
    def test_skips_dead_backends(self):
        pool = make_pool()
        pool.backends[0].alive = False
        for _ in range(20):
            b, _ = pool.acquire()
            self.assertIsNot(b, pool.backends[0])
            b.release()

    def test_prefers_least_loaded(self):
        pool = make_pool()
        pool.backends[0].try_acquire(10)  # b1 уже занят одним запросом
        for _ in range(10):
            b, _ = pool.acquire()
            self.assertIsNot(b, pool.backends[0])
            b.release()

    def test_round_robin_when_idle(self):
        pool = make_pool()
        seen = set()
        for _ in range(6):
            b, _ = pool.acquire()
            seen.add(b.name)
            b.release()
        self.assertEqual(len(seen), 3)

    def test_overloaded_backends_are_skipped_then_503(self):
        pool = make_pool(max_conns=1)
        got = [pool.acquire()[0] for _ in range(3)]
        self.assertEqual(len({b.name for b in got}), 3)
        self.assertIsNone(pool.acquire()[0])  # слотов не осталось

    def test_sticky_hit(self):
        pool = make_pool()
        target = pool.backends[1]
        b, sticky = pool.acquire(target.id)
        self.assertIs(b, target)
        self.assertTrue(sticky)

    def test_sticky_falls_back_when_backend_dead(self):
        pool = make_pool()
        target = pool.backends[1]
        target.alive = False
        b, sticky = pool.acquire(target.id)
        self.assertIsNot(b, target)
        self.assertFalse(sticky)  # значит, клиенту выдадут новую cookie

    def test_unknown_cookie_is_ignored(self):
        pool = make_pool()
        b, sticky = pool.acquire("deadbeef")
        self.assertIsNotNone(b)
        self.assertFalse(sticky)


class HealthTests(unittest.TestCase):
    def test_first_check_is_trusted_immediately(self):
        b = Backend("http://127.0.0.1:9001")
        self.assertTrue(b.record_check(True))
        self.assertTrue(b.alive)

    def test_hysteresis(self):
        b = Backend("http://127.0.0.1:9001")
        b.record_check(True)
        self.assertFalse(b.record_check(False))  # одна неудача — ещё жив
        self.assertTrue(b.alive)
        self.assertTrue(b.record_check(False))   # вторая подряд — умер
        self.assertFalse(b.alive)
        self.assertFalse(b.record_check(True))   # один успех — ещё мёртв
        self.assertFalse(b.alive)
        self.assertTrue(b.record_check(True))    # второй подряд — ожил
        self.assertTrue(b.alive)


if __name__ == "__main__":
    unittest.main()
