"""A machine-wide limit on concurrent Ollama generations (the GPU), shared by every ResumeOps process.

The dashboard's automation worker, its import worker and the resume watcher all call Ollama. Ollama queues
requests it cannot serve at once, and a queued request still counts against the caller's socket timeout and
stream deadline, so a burst of work turned into timeouts. Here a caller waits for a free GPU slot first (without
any timeout running), then generates.

A slot is an exclusive SQLite transaction on one small file per slot: the operating system releases it when the
holder exits or crashes, so a slot is never lost. The gate is re-entrant within a thread (a generation that
retries in a larger window keeps its slot). RESUMEOPS_GPU_SLOTS (default 1, at most 8) sets the number of slots;
RESUMEOPS_GPU_GATE sets the folder (default: <temp>/resumeops-gpu).
"""
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_held = threading.local()


def slots():
    try:
        return max(1, min(8, int(os.environ.get('RESUMEOPS_GPU_SLOTS', '1'))))
    except ValueError:
        return 1


def folder():
    return Path(os.environ.get('RESUMEOPS_GPU_GATE') or Path(tempfile.gettempdir()) / 'resumeops-gpu')


def _try(index):
    path = folder() / f'slot-{index}.sqlite3'
    connection = sqlite3.connect(path, timeout=0, isolation_level=None, check_same_thread=False)
    try:
        connection.execute('BEGIN EXCLUSIVE')
        return connection
    except sqlite3.OperationalError:
        connection.close()
        return None


@contextmanager
def gpu_slot(poll=0.2, on_wait=None):
    """Hold one GPU slot for the block. ``on_wait(seconds)`` is called while waiting (for progress text)."""
    if getattr(_held, 'depth', 0):
        _held.depth += 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    folder().mkdir(parents=True, exist_ok=True)
    started, connection = time.monotonic(), None
    while connection is None:
        for index in range(slots()):
            connection = _try(index)
            if connection is not None:
                break
        if connection is None:
            if on_wait:
                on_wait(time.monotonic() - started)
            time.sleep(poll)
    _held.depth = 1
    try:
        yield
    finally:
        _held.depth = 0
        try:
            connection.execute('ROLLBACK')
        finally:
            connection.close()


def busy():
    """How many slots are held right now (for the operator's view)."""
    folder().mkdir(parents=True, exist_ok=True)
    held = 0
    for index in range(slots()):
        connection = _try(index)
        if connection is None:
            held += 1
        else:
            connection.execute('ROLLBACK')
            connection.close()
    return held
