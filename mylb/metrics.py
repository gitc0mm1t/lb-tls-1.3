"""Мини-реализация метрик Prometheus (текстовый формат экспозиции).

Формат на самом деле очень простой: Prometheus ходит на /metrics и читает текст
вида `имя{метка="значение"} число`. Здесь он написан руками, чтобы было видно,
что за магия стоит за библиотекой prometheus_client. В реальных проектах
берут её — API очень похож.

Типы метрик:
  Counter   — только растёт (запросы, ошибки). Смотрят через rate().
  Gauge     — число, которое ходит вверх-вниз (активные соединения, жив/мёртв).
  Histogram — распределение значений (время ответа) → считаем p95/p99.
"""
import bisect
import threading

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
REGISTRY = []  # сюда каждая метрика добавляет себя при создании


def _num(x) -> str:
    if x == float("inf"):
        return "+Inf"
    return repr(x) if isinstance(x, float) else str(x)


def _labels(names, values) -> str:
    if not names:
        return ""
    esc = lambda v: str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
    return "{" + ",".join(f'{n}="{esc(v)}"' for n, v in zip(names, values)) + "}"


class _Metric:
    kind = ""

    def __init__(self, name, help_text, labelnames=()):
        self.name, self.help, self.labelnames = name, help_text, tuple(labelnames)
        self._lock = threading.Lock()  # метрики обновляют много потоков одновременно
        REGISTRY.append(self)

    def _header(self):
        return [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]


class Counter(_Metric):
    kind = "counter"

    def __init__(self, name, help_text, labelnames=()):
        super().__init__(name, help_text, labelnames)
        self._values = {} if labelnames else {(): 0}

    def inc(self, *labelvalues):
        with self._lock:
            self._values[labelvalues] = self._values.get(labelvalues, 0) + 1

    def render(self):
        with self._lock:
            items = sorted(self._values.items())
        lines = self._header()
        lines += [f"{self.name}{_labels(self.labelnames, lv)} {v}" for lv, v in items]
        return lines


class CallbackGauge(_Metric):
    """Gauge, значение которого вычисляется в момент скрейпа — всегда актуально."""
    kind = "gauge"

    def __init__(self, name, help_text, labelnames=()):
        super().__init__(name, help_text, labelnames)
        self._children = []

    def add(self, labelvalues, fn):
        """Добавить серию. Если серия с такими метками уже есть — заменить её."""
        lv = tuple(labelvalues)
        with self._lock:
            # Собираем новый список вместо правки старого: render() в другом потоке
            # может как раз обходить старый, и менять его под ногами нельзя.
            self._children = [c for c in self._children if c[0] != lv] + [(lv, fn)]

    def remove(self, labelvalues):
        """Убрать серию (бэкенд удалили через admin API — его gauge больше не нужен)."""
        lv = tuple(labelvalues)
        with self._lock:
            self._children = [c for c in self._children if c[0] != lv]

    def render(self):
        lines = self._header()
        for lv, fn in self._children:
            lines.append(f"{self.name}{_labels(self.labelnames, lv)} {fn()}")
        return lines


class Histogram(_Metric):
    kind = "histogram"

    def __init__(self, name, help_text, labelnames=(), buckets=DEFAULT_BUCKETS):
        super().__init__(name, help_text, labelnames)
        self.buckets = tuple(sorted(buckets))
        self._data = {}  # labelvalues -> [счётчики по корзинам (+Inf в конце), сумма]

    def observe(self, value, *labelvalues):
        with self._lock:
            d = self._data.get(labelvalues)
            if d is None:
                d = self._data[labelvalues] = [[0] * (len(self.buckets) + 1), 0.0]
            d[0][bisect.bisect_left(self.buckets, value)] += 1
            d[1] += value

    def render(self):
        with self._lock:
            snap = [(lv, list(c), s) for lv, (c, s) in sorted(self._data.items())]
        lines = self._header()
        names = self.labelnames + ("le",)
        for lv, counts, total in snap:
            cum = 0
            for bound, c in zip(self.buckets + (float("inf"),), counts):
                cum += c  # в Prometheus корзины кумулятивные: "<= bound"
                lines.append(f"{self.name}_bucket{_labels(names, lv + (_num(bound),))} {cum}")
            base = _labels(self.labelnames, lv)
            lines.append(f"{self.name}_sum{base} {total}")
            lines.append(f"{self.name}_count{base} {cum}")
        return lines


def render() -> str:
    lines = []
    for m in REGISTRY:
        lines.extend(m.render())
    return "\n".join(lines) + "\n"


# --- Метрики балансировщика (имена те же, что были бы в любой реализации) ---

requests_total = Counter(
    "lb_requests_total", "Всего запросов, прошедших через балансировщик.", ("backend", "code"))
request_duration = Histogram(
    "lb_request_duration_seconds", "Время обработки запроса (от приёма до конца ответа клиенту).", ("backend",))
rejected_total = Counter(
    "lb_rejected_total", "Запросы, отклонённые с 503: нет живого и незагруженного бэкенда.")
health_checks_total = Counter(
    "lb_health_checks_total", "Результаты активных health-check'ов.", ("backend", "result"))
backend_up = CallbackGauge(
    "lb_backend_up", "1 — бэкенд считается живым, 0 — выведен из ротации.", ("backend",))
backend_active = CallbackGauge(
    "lb_backend_active_connections", "Сколько запросов бэкенд обрабатывает прямо сейчас.", ("backend",))
