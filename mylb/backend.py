"""Один upstream-сервер: состояние (жив/мёртв), нагрузка и проверка здоровья."""
import hashlib
import http.client
import threading
from urllib.parse import urlsplit

# Гистерезис: бэкенд меняет состояние не по одной проверке, а по серии.
# Без этого один случайный таймаут выкидывает сервер из ротации, а потом
# возвращает обратно ("флаппинг"), что вредно само по себе.
RISE = 2  # столько успешных проверок подряд нужно, чтобы "ожить"
FALL = 2  # столько неудачных подряд нужно, чтобы "умереть"


class Backend:
    def __init__(self, raw_url: str):
        u = urlsplit(raw_url)
        if u.scheme != "http" or not u.hostname:
            raise ValueError(f"некорректный URL бэкенда {raw_url!r} (нужен вид http://host:port)")
        self.host = u.hostname
        self.port = u.port or 80
        self.name = f"{self.host}:{self.port}"  # для логов и метрик
        # Короткий непрозрачный id для sticky-cookie: не светим клиенту внутренние адреса.
        self.id = hashlib.sha256(self.name.encode()).hexdigest()[:8]
        self.url = f"http://{self.name}"  # адрес в нормальном виде — для admin API и файла состояния

        # Два независимых признака:
        #   alive — что показали проверки здоровья (решает сам балансировщик);
        #   state — что решил оператор через admin API: "active" или "draining"
        #           (draining = новых запросов не давать, идущие пусть дорабатывают).
        self.state = "active"
        self.alive = False
        self._active = 0  # сколько запросов бэкенд обрабатывает прямо сейчас
        self._lock = threading.Lock()
        self._checked = False
        self._ok_streak = 0
        self._fail_streak = 0

    # --- нагрузка ---

    @property
    def usable(self) -> bool:
        """Можно ли отправлять сюда новые запросы: и здоров, и не выведен оператором."""
        return self.alive and self.state == "active"

    @property
    def active(self) -> int:
        return self._active

    def try_acquire(self, limit: int) -> bool:
        """Занять "слот" на бэкенде, если он ещё не достиг лимита.

        Проверка и увеличение счётчика — под одним локом. Без него два потока
        могли бы одновременно увидеть "есть место" и оба занять последний слот
        (race condition).
        """
        with self._lock:
            if self._active >= limit:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._active -= 1

    # --- здоровье ---

    def set_alive(self, value: bool) -> None:
        """Пассивная проверка: реальный запрос упал — выкидываем бэкенд сразу,
        не дожидаясь следующего health-check'а. Вернёт его уже health-check."""
        self.alive = value

    def probe(self, path: str, timeout: float = 2.0) -> bool:
        """Активная проверка: GET path, любой 2xx = здоров."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.read()  # дочитываем тело
            return 200 <= resp.status < 300
        except (OSError, http.client.HTTPException):  # отказ в соединении, таймаут, мусор в ответе
            return False
        finally:
            conn.close()

    def record_check(self, ok: bool) -> bool:
        """Учесть результат проверки. Возвращает True, если состояние изменилось."""
        with self._lock:
            was = self.alive
            if not self._checked:
                # Самая первая проверка — верим сразу, иначе после старта
                # пришлось бы ждать несколько секунд, пока бэкенды "оживут".
                self._checked = True
                self.alive = ok
            elif ok:
                self._ok_streak += 1
                self._fail_streak = 0
                if not was and self._ok_streak >= RISE:
                    self.alive = True
            else:
                self._fail_streak += 1
                self._ok_streak = 0
                if was and self._fail_streak >= FALL:
                    self.alive = False
            return was != self.alive
