"""Seeded randomness the simulator can replay to the byte.

Every random choice the simulator makes is drawn from a `random.Random` seeded by `derive()`,
which hashes the world seed together with a fixed label for what is being drawn. Python's own
`hash()` is never used: it is salted per process, so anything seeded from it would differ
between two runs of the same world.
"""
import bisect
import hashlib
import random


def derive(seed, *parts) -> int:
    """A 64-bit integer fixed by `seed` and `parts`, the same in every process and on every
    platform. Parts are joined with a unit separator, so ('ab', 'c') and ('a', 'bc') differ."""
    text = '\x1f'.join(str(p) for p in (seed, *parts))
    return int.from_bytes(hashlib.sha256(text.encode('utf-8')).digest()[:8], 'big')


def rng(seed, *parts) -> random.Random:
    """A generator of its own for one purpose. Draws from it never shift any other's."""
    return random.Random(derive(seed, *parts))


class Weighted:
    """A fixed weighted choice over (value, weight) pairs, built once and drawn many times.

    Only `Random.random()` is used, whose sequence for an integer seed has been stable across
    CPython releases, so a world draws the same values on every supported version."""

    def __init__(self, pairs):
        pairs = [(v, float(w)) for v, w in pairs if w and float(w) > 0]
        if not pairs:
            raise ValueError('a weighted choice needs at least one positive weight')
        self.values = [v for v, _ in pairs]
        total = 0.0
        self._cum = []
        for _, w in pairs:
            total += w
            self._cum.append(total)
        self._total = total
        # random() * total can round up to total itself, one past the last bucket.
        self._cum[-1] = float('inf')

    def pick(self, r: random.Random):
        return self.values[bisect.bisect_right(self._cum, r.random() * self._total)]


def percentile_sampler(points: dict):
    """A sampler for a distribution known only by a few percentiles, e.g.
    {'p10': 23, 'p50': 60, 'p90': 120, 'p99': 240}: piecewise linear between the given points,
    with p0 taken as half of the lowest and p100 as the highest. Returns f(r) -> float."""
    knots = sorted((float(k[1:]) / 100.0, float(v)) for k, v in points.items()
                   if k.startswith('p') and v is not None)
    if not knots:
        raise ValueError('no percentiles to sample from')
    if knots[0][0] > 0:
        knots.insert(0, (0.0, knots[0][1] / 2.0))
    if knots[-1][0] < 1:
        knots.append((1.0, knots[-1][1]))
    qs = [q for q, _ in knots]

    def sample(r: random.Random) -> float:
        u = r.random()
        i = max(1, bisect.bisect_right(qs, u))
        i = min(i, len(knots) - 1)
        (q0, v0), (q1, v1) = knots[i - 1], knots[i]
        if q1 == q0:
            return v1
        return v0 + (v1 - v0) * (u - q0) / (q1 - q0)

    return sample
