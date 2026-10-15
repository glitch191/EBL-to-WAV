"""E-mu Emulator X banks (.exb + SamplePool/*.ebl) -> WAV or FLAC files.

One audio file per .ebl sample, in ``<destination>/<bank name>/<sample name>.wav``. The bank name
comes from the .exb file next to the SamplePool folder, else from the folder or archive name.
Everything that comes out of an archive goes under the archive's name and keeps the archive's
folder layout: ``<destination>/<archive>/<sub-folders>/<bank>/<sample name>.wav``.
WAV files keep the sample's loop in a ``smpl`` chunk; FLAC files cannot hold it.
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import struct
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

from . import ebl

# --------------------------------------------------------------------------- destination

def documents_dir() -> str:
    """The current user's Documents folder, as Windows reports it (follows OneDrive or other
    redirections); ~/Documents elsewhere or if the lookup fails."""
    try:
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("data", ctypes.c_ubyte * 16)]

        folder = GUID()
        folder.data[:] = list(uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}").bytes_le)  # FOLDERID_Documents
        path = wintypes.LPWSTR()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(path)) == 0:
            try:
                return path.value
            finally:
                ctypes.windll.ole32.CoTaskMemFree(path)
    except (AttributeError, OSError, ValueError):
        pass
    return os.path.join(os.path.expanduser("~"), "Documents")


DEFAULT_DESTINATION = os.path.join(documents_dir(), "Samples")
FORMATS = ("wav", "flac")
MANIFEST = ".ebl-to-wav"  # per bank folder: every file this tool wrote there
SAMPLE_POOL = "samplepool"


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:100] or "Untitled"


def bank_name(name: str) -> str:
    """"Proteus 2000.exb" (a folder or archive named after the bank) -> "Proteus 2000"."""
    name = re.sub(r"\.exb$", "", name.strip(), flags=re.I)
    return safe_name(name)


# --------------------------------------------------------------------------- jobs

@dataclass
class Job:
    src: str        # .ebl file
    name: str       # sample name stored in the file
    bank: str       # top output folder: the bank, or the archive the sample came from
    folder: str     # output folder relative to the destination ("Bank" or "Archive\Sub\Bank")
    category: str   # E-mu category ("Piano", "Percussion"...), often empty
    channels: int
    rate: int
    frames: int
    loop: bool
    dst: str        # output .wav / .flac
    root: str       # bank folder
    weight: int     # bytes of audio (for time estimates)


ARCHIVES = (".zip", ".7z")
_temp_dirs: list[str] = []


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path.rstrip("\\/")))[0]


# Scan progress: ``progress(text, done, total)`` is called from the scanning thread; ``done`` and
# ``total`` are byte counts while unpacking an archive, ``None`` when the step has no measure.

def _no_progress(text: str, done: int | None = None, total: int | None = None) -> None:
    pass


def _safe_member_path(out: str, name: str) -> str | None:
    """Where an archive member goes under ``out``: no absolute paths, no "..", no characters
    Windows forbids. None if it would still end up outside ``out``."""
    parts = [re.sub(r'[<>:"|?*\x00-\x1f]', "_", p).rstrip(" .") for p in re.split(r"[\\/]+", name)]
    parts = [p for p in parts if p and p not in (".", "..")]
    if not parts:
        return None
    target = os.path.abspath(os.path.join(out, *parts))
    return target if os.path.commonpath([target, os.path.abspath(out)]) == os.path.abspath(out) else None


class _ByteProgress:
    """Thread-safe byte counter shared by the extraction threads. ``add`` reports progress and
    raises ScanCancelled for every thread once one of them was told to stop."""

    def __init__(self, label: str, total: int, progress) -> None:
        self.label, self.total, self.progress = label, total, progress
        self.done = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        progress(label, 0, total)

    def add(self, n: int) -> None:
        if self.stop.is_set():
            raise ScanCancelled()
        with self.lock:
            self.done += n
            done = min(self.done, self.total)
        try:
            self.progress(self.label, done, self.total)
        except ScanCancelled:
            self.stop.set()
            raise


def _parallel(tasks: list, progress: _ByteProgress) -> None:
    """Run ``tasks`` (callables) on one thread per logical CPU; the first failure stops the others
    and is raised (a cancellation wins over other errors)."""
    workers = max(1, min(os.cpu_count() or 1, len(tasks)))
    if workers == 1:
        for task in tasks:
            task()
        return
    errors = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(task) for task in tasks]:
            try:
                future.result()
            except BaseException as e:
                progress.stop.set()
                errors.append(e)
    if errors:
        raise next((e for e in errors if isinstance(e, ScanCancelled)), errors[0])


def _extract_zip(path: str, out: str, label: str, progress) -> None:
    """Members are decompressed in parallel (zlib releases the GIL), each thread with its own
    handle on the archive."""
    import zipfile
    with zipfile.ZipFile(path) as z:
        members = z.infolist()
    files = []
    for m in members:
        target = _safe_member_path(out, m.filename)
        if target is None:
            continue
        if m.is_dir():
            os.makedirs(target, exist_ok=True)
        else:
            files.append((m, target))
    files.sort(key=lambda f: -f[0].file_size)  # big members first: better load balance
    counter = _ByteProgress(label, sum(m.file_size for m, _ in files), progress)
    local = threading.local()
    handles: list = []
    handles_lock = threading.Lock()

    def extract(member, target) -> None:
        z = getattr(local, "zip", None)
        if z is None:
            z = local.zip = zipfile.ZipFile(path)
            with handles_lock:
                handles.append(z)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with z.open(member) as src, open(target, "wb") as dst:
            while chunk := src.read(2**20):
                dst.write(chunk)
                counter.add(len(chunk))

    try:
        _parallel([lambda m=m, t=t: extract(m, t) for m, t in files], counter)
    finally:
        for z in handles:
            z.close()


# --- .7z: one big block / solid archive -> 7-Zip's own decoder if installed (several times
# faster); one block per file -> py7zr (faster there); no 7-Zip, or 7-Zip fails -> py7zr.

_7zip_path: list = []  # cache: [path or None]


def find_7zip() -> str | None:
    """7-Zip's 7z.exe if it is installed (set E2W_7ZIP=off to ignore it)."""
    if os.environ.get("E2W_7ZIP", "").lower() == "off":
        return None
    if _7zip_path:
        return _7zip_path[0]
    candidates = []
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\7-Zip", 0, winreg.KEY_READ | view) as key:
                        for value in ("Path64", "Path"):
                            try:
                                candidates.append(os.path.join(winreg.QueryValueEx(key, value)[0], "7z.exe"))
                            except OSError:
                                pass
                except OSError:
                    pass
    except ImportError:
        pass
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if os.environ.get(env):
            candidates.append(os.path.join(os.environ[env], "7-Zip", "7z.exe"))
    candidates.append(shutil.which("7z") or "")
    found = next((c for c in candidates if c and os.path.isfile(c)), None)
    _7zip_path.append(found)
    return found


def _extract_7z_native(exe: str, path: str, out: str, counter: _ByteProgress) -> None:
    """Run 7z.exe (hidden window); its percentage output drives the progress; a cancellation
    kills it. 7-Zip itself drops absolute paths and ".." from member names."""
    import subprocess
    cmd = [exe, "x", path, f"-o{out}", "-y", "-p", "-mmt=on", "-bso0", "-bsp1", "-bb0"]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    errors: list[bytes] = []
    reader = threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)
    reader.start()
    reported = 0
    try:
        while chunk := proc.stdout.read1(256):
            percents = re.findall(rb"(\d{1,3})%", chunk)
            if percents:
                now = counter.total * min(100, int(percents[-1])) // 100
                if now > reported:
                    counter.add(now - reported)
                    reported = now
    except ScanCancelled:
        proc.kill()
        proc.wait()
        raise
    code = proc.wait()
    reader.join(timeout=5)
    if code >= 2:  # 0 = OK, 1 = warnings, 2+ = errors
        message = b"".join(errors).decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"7-Zip exit code {code}: {message[-1] if message else ''}")
    if counter.total > reported:
        counter.add(counter.total - reported)


def _extract_7z_py(path: str, out: str, counter: _ByteProgress) -> None:
    """py7zr through our own file writers, called from the extracting thread while data is
    decompressed: progress follows the data and a ScanCancelled stops the extraction at once."""
    import py7zr
    import py7zr.io

    writers: list = []
    writers_lock = threading.Lock()

    class Writer(py7zr.io.Py7zIO):
        def __init__(self, target: str | None) -> None:
            self.file = None
            if target:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                self.file = open(target, "wb")
            self.length = 0

        def write(self, data) -> int:
            counter.add(len(data))
            self.length += len(data)
            return self.file.write(data) if self.file else len(data)

        def read(self, size=None) -> bytes: return b""
        def seek(self, offset, whence=0) -> int: return 0
        def size(self) -> int: return self.length

        def flush(self) -> None:
            if self.file:
                self.file.flush()

        def close(self) -> None:
            if self.file:
                self.file.close()
                self.file = None

    class Factory(py7zr.io.WriterFactory):
        def create(self, filename: str) -> "Writer":
            # py7zr passes the full output path (out + member name): keep the member part.
            full, root = os.path.abspath(filename), os.path.abspath(out)
            if os.path.isabs(filename) and os.path.commonpath([full, root]) == root:
                filename = os.path.relpath(full, root)
            writer = Writer(_safe_member_path(out, filename))
            with writers_lock:
                writers.append(writer)
            return writer

    try:
        with py7zr.SevenZipFile(path, "r") as z:
            z.extractall(out, factory=Factory())
    finally:
        for writer in writers:
            writer.close()


def _extract_7z(path: str, out: str, label: str, progress) -> None:
    import py7zr
    with py7zr.SevenZipFile(path, "r") as z:
        info = z.archiveinfo()
        entries = [e for e in z.list() if not e.is_directory]
    counter = _ByteProgress(label, sum(e.uncompressed or 0 for e in entries), progress)
    blocks = info.blocks or 1
    if len(entries) > 1 and blocks >= len(entries):  # non-solid: every file is its own block
        _extract_7z_py(path, out, counter)
        return
    exe = find_7zip()
    if exe:
        try:
            _extract_7z_native(exe, path, out, counter)
            return
        except ScanCancelled:
            raise
        except Exception:  # 7-Zip missing a codec, damaged install...: start again with py7zr
            shutil.rmtree(out, ignore_errors=True)
            os.makedirs(out, exist_ok=True)
            counter = _ByteProgress(label, counter.total, progress)
    _extract_7z_py(path, out, counter)


def extract_archive(path: str, progress=_no_progress) -> str:
    """Unpack a .zip / .7z into a temporary folder (removed by ``cleanup_temp``), reporting the
    uncompressed bytes written so far."""
    out = tempfile.mkdtemp(prefix=TEMP_PREFIX)
    _temp_dirs.append(out)
    label = f"Unpacking {os.path.basename(path)}"
    if path.lower().endswith(".7z"):
        _extract_7z(path, out, label, progress)
    else:
        _extract_zip(path, out, label, progress)
    return out


TEMP_PREFIX = "ebl-to-wav_"


class ScanCancelled(Exception):
    """Raised by a progress callback to stop a scan (e.g. the window is closing)."""


def cleanup_temp() -> None:
    while _temp_dirs:
        shutil.rmtree(_temp_dirs.pop(), ignore_errors=True)


def cleanup_stale_temp(max_age_hours: float = 6.0) -> None:
    """Remove unpacked archives left behind by an earlier session that could not clean up
    (killed, crashed). Recent folders are kept: another open window may still be using them."""
    import time
    limit = time.time() - max_age_hours * 3600
    base = tempfile.gettempdir()
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in names:
        path = os.path.join(base, name)
        try:
            if name.startswith(TEMP_PREFIX) and os.path.isdir(path) and os.path.getmtime(path) < limit:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _bank_of(path: str, hint: str | None, base: str | None) -> str:
    """Bank name for an .ebl file: the .exb next to its SamplePool folder, else that folder's
    name; ``hint`` (archive or dropped folder name) when the file sits at the top of ``base``."""
    folder = os.path.dirname(path)
    if os.path.basename(folder).lower() == SAMPLE_POOL:
        folder = os.path.dirname(folder)
        bank = _single_exb(folder)
        if bank:
            return bank
    if hint and base and os.path.normcase(folder) == os.path.normcase(base):
        return bank_name(hint)
    return bank_name(os.path.basename(folder.rstrip("\\/")) or "E-MU Bank")


def _single_exb(folder: str) -> str | None:
    try:
        exb = [n for n in os.listdir(folder) if n.lower().endswith(".exb")
               and os.path.isfile(os.path.join(folder, n))]
    except OSError:
        return None
    return bank_name(_stem(exb[0])) if len(exb) == 1 else None


def _archive_tree(folder: str, base: str, own: str) -> tuple[str, ...]:
    """Output sub-folders for a folder inside an unpacked archive: the archive's own layout, with
    each "<bank>.exb/SamplePool" pair shown as the bank name, and without a leading folder that
    repeats the archive name ("Bank.zip" holding "Bank/..." would give "Bank/Bank/...")."""
    rel = os.path.relpath(folder, base)
    parts = [] if rel == "." else rel.split(os.sep)
    out: list[str] = []
    for i, part in enumerate(parts):
        if part.lower() == SAMPLE_POOL:
            bank = _single_exb(os.path.join(base, *parts[:i]))
            if bank:
                if out:
                    out[-1] = bank
                else:
                    out.append(bank)
                continue
        out.append(bank_name(part))
    if out and out[0].lower() == own.lower():
        out = out[1:]
    return tuple(out)


def _scan(path: str, hint: str | None, base: str | None, depth: int, found: list, errors: list,
          progress=_no_progress, archive: tuple | None = None) -> int:
    """Collect (.ebl file, output folder name, sub-folders) under ``path``; returns the
    ignored-file count. ``archive`` = (output folder name, unpacked root, sub-folder prefix) for
    files that come out of an archive: they keep the archive's layout under its name."""
    ignored = 0
    low = path.lower()
    if os.path.isdir(path):
        hint, base = hint or os.path.basename(path.rstrip("\\/")), base or path
        progress(f"Looking through {os.path.basename(path.rstrip(chr(92) + '/')) or path}")
        for folder, _, names in os.walk(path):
            for n in sorted(names):
                if n.lower().endswith((".ebl",) + ARCHIVES):
                    ignored += _scan(os.path.join(folder, n), hint, base, depth, found, errors, progress, archive)
    elif low.endswith(ARCHIVES) and os.path.isfile(path):
        if depth >= 3:
            return 0
        try:
            out = extract_archive(path, progress)
            name = bank_name(_stem(path))
            if archive:  # archive inside an archive: under its place in the outer layout
                top, outer, prefix, own = archive
                inner = (top, out, prefix + _archive_tree(os.path.dirname(path), outer, own) + (name,), name)
            else:
                inner = (name, out, (), name)
            ignored += _scan(out, _stem(path), out, depth + 1, found, errors, progress, inner)
        except ScanCancelled:
            raise
        except Exception as e:  # corrupt / encrypted / unsupported archive
            errors.append((path, f"unreadable archive ({e})"))
    elif low.endswith(".exb") and os.path.isfile(path):
        pool = next((os.path.join(os.path.dirname(path), n) for n in os.listdir(os.path.dirname(path) or ".")
                     if n.lower() == SAMPLE_POOL and os.path.isdir(os.path.join(os.path.dirname(path), n))), None)
        if pool is None:
            errors.append((path, "no SamplePool folder next to it"))
        else:
            ignored += _scan(pool, _stem(path), pool, depth, found, errors, progress, archive)
    elif low.endswith(".ebl") and os.path.isfile(path):
        if archive:
            top, root, prefix, own = archive
            found.append((path, top, prefix + _archive_tree(os.path.dirname(path), root, own)))
        else:
            found.append((path, _bank_of(path, hint, base), ()))
    else:
        ignored += 1
    return ignored


def _natural(text: str) -> list:
    """Sort key: "SL2" before "SL10"."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def _unique(dst: str, seen: set[str]) -> str:
    stem, ext = os.path.splitext(dst)
    n, out = 2, dst
    while out.lower() in seen:
        out, n = f"{stem} ({n}){ext}", n + 1
    seen.add(out.lower())
    return out


def collect_jobs(paths: list[str], destination: str, fmt: str = "wav",
                 progress=_no_progress) -> tuple[list[Job], list[tuple[str, str]], int]:
    """One job per .ebl sample in ``paths`` (.exb files, folders and .zip / .7z archives are
    searched too). Returns (jobs, [(file, error)], ignored_count)."""
    found: list = []
    errors: list = []
    ignored = sum(_scan(os.path.abspath(p), None, None, 0, found, errors, progress) for p in paths)
    jobs: list[Job] = []
    seen: set[str] = set()
    done: set[str] = set()
    found.sort(key=lambda f: ([_natural(p) for p in (f[1],) + f[2]], _natural(os.path.basename(f[0]))))
    for i, (path, bank, sub) in enumerate(found):
        key = os.path.normcase(path)
        if key in done:
            continue
        done.add(key)
        if i % 50 == 0:
            progress(f"Reading {len(found)} sample(s)")
        try:
            s = ebl.read_header(path)
        except (OSError, ValueError, struct.error) as e:
            errors.append((path, str(e)))
            continue
        name = s.name or _stem(path)
        root = os.path.join(destination, bank)
        dst = _unique(os.path.join(root, *sub, safe_name(name) + "." + fmt), seen)
        jobs.append(Job(path, name, bank, os.path.join(bank, *sub), s.category, s.channels, s.rate, s.frames,
                        s.loop is not None, dst, root, s.frames * s.channels * 2))
    return jobs, errors, ignored


# --------------------------------------------------------------------------- writing

def wav_bytes(audio: np.ndarray, rate: int, loop: tuple[int, int] | None) -> bytes:
    """16-bit PCM WAV; a loop goes into a ``smpl`` chunk (start and end frames inclusive),
    which samplers read as the sustain loop."""
    channels = audio.shape[1]
    data = np.ascontiguousarray(audio, dtype="<i2").tobytes()
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * channels * 2, channels * 2, 16)
    chunks = [b"fmt " + struct.pack("<I", len(fmt)) + fmt]
    if loop:
        smpl = struct.pack("<9I", 0, 0, round(1e9 / rate), 60, 0, 0, 0, 1, 0)
        smpl += struct.pack("<6I", 0, 0, loop[0], loop[1], 0, 0)
        chunks.append(b"smpl" + struct.pack("<I", len(smpl)) + smpl)
    chunks.append(b"data" + struct.pack("<I", len(data)) + data + (b"\0" if len(data) % 2 else b""))
    body = b"WAVE" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def tmp_path(dst: str) -> str:
    """Temporary name an output is written under before it is moved into place."""
    return dst + ".tmp"


def process_job(job: Job) -> dict:
    res = {"dst": job.dst, "src": job.src, "size": 0, "status": "ok", "message": "Converted"}
    tmp = tmp_path(job.dst)
    try:
        sample = ebl.read_header(job.src)
        audio = ebl.read_audio(job.src, sample)
        os.makedirs(os.path.dirname(job.dst), exist_ok=True)
        if job.dst.lower().endswith(".flac"):
            import soundfile as sf
            sf.write(tmp, audio, sample.rate, subtype="PCM_16", format="FLAC")
        else:
            with open(tmp, "wb") as f:
                f.write(wav_bytes(audio, sample.rate, sample.loop))
        os.replace(tmp, job.dst)
        res["size"] = os.path.getsize(job.dst)
    except Exception as e:  # one broken sample must not stop the batch
        res.update(status="error", message=f"Error: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
    return res


# Files this tool wrote in a bank folder are listed in a hidden manifest: a later run may replace
# or remove them, anything else in the folder belongs to the user and is never touched.

def _read_manifest(root: str) -> set[str]:
    try:
        with open(os.path.join(root, MANIFEST), encoding="utf-8") as f:
            return {os.path.normpath(os.path.join(root, line.strip())) for line in f if line.strip()}
    except OSError:
        return set()


def _write_manifest(root: str, files: set[str]) -> None:
    path = os.path.join(root, MANIFEST)
    files = sorted(os.path.relpath(f, root) for f in files if os.path.isfile(f))
    try:
        if not files:
            if os.path.exists(path):
                os.remove(path)
            return
        if os.path.exists(path):
            ctypes.windll.kernel32.SetFileAttributesW(path, 0x80)  # normal, so it can be rewritten
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(files) + "\n")
        ctypes.windll.kernel32.SetFileAttributesW(path, 0x2)  # hidden
    except (OSError, AttributeError):
        pass


def existing_outputs(root: str) -> set[str]:
    """Files a previous run of this tool wrote under ``root``."""
    return {p for p in _read_manifest(root) if os.path.isfile(p)}


def remove_stale(root: str, keep: set[str], before: set[str]) -> int:
    """Delete files written by an earlier run that this run did not rewrite (for example .wav
    files after switching to FLAC), then empty folders. Files the user added are never touched."""
    removed = 0
    keep_l = {k.lower() for k in keep}
    for path in before:
        if path.lower() not in keep_l:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    for base, dirs, files in os.walk(root, topdown=False):
        if base != root and not dirs and not files:
            try:
                os.rmdir(base)
            except OSError:
                pass
    return removed


def finalize(root: str, before: set[str], results: list[dict], complete: bool) -> int:
    """After a run: on completion, remove stale outputs; always update the manifest.
    A sample that failed keeps its previous file, if any."""
    attempted = {r["dst"] for r in results}
    removed = remove_stale(root, attempted, before) if complete else 0
    written = {r["dst"] for r in results if r["status"] == "ok"}
    _write_manifest(root, written | (before & attempted if complete else before))
    return removed


# --------------------------------------------------------------------------- runner

WORKERS_OVERRIDE = int(os.environ.get("E2W_WORKERS", "0"))  # benchmarks only


def plan_workers(jobs: list[Job]) -> int:
    """Threads: reading and writing is mostly disk work, and numpy, file I/O and libsndfile
    release the GIL."""
    return max(1, min(WORKERS_OVERRIDE or os.cpu_count() or 1, 8, len(jobs)))


class Runner:
    """Thread pool; results arrive in ``events`` as ("result", dict), then ("done", None)
    or ("cancelled", None)."""

    def __init__(self, events) -> None:
        self.events = events
        self._pool: ThreadPoolExecutor | None = None
        self._pending = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.workers = 0

    @property
    def running(self) -> bool:
        return self._pool is not None

    def submit(self, jobs: list[Job]) -> None:
        if not jobs:
            return
        with self._lock:
            if self._pool is None:
                self.workers = plan_workers(jobs)
                self._pool = ThreadPoolExecutor(max_workers=self.workers)
                self._stop.clear()
            self._pending += len(jobs)
            pool = self._pool
        for job in sorted(jobs, key=lambda j: -j.weight):  # big samples first
            pool.submit(self._run, job)

    def _run(self, job: Job) -> None:
        if self._stop.is_set():
            return
        res = process_job(job)
        if self._stop.is_set():  # cancelled meanwhile: the file is complete, report it anyway
            self.events.put(("result", res))
            return
        self.events.put(("result", res))
        with self._lock:
            self._pending -= 1
            finished = self._pending == 0 and self._pool is not None
            pool = self._pool if finished else None
            if finished:
                self._pool = None
        if pool is not None:
            pool.shutdown(wait=False)
            self.events.put(("done", None))

    def cancel(self) -> None:
        """Stop now: queued samples are skipped, the ones being written are finished."""
        with self._lock:
            pool, self._pool, self._pending = self._pool, None, 0
        if pool is None:
            return
        self._stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        self.events.put(("cancelled", None))


def run_blocking(paths: list[str], destination: str, fmt: str = "wav", on_result=None) -> list[dict]:
    """Headless conversion (CLI / tests), with the same replace + cleanup as the window."""
    import queue
    try:
        jobs, errors, _ = collect_jobs(paths, destination, fmt)
        for path, err in errors:
            print(f"[error] {path}: {err}")
        roots = {j.root for j in jobs}
        before = {r: existing_outputs(r) for r in roots}
        events: queue.Queue = queue.Queue()
        Runner(events).submit(jobs)
        results = []
        while len(results) < len(jobs):
            kind, payload = events.get()
            if kind == "result":
                results.append(payload)
                if on_result:
                    on_result(payload)
        root_of = {j.dst: j.root for j in jobs}
        for root in roots:
            finalize(root, before[root], [r for r in results if root_of[r["dst"]] == root], True)
        return results
    finally:
        cleanup_temp()
