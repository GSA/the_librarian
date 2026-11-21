"""Forked worker processes for CPU-bound, read-only checks over many independent items.

Used by the selection identity check (``renditions``) and the integrity audit (``integrity``). A caller
sets the job (shared copy-on-write with every worker), names a module-level function that receives one
``(start, stop)`` chunk and reads the job from ``JOB``, and gets each chunk's outcome back in order.
This is an accept-only fast path: ``run`` returns ``None`` whenever it cannot or should not fork, and
the caller then does the same work sequentially.
"""

import concurrent.futures
import gc
import multiprocessing
import threading

JOB = None


def run(job, function, total, workers):
    """Outcomes of ``function((start, stop))`` over chunks covering ``range(total)``, or None.

    None means the work was not done in workers: fewer than two workers or no items, a process that
    already has other threads (a child would inherit whatever lock another thread held), no ``fork``
    start method, or any pool failure.
    """
    global JOB
    if (workers < 2 or total < 1 or threading.active_count() > 1
            or "fork" not in multiprocessing.get_all_start_methods()):
        return None
    size = -(-total // (workers * 8))
    chunks = [(start, min(start + size, total)) for start in range(0, total, size)]
    JOB = job
    gc.collect()
    gc.freeze()  # keep the collector from touching, and so copying, the shared heap in every worker
    try:
        with concurrent.futures.ProcessPoolExecutor(
                max_workers=workers, mp_context=multiprocessing.get_context("fork")) as pool:
            futures = [pool.submit(function, chunk) for chunk in chunks]
            return [future.result() for future in futures]
    except Exception:
        return None
    finally:
        JOB = None
        gc.unfreeze()
