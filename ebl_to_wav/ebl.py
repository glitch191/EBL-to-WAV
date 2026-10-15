"""Reader for E-mu Emulator X sample files (.ebl).

Layout (big-endian chunk headers, little-endian sample header and audio):

    "FORM" size
    "E5B0TOC2" toc_length        table of contents, 78 bytes per entry:
        "E5S1" length offset index(2) name(64, UTF-16LE)
    at offset: "E5S1" length, then the chunk body:
        +0   6 bytes             +4 = header version (1 or 2)
        +6   name (64, UTF-16LE)
        +70  12 x uint32 LE      unknown, ch1 start, ch2 start, ch1 end, ch2 end,
                                 ch1 loop start, ch2 loop start, ch1 loop end, ch2 loop end,
                                 sample rate, flags (0x10000 = loop on), unknown
        +118 category (UTF-16BE, e.g. "Piano"; empty in older files)

Positions are byte offsets from the chunk body + 2; "end" and "loop end" point at the last
sample (inclusive). Audio is 16-bit PCM, one channel after the other (not interleaved).
Version 1 headers hold one channel: their "ch2 start" field is the end of the data instead.
Measured on 3,089 files from four banks; the unknown fields are not needed to play the audio.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

HEADER_READ = 512  # enough for every header field
LOOP_FLAG = 0x10000


class EBLError(ValueError):
    """The file is not a readable .ebl sample."""


@dataclass
class Sample:
    name: str
    category: str
    rate: int
    channels: int
    frames: int
    loop: tuple[int, int] | None  # first and last frame of the loop (inclusive)
    spans: list[tuple[int, int]]  # absolute byte range of each channel's audio, end exclusive

    @property
    def seconds(self) -> float:
        return self.frames / self.rate if self.rate else 0.0


def _utf16(raw: bytes, codec: str = "utf-16-le") -> str:
    return raw.decode(codec, "replace").split("\0")[0].strip()


def read_header(path: str) -> Sample:
    """Read the sample description without loading the audio."""
    with open(path, "rb") as f:
        head = f.read(HEADER_READ)
        f.seek(0, 2)
        size = f.tell()
    if len(head) < 0x20 or head[:4] != b"FORM" or head[8:16] != b"E5B0TOC2":
        raise EBLError("not an E-mu .ebl file")
    offset = struct.unpack(">I", head[0x1C:0x20])[0]
    body = offset + 8
    if head[offset:offset + 4] != b"E5S1" or body + 182 > len(head):
        raise EBLError("no sample chunk found")
    length = struct.unpack(">I", head[offset + 4:body])[0]
    end_of_chunk = min(size, body + length)
    version = head[body + 4]
    name = _utf16(head[body + 6:body + 70]) or _utf16(head[0x22:0x62])
    category = _utf16(head[body + 118:body + 182], "utf-16-be")
    (_, s1, s2, e1, e2, loop_start, _, loop_end, _, rate, flags, _) = struct.unpack(
        "<12I", head[body + 70:body + 118])
    base = body + 2
    spans = [(base + s1, base + e1 + 2)]
    if version >= 2 and s2 != s1:
        spans.append((base + s2, base + e2 + 2))
    for start, end in spans:
        if not (base <= start < end <= end_of_chunk) or (end - start) % 2:
            raise EBLError("audio data out of bounds")
    if not 1000 <= rate <= 384000:
        raise EBLError(f"unexpected sample rate {rate}")
    frames = min((end - start) // 2 for start, end in spans)
    loop = None
    if flags & LOOP_FLAG:
        first, last = (loop_start - s1) // 2, (loop_end - s1) // 2
        if 0 <= first <= last < frames:
            loop = (first, last)
    return Sample(name, category, rate, len(spans), frames, loop, spans)


def read_audio(path: str, sample: Sample) -> np.ndarray:
    """Audio as int16, shape (frames, channels)."""
    out = np.empty((sample.frames, sample.channels), dtype=np.int16)
    with open(path, "rb") as f:
        for ch, (start, _) in enumerate(sample.spans):
            f.seek(start)
            raw = f.read(sample.frames * 2)
            if len(raw) < sample.frames * 2:
                raise EBLError("file is truncated")
            out[:, ch] = np.frombuffer(raw, dtype="<i2")
    return out
