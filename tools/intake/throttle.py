"""Adaptive model-call concurrency: AIMD below an operator-configured cap.

``qualify --concurrency N`` is a ceiling, never a target to exceed: the limiter starts at
``initial`` (default N; a non-adaptive run always holds N), halves its limit when a call meets a rate limit (HTTP 429) or
overload (HTTP 503), and adds one back after a window of clean replies, always staying
within ``[1, N]``. Every model call of a run, on any thread, passes through it, so at most
``limit`` calls are in flight. It decides only how many calls overlap: what is reviewed, the
order of ledger work, token accounting and every outcome are unchanged.
"""
import collections
import contextlib
import threading
import time

THROTTLE_CODES = frozenset({"http-429", "http-503"})


class Limiter:
    def __init__(self, cap, *, initial=None, adaptive=True, decrease=0.5, clock=time.monotonic):
        initial = cap if initial is None or not adaptive else initial
        if (type(cap) is not int or cap < 1 or type(initial) is not int
                or not 1 <= initial <= cap or not 0 < decrease < 1):
            raise ValueError("invalid concurrency limits")
        self.cap, self.initial, self.adaptive, self._decrease = cap, initial, bool(adaptive), decrease
        self._clock, self._limit = clock, float(initial)
        self._cond, self._local = threading.Condition(), threading.local()
        self._queue = collections.deque()
        self._in_flight = self._calls = self._max_in_flight = 0
        self._generation = self._clean = self._backoffs = self._signals = 0
        self._lowest = initial
        self._busy, self._started = 0.0, clock()
        self._mark = self._started

    @property
    def limit(self):
        return int(self._limit)

    def _tick(self):
        now = self._clock()
        self._busy += self._in_flight * (now - self._mark)
        self._mark = now

    def acquire(self):
        """Wait, in arrival order, for a free slot; this thread then holds one."""
        with self._cond:
            me = object()
            self._queue.append(me)
            try:
                while self._queue[0] is not me or self._in_flight >= self.limit:
                    self._cond.wait()
            except BaseException:
                self._queue.remove(me)
                self._cond.notify_all()
                raise
            self._queue.popleft()
            self._tick()
            self._in_flight += 1
            self._max_in_flight = max(self._max_in_flight, self._in_flight)
            self._local.slot = [self._generation, False]
            self._cond.notify_all()

    def release(self, clean):
        """Free this thread's slot; a clean reply with no throttle signal earns additive increase."""
        with self._cond:
            signalled = self._local.slot[1]
            self._local.slot = None
            self._tick()
            self._in_flight -= 1
            self._calls += 1
            if clean and not signalled and self.adaptive:
                self._clean += 1
                if self._clean >= self.limit and self._limit < self.cap:
                    self._limit, self._clean = min(self.cap, self._limit + 1), 0
            self._cond.notify_all()

    def throttled(self, code=None):
        """A call of this thread met a rate limit or overload: multiplicative decrease.

        Calls already running when the limit last fell cannot lower it again with their first
        signal, so a burst of simultaneous 429s from one overload costs one halving, not one per
        call. A call that is throttled again after waiting is fresh evidence and lowers it.
        """
        slot = getattr(self._local, "slot", None)
        if slot is None:
            return
        with self._cond:
            fresh = slot[0] == self._generation or slot[1]
            slot[1] = True
            self._signals += 1
            if self.adaptive and fresh:
                self._generation += 1
                slot[0] = self._generation
                before, self._limit = self._limit, max(1.0, self._limit * self._decrease)
                self._clean = 0
                self._backoffs += self._limit < before
                self._lowest = min(self._lowest, self.limit)

    def _observe(self, code):
        if code in THROTTLE_CODES:
            self.throttled(code)

    def attempt(self, fn):
        """Run one HTTP attempt ``fn() -> (reply, failure)`` in a slot, then free it.

        ``failure`` is None or ``(code, retryable, retry_after)``. A 429/503 lowers the limit
        while the slot is still held; the slot is released before the transport waits to retry,
        and the retry takes a slot again under the then-current limit.
        """
        self.acquire()
        clean = False
        try:
            result = fn()
            failure = result[1]
            if failure is not None:
                self._observe(failure[0])
            clean = failure is None
            return result
        finally:
            self.release(clean)

    @contextlib.contextmanager
    def gating(self, transport):
        """Yield ``transport`` gated by this limiter.

        A transport with a ``gate`` attribute (``usai_transport``) is gated per HTTP attempt
        through ``attempt``, so retries after a decrease respect the lowered limit. Any other
        transport is gated per call; a raised ``TransportError`` carrying a throttle code lowers
        the limit.
        """
        if hasattr(transport, "gate"):
            transport.gate = self.attempt
            try:
                yield transport
            finally:
                transport.gate = None
            return

        def gated(model, messages):
            self.acquire()
            clean = False
            try:
                reply = transport(model, messages)
                clean = True
                return reply
            except Exception as error:
                self._observe(getattr(error, "code", None))
                raise
            finally:
                self.release(clean)
        yield gated

    def report(self):
        """Body-free effective rate of this run's model calls."""
        with self._cond:
            self._tick()
            seconds = self._mark - self._started
            return {"cap": self.cap, "initial": self.initial, "adaptive": self.adaptive,
                    "final": self.limit, "lowest": self._lowest, "backoffs": self._backoffs,
                    "throttle_signals": self._signals, "max_in_flight": self._max_in_flight,
                    "calls": self._calls, "seconds": round(seconds, 3),
                    "mean_in_flight": round(self._busy / seconds, 2) if seconds > 0 else 0.0,
                    "calls_per_minute": round(self._calls * 60 / seconds, 1) if seconds > 0 else 0.0}
