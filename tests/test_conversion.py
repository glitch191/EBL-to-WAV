"""End-to-end tests on small .ebl files generated on the fly (no copyrighted material).

    py -m unittest discover -s tests -v
"""

import os
import shutil
import struct
import tempfile
import unittest
import zipfile

import numpy as np
import soundfile as sf

from ebl_to_wav import ebl, engine

RATE = 44100


def tone(frames: int, freq: float = 440.0) -> np.ndarray:
    t = np.arange(frames) / RATE
    return (np.sin(2 * np.pi * freq * t) * 12000).astype(np.int16)


# --------------------------------------------------------------------------- minimal EBL writer

def _utf16(text: str, codec: str = "utf-16-le") -> bytes:
    return text.encode(codec).ljust(64, b"\0")[:64]


def ebl_bytes(name: str, channels: list[np.ndarray], loop: tuple[int, int] | None = None,
              rate: int = RATE, category: str = "", version: int = 2, gap: int = 0, tail: int = 0) -> bytes:
    """An .ebl file laid out as Emulator X writes it (see ebl.py). ``gap`` padding samples go
    between the channels, ``tail`` bytes after the audio (both occur in real files)."""
    head = 184 if version == 1 else 188  # first audio byte, relative to the chunk body + 2
    n = len(channels[0])
    starts = [head + c * (n + gap) * 2 for c in range(len(channels))]
    ends = [s + (n - 1) * 2 for s in starts]
    loop_start = head + loop[0] * 2 if loop else head
    loop_end = head + loop[1] * 2 if loop else head - 2
    if version == 1:
        fields = (0, head, ends[0] + 2, ends[0], ends[0], loop_start, ends[0] + 2, loop_end, ends[0] + 2)
    else:
        s2, e2 = (starts[1], ends[1]) if len(channels) > 1 else (starts[0], ends[0])
        fields = (0, starts[0], s2, ends[0], e2, loop_start, loop_start + s2 - starts[0], loop_end,
                  loop_end + s2 - starts[0])
    flags = 0x10000 if loop else 0
    header = (struct.pack(">I", 0) + struct.pack("<BB", version, 0) + _utf16(name)
              + struct.pack("<12I", *fields, rate, flags, 0x02010101) + _utf16(category, "utf-16-be"))
    audio = b""
    for c, data in enumerate(channels):
        audio += data.astype("<i2").tobytes() + (b"\0\0" * gap if c < len(channels) - 1 else b"")
    body = header.ljust(head + 2, b"\0") + audio + b"\0" * tail
    toc = b"E5S1" + struct.pack(">II", len(body), 0x62) + b"\0\0" + _utf16(name)
    chunk = b"E5S1" + struct.pack(">I", len(body)) + body
    form = b"E5B0TOC2" + struct.pack(">I", len(toc)) + toc + chunk
    return b"FORM" + struct.pack(">I", len(form)) + form


def write_bank(folder: str, name: str = "My Bank") -> str:
    """<folder>/<name>.exb/{<name>.exb, SamplePool/<name>SL00N.ebl}, as banks are distributed.
    Samples: a looped mono tone, a stereo pair with a padding sample between the channels and a
    40-byte tail, and an older version-1 header. Returns the .exb path."""
    bank = os.path.join(folder, name + ".exb")
    pool = os.path.join(bank, "SamplePool")
    os.makedirs(pool)
    exb = os.path.join(bank, name + ".exb")
    with open(exb, "wb") as f:
        f.write(b"FORM\0\0\0\4E5B0")  # the window never reads it, only its name
    samples = [
        ebl_bytes("Pad C3   ", [tone(1000)], loop=(100, 899), category="Synth"),
        ebl_bytes("Piano/Wide", [tone(800, 330), tone(800, 331)], rate=22050, gap=1, tail=40),
        ebl_bytes("Old Kick", [tone(500, 60)], version=1),
    ]
    for i, data in enumerate(samples, 1):
        with open(os.path.join(pool, f"{name}SL{i:03d}.ebl"), "wb") as f:
            f.write(data)
    return exb


class EBLReader(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="e2w_test_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, data: bytes) -> str:
        path = os.path.join(self.tmp, "x.ebl")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_mono_loop(self):
        path = self.write(ebl_bytes("Pad C3  ", [tone(1000)], loop=(100, 899), category="Synth"))
        s = ebl.read_header(path)
        self.assertEqual((s.name, s.category, s.rate, s.channels, s.frames, s.loop),
                         ("Pad C3", "Synth", RATE, 1, 1000, (100, 899)))
        np.testing.assert_array_equal(ebl.read_audio(path, s)[:, 0], tone(1000))

    def test_stereo_with_gap(self):
        left, right = tone(800, 330), tone(800, 331)
        path = self.write(ebl_bytes("Wide", [left, right], gap=1, tail=40))
        s = ebl.read_header(path)
        self.assertEqual((s.channels, s.frames, s.loop), (2, 800, None))
        audio = ebl.read_audio(path, s)
        np.testing.assert_array_equal(audio[:, 0], left)
        np.testing.assert_array_equal(audio[:, 1], right)

    def test_version_1(self):
        path = self.write(ebl_bytes("Old", [tone(500)], loop=(10, 400), version=1))
        s = ebl.read_header(path)
        self.assertEqual((s.channels, s.frames, s.loop), (1, 500, (10, 400)))

    def test_rejects_other_files(self):
        with self.assertRaises(ebl.EBLError):
            ebl.read_header(self.write(b"RIFF" + b"\0" * 300))
        data = bytearray(ebl_bytes("Short", [tone(100)]))
        with self.assertRaises(ebl.EBLError):
            ebl.read_header(self.write(bytes(data[:-50])))  # audio cut off


class Conversion(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="e2w_test_")
        self.out = os.path.join(self.tmp, "out")
        self.exb = write_bank(self.tmp)

    def tearDown(self) -> None:
        engine.cleanup_temp()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def outputs(self) -> list[str]:
        found = []
        for base, _, names in os.walk(self.out):
            found += [os.path.relpath(os.path.join(base, n), self.out) for n in names if n != engine.MANIFEST]
        return sorted(found)

    def test_exb_bank_to_wav(self):
        results = engine.run_blocking([self.exb], self.out)
        self.assertEqual([r["status"] for r in results], ["ok"] * 3)
        self.assertEqual(self.outputs(), [os.path.join("My Bank", n) for n in
                                          ("Old Kick.wav", "Pad C3.wav", "Piano_Wide.wav")])
        audio, rate = sf.read(os.path.join(self.out, "My Bank", "Piano_Wide.wav"), dtype="int16")
        self.assertEqual((rate, audio.shape), (22050, (800, 2)))
        np.testing.assert_array_equal(audio[:, 1], tone(800, 331))
        with open(os.path.join(self.out, "My Bank", "Pad C3.wav"), "rb") as f:
            data = f.read()
        smpl = data.index(b"smpl") + 8
        self.assertEqual(struct.unpack("<II", data[smpl + 44:smpl + 52]), (100, 899))  # loop start, end
        self.assertEqual(sf.info(os.path.join(self.out, "My Bank", "Pad C3.wav")).frames, 1000)

    def test_same_layout_from_folder_pool_file_and_archive(self):
        bank = os.path.dirname(self.exb)
        archive = shutil.make_archive(os.path.join(self.tmp, "My Bank.exb"), "zip", self.tmp, "My Bank.exb")
        sources = ([bank], [os.path.join(bank, "SamplePool")],
                   [os.path.join(bank, "SamplePool", "My BankSL001.ebl")], [archive])
        for paths in sources:
            jobs, errors, _ = engine.collect_jobs(paths, self.out)
            self.assertEqual(errors, [])
            self.assertEqual({j.bank for j in jobs}, {"My Bank"}, paths)
            engine.cleanup_temp()

    def test_archive_keeps_its_layout_under_its_name(self):
        src = os.path.join(self.tmp, "src")
        write_bank(os.path.join(src, "Synths"), "Bank A")
        os.makedirs(os.path.join(src, "Drums", "Kit"))
        with open(os.path.join(src, "Drums", "Kit", "hit.ebl"), "wb") as f:
            f.write(ebl_bytes("Hit", [tone(300)]))
        with open(os.path.join(src, "Top.ebl"), "wb") as f:
            f.write(ebl_bytes("Top", [tone(300)]))
        inner = os.path.join(self.tmp, "inner")
        write_bank(os.path.join(inner, "More"), "Bank B")  # More.zip holds More/Bank B.exb/...
        os.makedirs(os.path.join(src, "Extras"))
        shutil.make_archive(os.path.join(src, "Extras", "More"), "zip", inner)
        archive = shutil.make_archive(os.path.join(self.tmp, "Collection"), "zip", src)

        results = engine.run_blocking([archive], self.out)
        self.assertEqual({r["status"] for r in results}, {"ok"})
        folders = sorted({os.path.dirname(p) for p in self.outputs()})
        self.assertEqual(folders, [os.path.join("Collection", *p) for p in
                                   ((), ("Drums", "Kit"), ("Extras", "More", "Bank B"), ("Synths", "Bank A"))])
        self.assertEqual(os.listdir(self.out), ["Collection"])  # nothing next to the archive's folder

        # Converting again keeps the layout and removes nothing that is still produced
        before = self.outputs()
        engine.run_blocking([archive], self.out)
        self.assertEqual(self.outputs(), before)

    def test_archive_named_like_its_top_folder_is_not_doubled(self):
        archive = shutil.make_archive(os.path.join(self.tmp, "My Bank"), "zip", self.tmp, "My Bank.exb")
        jobs, _, _ = engine.collect_jobs([archive], self.out)
        self.assertEqual({j.folder for j in jobs}, {"My Bank"})

    def test_loose_samples_take_the_folder_name(self):
        loose = os.path.join(self.tmp, "Loose Hits")
        shutil.copytree(os.path.join(os.path.dirname(self.exb), "SamplePool"), loose)
        jobs, _, _ = engine.collect_jobs([loose], self.out)
        self.assertEqual({j.bank for j in jobs}, {"Loose Hits"})

    def test_flac_then_back_to_wav_replaces_own_files_only(self):
        engine.run_blocking([self.exb], self.out, "flac")
        self.assertTrue(all(p.endswith(".flac") for p in self.outputs()))
        mine = os.path.join(self.out, "My Bank", "notes.txt")
        with open(mine, "w") as f:
            f.write("added by hand")
        engine.run_blocking([self.exb], self.out, "wav")
        self.assertEqual([p for p in self.outputs() if not p.endswith(".wav")],
                         [os.path.join("My Bank", "notes.txt")])

    def test_unreadable_sample_is_reported(self):
        bad = os.path.join(os.path.dirname(self.exb), "SamplePool", "Broken.ebl")
        with open(bad, "wb") as f:
            f.write(b"FORM junk")
        jobs, errors, _ = engine.collect_jobs([self.exb], self.out)
        self.assertEqual(len(jobs), 3)
        self.assertEqual([os.path.basename(p) for p, _ in errors], ["Broken.ebl"])

    def test_exb_without_sample_pool(self):
        lone = os.path.join(self.tmp, "Lone.exb")
        with open(lone, "wb") as f:
            f.write(b"FORM")
        jobs, errors, _ = engine.collect_jobs([lone], self.out)
        self.assertEqual((jobs, len(errors)), ([], 1))

    def test_duplicate_names_get_a_number(self):
        pool = os.path.join(os.path.dirname(self.exb), "SamplePool")
        shutil.copy(os.path.join(pool, "My BankSL001.ebl"), os.path.join(pool, "My BankSL004.ebl"))
        jobs, _, _ = engine.collect_jobs([self.exb], self.out)
        self.assertIn("Pad C3 (2).wav", [os.path.basename(j.dst) for j in jobs])

    def test_zip_slip_is_blocked(self):
        archive = os.path.join(self.tmp, "evil.zip")
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("../../outside.ebl", b"x")
        engine.collect_jobs([archive], self.out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "outside.ebl")))


if __name__ == "__main__":
    unittest.main()
