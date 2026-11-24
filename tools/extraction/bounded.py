"""Run a child process in its own session with a wall-clock timeout, an output cap and a tree kill.

Shared by the stdlib converter worker and the sandboxed LibreOffice converter. Standard library only.
"""
import os
import select
import signal
import subprocess
import time


class BoundedError(Exception):
    """A refused or failed child run; ``code`` is a stable hyphenated reason."""

    def __init__(self, code, message, detail=""):
        super().__init__(message)
        self.code = code
        self.detail = detail  # a stderr tail for operators; never stored with a document's disposition


def terminate(process):
    """Stop ``process``'s process group: TERM, then KILL, then reap."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=2)
            break
        except subprocess.TimeoutExpired:
            continue
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run(command, timeout, limit, *, env, stderr_limit, cwd=None, diagnostics=None):
    """Run ``command`` in its own session; return (returncode, stdout) or raise ``BoundedError``.

    ``conversion-timeout`` when the wall clock runs out, ``conversion-limit`` when stdout exceeds
    ``limit``, ``converter-runtime-failed`` when the launch fails. The process group is killed on every
    exit path, so a descendant that outlives its leader dies with it. When ``diagnostics`` is a list, the
    last bytes of stderr (at most ``stderr_limit``) are appended to it.
    """
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True, env=env, cwd=cwd)
    except OSError as error:
        raise BoundedError("converter-runtime-failed", "Converter launch failed") from error
    out, err, size, tail = [], 0, 0, b""
    deadline = time.monotonic() + timeout
    streams = {process.stdout, process.stderr}
    try:
        while streams:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BoundedError("conversion-timeout", "Conversion exceeded its wall-clock timeout")
            ready, _, _ = select.select(list(streams), [], [], min(remaining, 1.0))
            for stream in ready:
                chunk = os.read(stream.fileno(), 1024 * 1024)
                if not chunk:
                    streams.discard(stream)
                elif stream is process.stdout:
                    size += len(chunk)
                    if size > limit:
                        raise BoundedError("conversion-limit", "Converter output exceeds its limit")
                    out.append(chunk)
                else:
                    err = min(err + len(chunk), stderr_limit)
                    tail = (tail + chunk)[-stderr_limit:]
        remaining = max(deadline - time.monotonic(), 0.1)
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise BoundedError("conversion-timeout", "Conversion exceeded its wall-clock timeout") from error
    finally:
        if process.returncode is None:
            terminate(process)
        else:
            try:  # descendants may outlive their leader
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        process.stdout.close()
        process.stderr.close()
        if diagnostics is not None:
            diagnostics.append(tail)
    return process.returncode, b"".join(out)
