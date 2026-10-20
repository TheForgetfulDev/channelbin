"""A DNS responder for a simulated world's host names, so a host can stop resolving.

Inside a container network the simulator names its hosts `<host>.<domain>` (`Simulator(...,
domain=...)`), and this answers for those names: an A record with the host's address, no
record of any other type, and NXDOMAIN for a name it does not hold or one that is marked down.
A name that will not resolve is its own failure, distinct from a host that refuses
connections (the world's `hosts_down`): ffmpeg reports it as `Failed to resolve hostname`,
which is what ChannelBin's host roll reacts to (dev/changelog/1168).

`alias()` adds a second name for a host's address, the way a reseller hands out several names
for one server; taking one name down leaves the other resolving.

UDP only, one question per query - what a stub resolver sends. Every query is counted, so a
scenario can tell that a client asked.
"""
import socket
import struct
import threading
from collections import Counter

THREAD_NAME = 'sim-dns'

TYPE_A = 1
CLASS_IN = 1
RCODE_NXDOMAIN = 3
# Response, authoritative, recursion desired echoed and available: what a resolver that owns
# the zone answers.
_FLAGS = 0x8400 | 0x0080
_TTL = 1


class DnsResponder:
    """Answers for `names` ({DNS name: IPv4 address}) on (address, port) UDP."""

    def __init__(self, names: dict, address: str = '0.0.0.0', port: int = 53):
        self._names = {n.lower().rstrip('.'): a for n, a in names.items()}
        self._down = set()
        self._lock = threading.Lock()
        self.queries = Counter()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((address, port))
        self._sock.settimeout(0.1)
        self.address = self._sock.getsockname()[:2]
        self._stopping = threading.Event()
        self._thread = None

    # --- what resolves --------------------------------------------------------------------

    def resolve(self, name: str) -> str | None:
        """The address `name` answers with right now, or None for NXDOMAIN."""
        key = name.lower().rstrip('.')
        with self._lock:
            if key in self._down:
                return None
            return self._names.get(key)

    def down(self, name: str) -> None:
        """Answer NXDOMAIN for `name` from now on."""
        with self._lock:
            key = self._known(name)
            self._down.add(key)

    def up(self, name: str) -> None:
        with self._lock:
            self._down.discard(self._known(name))

    def alias(self, name: str, target: str) -> None:
        """Make `name` resolve to the address `target` resolves to."""
        with self._lock:
            address = self._names[self._known(target)]
            self._names[name.lower().rstrip('.')] = address

    def state(self) -> dict:
        with self._lock:
            return {'names': dict(sorted(self._names.items())), 'down': sorted(self._down),
                    'queries': dict(self.queries)}

    def _known(self, name: str) -> str:
        key = name.lower().rstrip('.')
        if key not in self._names:
            raise KeyError(f'no DNS name {name!r}; known: {", ".join(sorted(self._names))}')
        return key

    # --- serving --------------------------------------------------------------------------

    def start(self) -> 'DnsResponder':
        self._thread = threading.Thread(target=self._serve, name=THREAD_NAME, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError('the DNS responder thread did not stop')
            self._thread = None
        self._sock.close()

    def _serve(self):
        while not self._stopping.is_set():
            try:
                data, peer = self._sock.recvfrom(1500)
            except socket.timeout:
                continue
            except OSError:
                return      # the socket was closed by stop()
            reply = self.answer(data)
            if reply is not None:
                try:
                    self._sock.sendto(reply, peer)
                except OSError:
                    pass    # the client's port is gone; a resolver retries on its own

    def answer(self, query: bytes) -> bytes | None:
        """The reply to one query, or None for bytes that are not a query to answer."""
        parsed = parse_query(query)
        if parsed is None:
            return None
        tid, rd, name, qtype, qclass, question = parsed
        address = self.resolve(name)
        with self._lock:
            self.queries[name] += 1
        flags = _FLAGS | (0x0100 if rd else 0)
        if address is None:
            return struct.pack('>HHHHHH', tid, flags | RCODE_NXDOMAIN, 1, 0, 0, 0) + question
        if qtype != TYPE_A or qclass != CLASS_IN:
            # The name exists and has no record of this type (NODATA): an AAAA lookup must
            # not read as the name being gone.
            return struct.pack('>HHHHHH', tid, flags, 1, 0, 0, 0) + question
        record = (b'\xc0\x0c' + struct.pack('>HHIH', TYPE_A, CLASS_IN, _TTL, 4)
                  + socket.inet_aton(address))
        return struct.pack('>HHHHHH', tid, flags, 1, 1, 0, 0) + question + record


def parse_query(data: bytes):
    """(id, recursion desired, name, type, class, question bytes) of a one-question query, or
    None."""
    if len(data) < 12:
        return None
    tid, flags, qdcount = struct.unpack('>HHH', data[:6])
    if flags & 0x8000 or qdcount != 1:
        return None
    i, labels = 12, []
    while True:
        if i >= len(data):
            return None
        n = data[i]
        if n == 0:
            break
        if n & 0xC0 or i + 1 + n > len(data):
            return None
        labels.append(data[i + 1:i + 1 + n].decode('ascii', 'replace'))
        i += n + 1
    if i + 5 > len(data):
        return None
    qtype, qclass = struct.unpack('>HH', data[i + 1:i + 5])
    return tid, bool(flags & 0x0100), '.'.join(labels).lower(), qtype, qclass, data[12:i + 5]


def build_query(name: str, qtype: int = TYPE_A, tid: int = 1) -> bytes:
    """A one-question query with recursion desired, for tests and the control API's own
    checks."""
    q = b''.join(bytes([len(p)]) + p.encode('ascii') for p in name.rstrip('.').split('.'))
    return struct.pack('>HHHHHH', tid, 0x0100, 1, 0, 0, 0) + q + b'\x00' + struct.pack(
        '>HH', qtype, CLASS_IN)
