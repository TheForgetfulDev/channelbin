"""The request ledger: every request the simulator answered, in the order it answered them.

A test reads it to check what a client actually asked for - how many times, of which host, as
which login - against what the client says it did.
"""
import threading
from dataclasses import asdict, dataclass, replace
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class Request:
    seq: int
    at: float               # the simulator's clock, seconds since the epoch, when answered
    host: str               # the world's name for the host that answered
    role: str               # panel, stream, guide-site or logos
    client: str
    method: str
    path: str
    query: dict
    endpoint: str           # auth, categories, catalog, playlist, guide, logo, stream or other
    user_agent: str
    login: str | None       # the username the credentials resolved to, if they did
    status: int
    bytes: int              # body bytes written: planned when recorded, actual once amended
    seconds: float          # wall time from request line to the last byte written
    note: str
    faults: tuple           # names of the faults that shaped the answer, in the order applied
    channel: str | None = None      # a stream request's channel, backend:stream id
    offered: tuple | None = None    # channel time a stream sent, (first, last), epoch seconds
    profile: str | None = None      # the loop a stream was cut from (sim/content.py)
    # Channel time this answer sent without its video, per withholding fault: (fault name,
    # first, last) in epoch seconds, read from the packets actually withheld rather than
    # from the fault's window, so it stays known after the fault is cleared.
    withheld: tuple = ()
    open: bool = False              # still being written; amended once the answer ends

    @property
    def action(self) -> str | None:
        return self.query.get('action')

    def as_dict(self) -> dict:
        return asdict(self)


def _matches(row: Request, key: str, value) -> bool:
    if key == 'action':
        return row.action == value
    if key == 'fault':
        return value in row.faults
    return getattr(row, key) == value


def split_target(target: str) -> tuple[str, dict]:
    parts = urlsplit(target)
    return parts.path, {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}


class Ledger:
    def __init__(self):
        self._lock = threading.Lock()
        self._rows: list[Request] = []

    def record(self, **fields) -> Request:
        with self._lock:
            row = Request(seq=len(self._rows) + 1, **fields)
            self._rows.append(row)
        return row

    def amend(self, row: Request, **changes) -> Request:
        """Replace a recorded row with what became known after it was recorded - how much
        was written, how long it took - keeping its place."""
        with self._lock:
            for i in range(len(self._rows) - 1, -1, -1):
                if self._rows[i] is row:
                    self._rows[i] = replace(row, **changes)
                    return self._rows[i]
        return row  # the ledger was cleared since; there is nothing left to amend

    def entries(self, **match) -> list[Request]:
        """Every row whose fields equal `match`; `action=` matches the query's action and
        `fault=` a fault the answer carried."""
        with self._lock:
            rows = list(self._rows)
        return [r for r in rows if all(_matches(r, k, v) for k, v in match.items())]

    def count(self, **match) -> int:
        return len(self.entries(**match))

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)
