import threading
import time
from collections import defaultdict, deque


class FailureLimiter:
    """Считает неудачные попытки по ключу в скользящем окне.

    Данные лежат в памяти процесса: после перезапуска сервера счётчики
    обнуляются, а при нескольких воркерах у каждого свои.
    """

    MAX_KEYS = 10_000  # защита памяти от перебора случайных ключей

    def __init__(self, max_failures: int, window_seconds: int, clock=time.monotonic):
        self.max_failures = max_failures
        self.window = window_seconds
        self._clock = clock
        self._failures: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()  # обычные (не async) эндпоинты работают в потоках

    def _alive(self, key: str, now: float):
        """Очередь свежих неудач по ключу или None, если их нет."""
        queue = self._failures.get(key)
        if queue is None:
            return None
        while queue and now - queue[0] > self.window:
            queue.popleft()
        if not queue:
            del self._failures[key]
            return None
        return queue

    def retry_after(self, key: str) -> int:
        """0 — можно пробовать, иначе сколько секунд подождать."""
        with self._lock:
            now = self._clock()
            queue = self._alive(key, now)
            if queue is None or len(queue) < self.max_failures:
                return 0
            return int(self.window - (now - queue[0])) + 1

    def register_failure(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            if len(self._failures) >= self.MAX_KEYS:
                for k in list(self._failures):
                    self._alive(k, now)  # выбрасываем устаревшие ключи
            self._failures[key].append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._failures.clear()