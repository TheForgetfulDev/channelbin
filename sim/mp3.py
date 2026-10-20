"""MP3 framing, for the radio mounts: a rendered MP3 loop served frame by frame in real time."""
from dataclasses import dataclass

# MPEG-1 Layer III, kb/s by bitrate index; and sample rates by index.
_BITRATES = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_RATES = (44100, 48000, 32000, 0)
SAMPLES_PER_FRAME = 1152


class Mp3Error(ValueError):
    """Bytes that are not a run of MPEG-1 Layer III frames."""


def _frame_length(b, o) -> tuple[int, int]:
    """(frame bytes, sample rate) of the frame header at `o`."""
    if b[o] != 0xFF or (b[o + 1] & 0xFE) != 0xFA:
        raise Mp3Error(f'no MPEG-1 Layer III frame header at byte {o}')
    kbps = _BITRATES[b[o + 2] >> 4]
    rate = _RATES[(b[o + 2] >> 2) & 0x03]
    if not kbps or not rate:
        raise Mp3Error(f'a free-format or reserved frame header at byte {o}')
    return 144_000 * kbps // rate + ((b[o + 2] >> 1) & 1), rate


@dataclass
class Mp3Loop:
    """A loop of whole MP3 frames: `offsets[i]` is where frame i starts."""
    name: str
    data: bytes
    offsets: list
    sample_rate: int

    @classmethod
    def parse(cls, data: bytes, name: str = '') -> 'Mp3Loop':
        offsets, rate, o = [], None, 0
        while o + 4 <= len(data):
            length, r = _frame_length(data, o)
            if rate is None:
                rate = r
            elif r != rate:
                raise Mp3Error(f'{name or "loop"}: the sample rate changes at byte {o}')
            if o + length > len(data):
                break
            offsets.append(o)
            o += length
        if not offsets:
            raise Mp3Error(f'{name or "loop"}: no whole frame')
        return cls(name, data[:o], offsets + [o], rate)

    @property
    def frames(self) -> int:
        return len(self.offsets) - 1

    def cut(self, f0: int, f1: int) -> bytes:
        """Frames f0 to f1 (exclusive), counted across passes of the loop."""
        out = []
        n = self.frames
        while f0 < f1:
            k, i0 = divmod(f0, n)
            i1 = min(n, i0 + (f1 - f0))
            out.append(self.data[self.offsets[i0]:self.offsets[i1]])
            f0 += i1 - i0
        return b''.join(out)
