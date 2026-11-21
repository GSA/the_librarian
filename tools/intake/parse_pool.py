"""Worker processes for the pure part of ingest: bytes in, parsed result out (Kata q868).

A job is a top-level function and its arguments (bytes and names, never a path to read) plus the byte
size it holds in the queue. Results reach the caller's ``done(result, error)`` callbacks in SUBMISSION
order, whatever order the workers finish in, and callbacks always run in the caller's thread, so a
report, ledger or catalog never depends on completion order and the caller stays the only writer.

- **Bounded.** ``submit`` waits (delivering finished results) while ``max_pending`` jobs or ``max_bytes``
  of input are queued, so memory is that bound, however big the inbox.
- **A failure is one job's.** A job that raises delivers ``(None, the exception)``. A worker that dies
  (killed, out of memory, a parser crash) delivers ``(None, WorkerDied)`` for the job it held and is
  replaced; other jobs, and the run, continue. The job is reported, not retried: it would only kill
  another worker, and the next run reads the document again.
- **Overlap.** Each worker process has a thread in this process that feeds it and receives its replies, so
  workers keep parsing (and their results are received) while the caller is busy committing.
- **One worker is the old path.** ``workers=1`` runs each job inline with the same callbacks and error
  mapping, so a run's output does not depend on the worker count.
- **Isolated.** Workers are spawned, not forked: they inherit no open ledger, catalog or inbox handle.
"""
import multiprocessing
import os
import queue
import signal
import threading

from .contracts import require

DEFAULT_WORKERS = 4
# An automatically sized pool runs its first jobs inline: starting processes costs more than a small inbox saves.
AUTO_INLINE = 32
MIB = 1024 * 1024
START_METHOD = "spawn"


class WorkerDied(Exception):
    """The worker holding a job exited before returning its result."""


def default_workers():
    return max(1, min(DEFAULT_WORKERS, os.cpu_count() or 1))


def _serve(conn, parent_end):
    """A worker's loop: run each received job, send back its result or its exception, until told to stop."""
    if parent_end is not None:
        parent_end.close()
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the parent stops its workers; Ctrl-C is not theirs
    while True:
        try:
            job = conn.recv()
        except EOFError:
            return  # the parent is gone
        if job is None:
            return
        fn, args = job
        try:
            reply = ("ok", fn(*args))
        except Exception as error:  # noqa: BLE001 - reported to the parent as this job's failure
            reply = ("error", error)
        try:
            conn.send(reply)
        except Exception as error:  # noqa: BLE001 - an unpicklable result or exception
            conn.send(("error", RuntimeError("unsendable job result: " + type(error).__name__)))


class Workers:
    """A bounded pool delivering results in submission order; use as a context manager."""

    def __init__(self, workers, *, max_pending=None, max_bytes=64 * MIB, inline_first=0):
        require(type(workers) is int and workers >= 1, "invalid_concurrency")
        self.workers = workers
        self.max_pending = max_pending or 4 * workers
        self.max_bytes = max_bytes
        self._inline_left = inline_first
        self._jobs = queue.SimpleQueue()
        self._cond = threading.Condition()
        self._threads, self._processes = [], set()
        self._finished = {}  # sequence -> (result, error), received and not yet delivered
        self._callbacks = {}  # sequence -> (done, size) of every job not yet delivered
        self._next = self._delivered = 0
        self._bytes = 0
        self._abandoned = False

    def __enter__(self):
        return self

    def __exit__(self, kind, value, trace):
        self.close(wait=kind is None)

    @property
    def parallel(self):
        """Whether jobs may run in worker processes (``workers > 1``)."""
        return self.workers > 1

    def submit(self, fn, args, size, done):
        """Queue ``fn(*args)`` holding ``size`` input bytes; ``done(result, error)`` runs later, in order.

        Blocks (delivering what has finished) while the queue is full. Returns the job's sequence number
        (see ``deliver_through``), or None when it ran inline and was delivered already.
        """
        if not self.parallel or self._inline_left > 0:
            self._inline_left = max(0, self._inline_left - 1)
            self.drain()  # everything submitted earlier is delivered first
            try:
                result, error = fn(*args), None
            except Exception as failure:  # noqa: BLE001 - this job's failure, as a worker would report it
                result, error = None, failure
            done(result, error)
            return None
        self._deliver()
        while self._callbacks and (len(self._callbacks) >= self.max_pending
                                   or self._bytes + size > self.max_bytes):
            self._wait(self._delivered)
        if not self._threads:
            self._start()
        sequence, self._next = self._next, self._next + 1
        self._callbacks[sequence] = (done, size)
        self._bytes += size
        self._jobs.put((sequence, fn, args))
        return sequence

    def deliver_through(self, sequence):
        """Wait for job ``sequence`` and every job submitted before it, and deliver their results."""
        if sequence is not None and self._delivered <= sequence:
            self._wait(sequence)

    def drain(self):
        """Wait for every submitted job and deliver its result."""
        if self._callbacks:
            self._wait(self._next - 1)

    def close(self, wait=True):
        if not wait:
            self._abandoned = True  # queued jobs are dropped, not run on fresh workers
            for process in list(self._processes):
                process.terminate()  # a feeder thread then sees the worker die and ends
        for _ in self._threads:
            self._jobs.put(None)
        for thread in self._threads:
            thread.join(30)
        for process in list(self._processes):
            if process.is_alive():
                process.kill()
        self._threads.clear()

    def _start(self):
        context = multiprocessing.get_context(START_METHOD)
        for number in range(self.workers):
            thread = threading.Thread(target=self._feed, args=(context,), daemon=True, name="parse-worker-%d" % number)
            thread.start()
            self._threads.append(thread)

    def _feed(self, context):
        """One worker's thread: start its process on demand, run one job at a time, replace it when it dies."""
        process = conn = None
        while True:
            job = self._jobs.get()
            if job is None:
                break
            sequence, fn, args = job
            if self._abandoned:
                continue
            try:
                if process is None:
                    conn, child = context.Pipe()
                    process = context.Process(target=_serve, args=(child, conn), daemon=True)
                    process.start()
                    child.close()
                    self._processes.add(process)
                conn.send((fn, args))
                kind, value = conn.recv()
                outcome = (value, None) if kind == "ok" else (None, value)
            except (EOFError, ConnectionError, OSError):
                # The worker exited while holding the job (or could not be reached): this job is its casualty.
                self._retire(process, conn)
                process = conn = None
                outcome = (None, WorkerDied("parse worker exited while holding a job"))
            except Exception as error:  # noqa: BLE001 - unpicklable arguments fail this job alone
                outcome = (None, error)
            with self._cond:
                self._finished[sequence] = outcome
                self._cond.notify_all()
        if process is not None:
            try:
                conn.send(None)
            except OSError:
                pass
            process.join(5)
            self._retire(process, conn)

    def _retire(self, process, conn):
        if conn is not None:
            conn.close()
        if process is not None:
            if process.is_alive():
                process.terminate()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join()
            self._processes.discard(process)

    def _wait(self, sequence):
        """Deliver, in order, every result up to job ``sequence``, waiting for the ones still running."""
        while self._delivered <= sequence:
            with self._cond:
                while self._delivered not in self._finished:
                    self._cond.wait()
            self._deliver()

    def _deliver(self):
        while True:
            with self._cond:
                if self._delivered not in self._finished:
                    return
                result, error = self._finished.pop(self._delivered)
            done, size = self._callbacks.pop(self._delivered)
            self._bytes -= size
            self._delivered += 1
            done(result, error)
