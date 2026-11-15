"""Набор бэкендов + логика выбора + периодические проверки здоровья."""
import threading
import time

from . import metrics
from .backend import Backend
from .logs import log


class DuplicateBackend(ValueError):
    """Бэкенд с таким адресом уже есть в пуле."""

    def __init__(self, existing: Backend):
        super().__init__(f"бэкенд {existing.name} указан дважды")
        self.existing = existing


class Pool:
    def __init__(self, urls, max_conns: int, health_path: str = "/healthz", health_interval: float = 2.0):
        # Эти три настройки можно менять на лету через admin API: код ниже
        # каждый раз читает их заново, а не запоминает значение при старте.
        self.max_conns = max_conns
        self.health_path = health_path
        self.health_interval = health_interval

        # Список бэкендов читают все потоки с запросами, а меняет его admin API.
        # Править список "на месте" нельзя: поток, который как раз его обходит,
        # увидел бы половину изменения. Поэтому при добавлении и удалении мы
        # собираем НОВЫЙ список и подменяем старый одним присваиванием
        # (copy-on-write). Читатель берёт ссылку один раз и работает со снимком.
        self.backends = []
        self.by_id = {}
        self._lock = threading.Lock()  # чтобы два изменения списка не перетёрли друг друга
        self._rr = 0
        self._rr_lock = threading.Lock()
        for raw in urls:
            self.add(Backend(raw))

    # --- состав пула ---

    def add(self, b: Backend) -> None:
        with self._lock:
            if b.id in self.by_id:
                raise DuplicateBackend(self.by_id[b.id])
            self.backends = self.backends + [b]
            self.by_id = {**self.by_id, b.id: b}
        # Prometheus будет вызывать эти функции при каждом скрейпе.
        metrics.backend_up.add((b.name,), lambda: 1 if b.alive else 0)
        metrics.backend_active.add((b.name,), lambda: b.active)

    def remove(self, backend_id: str):
        """Убрать бэкенд из пула. Возвращает его или None, если такого нет.

        Запросы, которые он обрабатывает прямо сейчас, не страдают: у каждого
        запроса своя ссылка на объект Backend, и слот он освободит как обычно.
        """
        with self._lock:
            b = self.by_id.get(backend_id)
            if b is None:
                return None
            self.backends = [x for x in self.backends if x is not b]
            self.by_id = {k: v for k, v in self.by_id.items() if k != backend_id}
        metrics.backend_up.remove((b.name,))
        metrics.backend_active.remove((b.name,))
        return b

    # --- выбор бэкенда ---

    def acquire(self, sticky_id=None):
        """Выбрать бэкенд и сразу занять на нём слот. Возвращает (backend, sticky).

        1. Есть cookie, указывающая на живой и не перегруженный бэкенд → берём его.
        2. Иначе берём живых, сортируем по числу активных запросов (least connections)
           и занимаем первого, у кого есть свободный слот.
        3. Никого нет → (None, False), вызывающий отдаст клиенту 503.

        "Живой" здесь значит usable: здоров и не выведен оператором (draining).
        """
        backends = self.backends  # снимок: дальше работаем только с ним
        if sticky_id:
            b = self.by_id.get(sticky_id)
            if b and b.usable and b.try_acquire(self.max_conns):
                return b, True
            # Бэкенд из cookie умер, перегружен, выведен или удалён — идём в общий
            # выбор, а клиенту выдадим новую cookie.

        n = len(backends)
        if n == 0:  # через admin API можно удалить и последний бэкенд
            return None, False
        with self._rr_lock:
            self._rr += 1
            start = self._rr % n  # каждый раз начинаем с другого места
        cands = [backends[(start + i) % n] for i in range(n)]
        cands = [b for b in cands if b.usable]
        # sort в Python стабильный: при равной нагрузке порядок сохраняется,
        # и благодаря сдвигу start получается round-robin.
        cands.sort(key=lambda b: b.active)
        for b in cands:
            if b.try_acquire(self.max_conns):
                return b, False
        return None, False

    # --- проверки здоровья ---

    def check_all(self) -> None:
        """Проверить все бэкенды параллельно и дождаться результата."""
        threads = [threading.Thread(target=self.check_one, args=(b,)) for b in self.backends]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def check_one(self, b: Backend) -> None:
        ok = b.probe(self.health_path)
        metrics.health_checks_total.inc(b.name, "ok" if ok else "fail")
        if b.record_check(ok):
            log("info", "состояние бэкенда изменилось", backend=b.name, alive=b.alive)

    def health_loop(self, stop: threading.Event) -> None:
        # Спим короткими отрезками, а не сразу весь интервал: если интервал
        # поменяли через admin API, новое значение подействует почти сразу,
        # а не после того, как закончится старое ожидание.
        last = time.monotonic()
        while not stop.wait(min(self.health_interval, 0.2)):  # wait вернёт True, когда попросят остановиться
            if time.monotonic() - last >= self.health_interval:
                last = time.monotonic()
                self.check_all()
