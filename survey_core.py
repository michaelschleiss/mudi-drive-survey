"""Asynchronous durable journal and shared numeric helpers, adapted from Uplink Atlas."""
from contextlib import closing
import json
import math
from pathlib import Path
import queue
import sqlite3
import tempfile
import threading
import time

def meters(a, b):
    lat1, lat2 = math.radians(a['lat']), math.radians(b['lat'])
    dlat, dlon = lat2-lat1, math.radians(b['lon']-a['lon'])
    h = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 12742000 * math.asin(min(1, math.sqrt(h)))


def finite(value):
    v = float(value)
    if not math.isfinite(v):
        raise ValueError('Expected a finite number')
    return v


class Journal:
    """One SQLite writer; acquisition never waits for disk or an HTTP client."""
    def __init__(self, path):
        self.temporary = tempfile.TemporaryDirectory(prefix='cell-hunt-') if str(path) == ':memory:' else None
        self.path = Path(self.temporary.name)/'session.sqlite3' if self.temporary else Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.pending = queue.Queue(maxsize=8000)
        self.error = ''
        self.saved = 0
        self.stopping = threading.Event()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, kind TEXT, ts REAL, data TEXT)')
            self.saved = db.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def history(self):
        with closing(sqlite3.connect(self.path)) as db:
            for row in db.execute('SELECT id,kind,ts,data FROM events ORDER BY id'):
                yield dict(json.loads(row[3]), id=row[0], kind=row[1], ts=row[2])

    def append(self, event):
        if self.error:
            return
        try:
            self.pending.put_nowait(event)
        except queue.Full:
            self.error = 'Recording queue is full. Stop and check storage.'

    def _write(self):
        try:
            with closing(sqlite3.connect(self.path)) as db:
                while not self.stopping.is_set() or not self.pending.empty():
                    try:
                        batch = [self.pending.get(timeout=.2)]
                    except queue.Empty:
                        continue
                    until = time.monotonic()+.15
                    while len(batch) < 256 and time.monotonic() < until:
                        try:
                            batch.append(self.pending.get(timeout=.001 if self.stopping.is_set() else max(.001, until-time.monotonic())))
                        except queue.Empty:
                            break
                    db.executemany('INSERT INTO events VALUES (?,?,?,?)',
                                   [(e['id'], e['kind'], e['ts'], json.dumps(e, allow_nan=False)) for e in batch])
                    db.commit()
                    self.saved = batch[-1]['id']
        except Exception as exc:
            self.error = f'Recording failed: {type(exc).__name__}. Check free disk space.'

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=10)
        if self.temporary and not self.thread.is_alive():
            self.temporary.cleanup()
