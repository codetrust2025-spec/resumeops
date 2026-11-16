"""Atomic records and a cross-process lock for a local workspace."""
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path


def write_json(path, content):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.' + path.name + '-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            with stream:
                json.dump(content, stream, indent=2, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def workspace_lock(directory, name):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / name, timeout=5)
    try:
        connection.execute('BEGIN EXCLUSIVE')
        yield
    finally:
        connection.close()
