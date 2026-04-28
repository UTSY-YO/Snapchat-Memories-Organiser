"""
Snapchat Memories Organiser v2.0
=================================
Made by UTSY-YO  |  https://github.com/UTSY-YO  |  April 2026

Matching strategy:
  1. Pre-scan all ZIPs for memories_history.json (master — ZIP 1 only).
  2. For each ZIP, parse its memories_history.html to get UUIDs in order.
  3. Cross-reference HTML UUID order against master JSON by position.
  4. Each UUID maps to exactly one JSON entry → exact time + GPS.
  5. Fallback to positional (date+type bucket) if no HTML present.

Note on timestamps:
  Dates are accurate for all years.
  Times (UTC) may be inconsistent for older memories — Snapchat has changed
  how it stored timezones over time and early exports did not always record
  times accurately.

Requirements:
  pip install Pillow piexif
  ffmpeg and ffprobe on PATH
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
import tkinter as tk
from collections import defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from tkinter import filedialog, scrolledtext, ttk

# ── App identity ──────────────────────────────────────────────────────────────

APP_NAME    = "Snapchat Memories Organiser"
APP_VERSION = "v2.0"
APP_AUTHOR  = "UTSY-YO"
APP_GITHUB  = "https://github.com/UTSY-YO"
APP_DATE    = "April 2026"

# ── Dependencies ──────────────────────────────────────────────────────────────

try:
    from PIL import Image
    PILLOW_OK  = True
    PILLOW_VER = Image.__version__
except ImportError:
    PILLOW_OK  = False
    PILLOW_VER = None

try:
    import piexif
    PIEXIF_OK = True
except ImportError:
    PIEXIF_OK = False

def _check_cli(tool):
    try:
        r = subprocess.run([tool, "-version"], capture_output=True, timeout=5)
        parts = r.stdout.decode(errors="replace").split("\n")[0].split()
        return True, (parts[2] if len(parts) > 2 else "found")
    except Exception:
        return False, None

FFMPEG        = "ffmpeg"
FFPROBE       = "ffprobe"
FFMPEG_OK,  FFMPEG_VER  = _check_cli(FFMPEG)
FFPROBE_OK, FFPROBE_VER = _check_cli(FFPROBE)

MEDIA_EXT = {".jpg", ".jpeg", ".png", ".mp4", ".mov", ".gif", ".webp"}

_IS_WIN = sys.platform == "win32"
_IS_MAC = sys.platform == "darwin"

if _IS_WIN:
    _INST_PILLOW  = "pip install Pillow"
    _INST_PIEXIF  = "pip install piexif"
    _INST_FFMPEG  = "winget install ffmpeg   (then restart this app)"
    _INST_FFPROBE = "Installed with ffmpeg — see above."
elif _IS_MAC:
    _INST_PILLOW  = "pip3 install Pillow"
    _INST_PIEXIF  = "pip3 install piexif"
    _INST_FFMPEG  = "brew install ffmpeg   (requires Homebrew — brew.sh)"
    _INST_FFPROBE = "Installed with ffmpeg — see above."
else:
    _INST_PILLOW  = "pip3 install Pillow"
    _INST_PIEXIF  = "pip3 install piexif"
    _INST_FFMPEG  = "sudo apt install ffmpeg"
    _INST_FFPROBE = "Installed with ffmpeg — see above."

UUID_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})_"
    r"([A-F0-9]{8}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{12})"
    r"-(main|overlay)\.(jpg|jpeg|mp4|mov|png|gif|webp)$",
    re.IGNORECASE,
)

# ── Datetime helpers ──────────────────────────────────────────────────────────

def parse_dt(s):
    """Parse datetime string → naive UTC datetime or None."""
    for fmt in (
        "%Y-%m-%d %H:%M:%S %Z",
        "%Y-%m-%d %H:%M:%S UTC",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            pass
    return None



def utc_ts(dt_utc):
    """
    UTC naive datetime → Unix timestamp.
    Must use .replace(tzinfo=utc) to avoid Python treating
    it as local time — which would shift by 10-11 hrs on Sydney machines.
    """
    return dt_utc.replace(tzinfo=timezone.utc).timestamp()

# ── File utilities ────────────────────────────────────────────────────────────

def set_file_times(path, dt_utc):
    """Set file modified/accessed time. On Windows also sets creation time."""
    ts = utc_ts(dt_utc)
    os.utime(path, (ts, ts))
    if not _IS_WIN:
        return
    try:
        import ctypes
        from ctypes import wintypes
        h = ctypes.windll.kernel32.CreateFileW(
            str(path), 0x40000000, 0, None, 3, 0x80, None)
        if h == ctypes.c_void_p(-1).value:
            return
        v  = int(ts * 10_000_000) + 116444736000000000
        ft = wintypes.FILETIME(v & 0xFFFFFFFF, v >> 32)
        ctypes.windll.kernel32.SetFileTime(
            h, ctypes.byref(ft), ctypes.byref(ft), ctypes.byref(ft))
        ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        pass


def safe_dest(folder, base, ext):
    p = folder / f"{base}{ext}"
    i = 1
    while p.exists():
        p = folder / f"{base}_{i:03d}{ext}"
        i += 1
    return p

# ── HTML parser ───────────────────────────────────────────────────────────────

_HTML_UUID_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})_"
    r"([A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}"
    r"-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12})"
    r"-(main)\.(jpg|jpeg|mp4|mov|png|gif|webp)",
    re.IGNORECASE,
)


class _MemHTMLParser(HTMLParser):
    """
    Parse memories_history.html by extracting UUID filenames from src= attributes
    on img and video tags — only 'main' files, not overlays.

    The HTML lists every memory with its UUID filename in src= in document order.
    This order matches the JSON entry order exactly.
    Result: uuid_order = [(file_date, UUID_UPPERCASE), ...] in HTML document order.
    """
    def __init__(self):
        super().__init__()
        self.uuid_order = []

    def handle_starttag(self, tag, attrs):
        if tag not in ("img", "video", "source"):
            return
        attrs_dict = dict(attrs)
        src = attrs_dict.get("src", "")
        m   = _HTML_UUID_RE.search(src)
        if m:
            file_date = m.group(1)
            uuid      = m.group(2).upper()  # normalise lowercase UUIDs to upper
            self.uuid_order.append((file_date, uuid))


def parse_html(html_bytes):
    """
    Parse memories_history.html.
    Returns [(file_date, UUID), ...] in document order — main files only.
    Position i here corresponds to JSON entry i for this ZIP.
    """
    p = _MemHTMLParser()
    p.feed(html_bytes.decode("utf-8", errors="replace"))
    return p.uuid_order

# ── ffprobe helpers ───────────────────────────────────────────────────────────

def probe_dimensions(path):
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries",
             "stream=width,height,codec_type:stream_side_data=rotation",
             "-of", "json", str(path)],
            capture_output=True, timeout=30)
        if r.returncode == 0:
            streams = json.loads(r.stdout.decode()).get("streams", [])
            if streams:
                w, h     = streams[0].get("width"), streams[0].get("height")
                rotation = 0
                for sd in streams[0].get("side_data_list", []):
                    if "rotation" in sd:
                        rotation = abs(int(sd["rotation"]))
                if rotation in (90, 270):
                    w, h = h, w
                if w and h:
                    return int(w), int(h)
    except Exception:
        pass
    return None, None


def probe_frame_count(path):
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=nb_frames,duration,r_frame_rate",
             "-of", "json", str(path)],
            capture_output=True, timeout=30)
        if r.returncode == 0:
            streams = json.loads(r.stdout.decode()).get("streams", [])
            if streams:
                s = streams[0]
                if "nb_frames" in s:
                    return int(s["nb_frames"])
                if "duration" in s and "r_frame_rate" in s:
                    a, b = s["r_frame_rate"].split("/")
                    fps  = float(a) / float(b) if float(b) else 30
                    return int(float(s["duration"]) * fps)
    except Exception:
        pass
    return None


def image_dimensions(path):
    if PILLOW_OK:
        try:
            with Image.open(path) as img:
                return img.size
        except Exception:
            pass
    return probe_dimensions(path)


def video_has_audio(path):
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(path)],
            capture_output=True, timeout=10)
        return bool(r.stdout.strip())
    except Exception:
        return False

# ── Compositing ───────────────────────────────────────────────────────────────

def composite_image(main_path, overlay_path, out_path):
    try:
        base = Image.open(main_path).convert("RGBA")
        ov   = Image.open(overlay_path).convert("RGBA")
        if ov.size != base.size:
            bw, bh = base.size
            ow, oh = ov.size
            scale  = min(bw / ow, bh / oh)
            nw, nh = int(ow * scale), int(oh * scale)
            ov     = ov.resize((nw, nh), Image.LANCZOS)
            canvas = Image.new("RGBA", base.size, (0, 0, 0, 0))
            canvas.paste(ov, ((bw - nw) // 2, (bh - nh) // 2), ov)
            ov = canvas
        Image.alpha_composite(base, ov).convert("RGB").save(out_path, quality=95)
        return True
    except Exception:
        shutil.copy2(main_path, out_path)
        return False


def composite_video(main_path, overlay_path, out_path, warn_fn,
                    progress_fn=None, kill_flag=None, hard_timeout=600):
    vw, vh       = probe_dimensions(main_path)
    ow, oh       = image_dimensions(overlay_path)
    total_frames = probe_frame_count(main_path)

    if vw and vh and ow and oh:
        filt = (
            f"[1:v]scale={vw}:{vh}:force_original_aspect_ratio=decrease,"
            f"pad={vw}:{vh}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,"
            f"format=rgba[ov];[0:v][ov]overlay=0:0[out]"
        )
    elif vw and vh:
        filt = f"[1:v]scale={vw}:{vh},format=rgba[ov];[0:v][ov]overlay=0:0[out]"
    else:
        filt = "[1:v]format=rgba[ov];[0:v][ov]overlay=0:0[out]"

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as pf:
        pipe = pf.name

    cmd = [FFMPEG, "-y",
           "-i", str(main_path), "-loop", "1", "-i", str(overlay_path),
           "-filter_complex", filt, "-map", "[out]"]
    if video_has_audio(main_path):
        cmd += ["-map", "0:a", "-c:a", "copy"]
    else:
        cmd += ["-an"]
    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-shortest",
        "-progress", pipe, "-loglevel", "warning", str(out_path),
    ]

    proc = None
    try:
        flags = subprocess.CREATE_NO_WINDOW if _IS_WIN else 0
        proc  = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=flags)

        job_start     = time.time()
        last_activity = time.time()
        last_frame    = 0
        slow_since    = None

        while proc.poll() is None:
            time.sleep(1)

            if kill_flag and kill_flag.is_set():
                warn_fn(f"Skipped by user: {out_path.name}")
                proc.kill(); proc.wait()
                _restore(out_path, main_path)
                return False

            if time.time() - job_start > hard_timeout:
                warn_fn(f"Hard timeout ({hard_timeout // 60} min) for "
                        f"{out_path.name} — original saved.")
                proc.kill(); proc.wait()
                _restore(out_path, main_path)
                return False

            try:
                with open(pipe, "r", errors="replace") as pf:
                    content = pf.read()
                frames = re.findall(r"frame=(\d+)", content)
                speeds = re.findall(r"speed=\s*([\d.]+)x", content)
                if frames:
                    cur = int(frames[-1])
                    if cur > last_frame:
                        last_frame    = cur
                        last_activity = time.time()
                    if progress_fn and total_frames:
                        pct     = min(100, int(cur / total_frames * 100))
                        spd_txt = f" ({speeds[-1]}x)" if speeds else ""
                        progress_fn(cur, total_frames, pct, spd_txt)
                    if speeds:
                        spd = float(speeds[-1])
                        if spd < 0.05:
                            if slow_since is None:
                                slow_since = time.time()
                            elif time.time() - slow_since > 60:
                                warn_fn(f"ffmpeg too slow ({spd:.3f}x) for "
                                        f"{out_path.name} — killed, original saved.")
                                proc.kill(); proc.wait()
                                _restore(out_path, main_path)
                                return False
                        else:
                            slow_since = None
            except Exception:
                pass

            if time.time() - last_activity > 60:
                warn_fn(f"ffmpeg stalled on {out_path.name} — killed, original saved.")
                proc.kill(); proc.wait()
                _restore(out_path, main_path)
                return False

        if proc.returncode != 0:
            err = proc.stderr.read().decode(errors="replace")[:300].strip()
            warn_fn(f"ffmpeg error for {out_path.name}: {err}")
            _restore(out_path, main_path)
            return False

        if not out_path.exists() or out_path.stat().st_size < 5_000:
            warn_fn(f"ffmpeg empty output for {out_path.name} — original saved.")
            _restore(out_path, main_path)
            return False

        return True

    except FileNotFoundError:
        warn_fn("ffmpeg not found on PATH — install it and restart the app.")
        _restore(out_path, main_path)
        return False
    except Exception as e:
        warn_fn(f"Unexpected ffmpeg error for {out_path.name}: {e}")
        if proc:
            try: proc.kill(); proc.wait()
            except Exception: pass
        _restore(out_path, main_path)
        return False
    finally:
        try: os.unlink(pipe)
        except Exception: pass


def _restore(out_path, original):
    try:
        if out_path.exists(): out_path.unlink()
    except Exception: pass
    try: shutil.copy2(original, out_path)
    except Exception: pass

# ── Media source ──────────────────────────────────────────────────────────────

class MediaSource:
    def __init__(self, source_path):
        self.path = Path(source_path)
        self._zf  = None
        if self.path.suffix.lower() == ".zip":
            self._zf    = zipfile.ZipFile(self.path, "r")
            self._names = self._zf.namelist()
        else:
            self._names = [
                str(f.relative_to(self.path))
                for f in self.path.rglob("*") if f.is_file()
            ]

    def namelist(self): return self._names

    def read(self, name):
        return self._zf.read(name) if self._zf else (self.path / name).read_bytes()

    def find_html(self):
        """Find memories_history.html — typically at memories/memories_history.html."""
        hits = [n for n in self._names
                if "memories_history" in n.lower() and n.endswith(".html")]
        if not hits:
            return None
        # Prefer the one inside memories/ folder
        hits.sort(key=lambda n: (0 if "memories/" in n.lower() else 1))
        return hits[0]

    def find_json(self):
        hits = [n for n in self._names
                if "memories_history" in n.lower() and n.endswith(".json")]
        return hits[0] if hits else None

    def close(self):
        if self._zf: self._zf.close()

    def __enter__(self):  return self
    def __exit__(self, *_): self.close()


def build_uuid_map(names):
    """
    Build UUID map preserving ZIP namelist order.
    This order matches the HTML row order — do NOT sort.
    """
    uuid_map = {}
    for name in names:
        m = UUID_RE.match(Path(name).name)
        if not m:
            continue
        file_date, uuid, role = m.group(1), m.group(2), m.group(3).lower()
        if uuid not in uuid_map:
            uuid_map[uuid] = {"main": None, "overlay": None,
                              "file_date": file_date}
        uuid_map[uuid][role] = name
    return uuid_map

# ── Master JSON ───────────────────────────────────────────────────────────────

def load_master_json(json_bytes):
    """
    Parse memories_history.json.
    Returns dict keyed by exact UTC datetime string → {lat, lon, type}
    This lets us look up GPS by exact datetime from the HTML.
    Also returns the full entry list sorted oldest-first.
    """
    try:
        data = json.loads(json_bytes.decode("utf-8", errors="replace"))
        raw  = data.get("Saved Media", [])
        # Build lookup by exact datetime string (normalised)
        by_dt    = {}   # "YYYY-MM-DD HH:MM:SS" → {lat, lon, type, date}
        entries  = []
        for item in raw:
            dt = parse_dt(item.get("Date", ""))
            if dt is None:
                continue
            kind = "video" if "video" in item.get("Media Type", "").lower() else "image"
            lat = lon = None
            m = re.search(r"Latitude, Longitude:\s*([-\d.]+),\s*([-\d.]+)",
                          item.get("Location", ""))
            if m:
                lat, lon = float(m.group(1)), float(m.group(2))
            entry = {"date": dt, "type": kind, "lat": lat, "lon": lon}
            entries.append(entry)
            # Key: normalised datetime string for exact lookup
            key = dt.strftime("%Y-%m-%d %H:%M:%S")
            by_dt[key] = entry
        entries.reverse()   # oldest-first
        return entries, by_dt
    except Exception:
        return [], {}

# ── GPS embedding ─────────────────────────────────────────────────────────────

def _dms(deg):
    d = int(abs(deg))
    m = int((abs(deg) - d) * 60)
    s = int(((abs(deg) - d) * 60 - m) * 60 * 100)
    return [(d, 1), (m, 1), (s, 100)]


def embed_gps_jpeg(path, lat, lon, dt_utc):
    if not PIEXIF_OK or lat is None:
        return False
    try:
        try:    exif = piexif.load(str(path))
        except: exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
        exif["GPS"] = {
            piexif.GPSIFD.GPSLatitudeRef:  b"N" if lat >= 0 else b"S",
            piexif.GPSIFD.GPSLatitude:     _dms(lat),
            piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
            piexif.GPSIFD.GPSLongitude:    _dms(lon),
        }
        if dt_utc:
            ds = dt_utc.strftime("%Y:%m:%d %H:%M:%S").encode()
            exif["Exif"][piexif.ExifIFD.DateTimeOriginal] = ds
            exif["0th"][piexif.ImageIFD.DateTime]         = ds
        piexif.insert(piexif.dump(exif), str(path))
        return True
    except Exception:
        return False


def embed_gps_video(path, lat, lon, dt_utc, warn_fn):
    if lat is None:
        return False
    tmp = path.with_suffix(".gpstmp.mp4")
    try:
        slat = f"+{lat:.6f}" if lat >= 0 else f"{lat:.6f}"
        slon = f"+{lon:.6f}" if lon >= 0 else f"{lon:.6f}"
        cmd  = [
            FFMPEG, "-y", "-i", str(path), "-c", "copy",
            "-map_metadata", "0",
            "-metadata", f"location={slat}{slon}/",
            "-metadata", f"location-eng={slat}{slon}/",
        ]
        if dt_utc:
            cmd += ["-metadata",
                    f"creation_time={dt_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"]
        cmd += ["-loglevel", "warning", str(tmp)]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 1000:
            shutil.move(str(tmp), str(path))
            return True
        if tmp.exists(): tmp.unlink()
        return False
    except Exception as e:
        warn_fn(f"GPS embed failed for {path.name}: {e}")
        if tmp.exists():
            try: tmp.unlink()
            except Exception: pass
        return False

# ── Pre-scan: find JSON before processing ─────────────────────────────────────

def find_json_in_queue(queue, log, warn):
    """
    Scan all ZIPs for memories_history.json before touching any media.
    Returns (entry_list, by_datetime_dict) or ([], {}).
    by_datetime_dict lets us look up GPS by exact UTC datetime string.
    """
    for src_path in queue:
        try:
            with MediaSource(src_path) as src:
                json_name = src.find_json()
                if json_name:
                    log(f"  Master JSON found: {Path(src_path).name}")
                    raw            = src.read(json_name)
                    entries, by_dt = load_master_json(raw)
                    if not entries:
                        warn(f"JSON found but could not be parsed in "
                             f"{Path(src_path).name}")
                        return [], {}
                    log(f"  {len(entries):,} entries loaded — GPS + timestamps ready.")
                    return entries, by_dt
        except Exception as e:
            warn(f"Could not scan {Path(src_path).name}: {e}")
    return [], {}

# ── Core processor ────────────────────────────────────────────────────────────

def process_zip(source_path, out_dir, json_by_dt,
                log, warn, progress, video_progress, do_overlay,
                kill_flag=None, hard_timeout=600, on_file_done=None):
    """
    Process one ZIP.

    Matching strategy:
      1. Parse this ZIP's own memories_history.html → ordered list of
         (exact_utc_datetime, type) — one entry per memory in this ZIP.
      2. Each HTML row i corresponds to media file i in the ZIP namelist.
      3. Look up each HTML datetime in master JSON by_dt dict → get GPS.
      4. This guarantees the correct time and GPS for every file.

    Fallback (no HTML):
      Match media files to JSON entries positionally by (date, type) bucket.
      Less accurate for multi-file days but still works.

    Returns: (matched, overlays, skipped, gps_count, failed)
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    matched = skipped = overlays = gps_count = 0
    failed  = []

    with MediaSource(source_path) as src:
        uuid_map = build_uuid_map(src.namelist())
        total    = len(uuid_map)

        if total == 0:
            warn(f"No Snapchat media found in {Path(source_path).name}.")
            return 0, 0, 0, 0, 0, []

        # ── Parse this ZIP's own HTML ─────────────────────────────────────
        # The HTML lists main files by UUID filename in document order.
        # The JSON is in the same global order.
        # We build UUID -> position map so every file gets exact time + GPS.
        html_name        = src.find_html()
        html_rows        = []   # [(file_date, UUID), ...] in HTML order
        html_uuid_to_pos = {}   # UUID -> index in html_rows

        if html_name:
            html_rows = parse_html(src.read(html_name))
            for pos, (_, u) in enumerate(html_rows):
                html_uuid_to_pos[u] = pos
            log(f"  HTML: {len(html_rows)} entries  |  Media: {total}")
            if len(html_rows) != total:
                warn(f"HTML entry count ({len(html_rows)}) != media count "
                     f"({total}) in {Path(source_path).name} — some files "
                     f"may fall back to positional matching.")
        else:
            log(f"  No HTML in this ZIP — using positional fallback.")
            log(f"  Media: {total}")

        # Build ordered JSON list for this ZIP's date range (for HTML matching)
        # Also build per-(date,type) fallback buckets (for when HTML is absent)
        day_counters = defaultdict(int)
        fallback_by_day = defaultdict(list)
        all_json_ordered = sorted(json_by_dt.values(), key=lambda e: e["date"])

        # For HTML matching: get JSON entries in this ZIP's date range
        zip_json_entries = []
        if html_rows and json_by_dt:
            first_date = html_rows[0][0]
            last_date  = html_rows[-1][0]
            zip_json_entries = [
                e for e in all_json_ordered
                if first_date <= e["date"].strftime("%Y-%m-%d") <= last_date
            ]

        # Fallback buckets (positional by date+type)
        for entry in all_json_ordered:
            dt   = entry["date"]
            kind = entry["type"]
            fallback_by_day[(dt.strftime("%Y-%m-%d"), kind)].append(entry)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            for i, uuid in enumerate(uuid_map.keys(), 1):
                info         = uuid_map[uuid]
                main_name    = info["main"]
                overlay_name = info["overlay"]
                file_date    = info["file_date"]

                progress(i, total, total - i)

                if not main_name:
                    skipped += 1
                    if on_file_done:
                        on_file_done(saved=0, overlay=0, gps=0, skipped=1,
                                     year=0, month=0, is_video=False)
                    continue

                ext      = Path(main_name).suffix.lower()
                is_video = ext in {".mp4", ".mov"}
                label    = "video" if is_video else "photo"

                # ── Step 1: Match this UUID to a JSON entry ───────────────
                #
                # PRIMARY (HTML available):
                #   The HTML src= attributes list main files by UUID in order.
                #   html_uuid_to_pos[uuid] gives us the position in this ZIP's HTML.
                #   zip_json_entries[pos] gives us the JSON entry at that position.
                #   This is a direct UUID → JSON match. Guaranteed correct.
                #
                # FALLBACK (no HTML):
                #   Positional match by (date, type) bucket.

                raw_dt     = None
                json_entry = None
                match_src  = "none"
                uuid_upper = uuid.upper()

                if html_uuid_to_pos and uuid_upper in html_uuid_to_pos:
                    pos = html_uuid_to_pos[uuid_upper]
                    if pos < len(zip_json_entries):
                        json_entry = zip_json_entries[pos]
                        raw_dt     = json_entry["date"]
                        match_src  = "html+json"
                    else:
                        match_src = "html-out-of-range"
                elif json_by_dt:
                    json_type  = "video" if is_video else "image"
                    day_key    = (file_date, json_type)
                    pos        = day_counters[day_key]
                    day_counters[day_key] += 1
                    candidates = fallback_by_day.get(day_key, [])
                    if pos < len(candidates):
                        json_entry = candidates[pos]
                        raw_dt     = json_entry["date"]
                        match_src  = "positional"

                # ── Step 2: Resolve datetime ──────────────────────────────
                # Use exact time from JSON if available, else date at midnight
                use_dt = raw_dt if raw_dt is not None else parse_dt(file_date)
                if use_dt is None:
                    use_dt = datetime.utcnow()

                # ── Step 3: Output filename ───────────────────────────────
                dest = safe_dest(
                    out_dir,
                    f"{use_dt.strftime('%Y-%m-%d_%H-%M-%S')}_{label}",
                    ext,
                )

                # ── Step 4: Extract media ─────────────────────────────────
                tmp_main = tmpdir / f"main_{uuid}{ext}"
                tmp_main.write_bytes(src.read(main_name))

                # ── Step 5: Apply overlay ─────────────────────────────────
                did_overlay = False

                if overlay_name and do_overlay:
                    tmp_ov = tmpdir / f"ov_{uuid}.png"
                    tmp_ov.write_bytes(src.read(overlay_name))

                    if is_video:
                        mb = round(tmp_main.stat().st_size / 1_048_576, 1)
                        log(f"  [{i}/{total}]  Encoding {dest.name}  ({mb} MB)...")

                        def _vp(f, tf, pct, spd="", name=dest.name):
                            video_progress(name, f, tf, pct, spd)

                        did_overlay = composite_video(
                            tmp_main, tmp_ov, dest, warn, _vp,
                            kill_flag=kill_flag, hard_timeout=hard_timeout)

                        if not did_overlay:
                            reason = ("Skipped by user"
                                      if (kill_flag and kill_flag.is_set())
                                      else "ffmpeg timed out or errored")
                            if kill_flag: kill_flag.clear()
                            failed.append({
                                "reason":     reason,
                                "dest":       dest.name,
                                "source":     main_name,
                                "overlay":    overlay_name,
                                "json_entry": json_entry,
                            })

                        log(f"  [{i}/{total}]  {dest.name}  — "
                            f"{'overlay applied' if did_overlay else 'saved without overlay'}")

                    elif PILLOW_OK:
                        did_overlay = composite_image(tmp_main, tmp_ov, dest)
                        if not did_overlay:
                            failed.append({
                                "reason":     "Pillow compositing failed",
                                "dest":       dest.name,
                                "source":     main_name,
                                "overlay":    overlay_name,
                                "json_entry": json_entry,
                            })
                        log(f"  [{i}/{total}]  {dest.name}  — "
                            f"photo {'with' if did_overlay else 'without'} overlay")
                    else:
                        shutil.copy2(tmp_main, dest)
                        log(f"  [{i}/{total}]  {dest.name}  — "
                            f"photo saved (install Pillow for overlays)")

                    tmp_ov.unlink(missing_ok=True)
                    if did_overlay:
                        overlays += 1
                else:
                    shutil.copy2(tmp_main, dest)
                    note = " (no filter)" if (do_overlay and not overlay_name) else ""
                    log(f"  [{i}/{total}]  {dest.name}  — {label} saved{note}")

                tmp_main.unlink(missing_ok=True)

                # ── Step 6: File timestamps ───────────────────────────────
                set_file_times(dest, use_dt)
                matched += 1

                # ── Step 7: GPS ───────────────────────────────────────────
                did_gps = False
                if json_entry:
                    lat = json_entry.get("lat")
                    lon = json_entry.get("lon")
                    if lat is not None and not (lat == 0.0 and lon == 0.0):
                        if ext in {".jpg", ".jpeg"}:
                            did_gps = embed_gps_jpeg(dest, lat, lon, use_dt)
                        elif ext in {".mp4", ".mov"}:
                            did_gps = embed_gps_video(dest, lat, lon, use_dt, warn)
                        if did_gps:
                            gps_count += 1

                # Update all stat cards per file
                if on_file_done:
                    on_file_done(saved=1,
                                 overlay=1 if did_overlay else 0,
                                 gps=1 if did_gps else 0,
                                 skipped=0,
                                 year=use_dt.year,
                                 month=use_dt.month,
                                 is_video=is_video)

    return matched, overlays, skipped, gps_count, failed


# ─────────────────────────────────────────────────────────────────────────────
# GUI
# ─────────────────────────────────────────────────────────────────────────────

BG      = "#0d0d0d"
BG2     = "#141414"
CARD    = "#1c1c1c"
CARD2   = "#242424"
BORDER  = "#333333"
SCROLL  = "#555555"
ACCENT  = "#FFFC00"
ACCENT2 = "#e8e000"
FG      = "#f0f0f0"
FG2     = "#aaaaaa"
FG3     = "#666666"
OK      = "#3fb950"
WARN_C  = "#d29922"
ERR_C   = "#f85149"
INFO_C  = "#58a6ff"
BLUE    = "#1f6feb"

FONT   = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")
FONT_S = ("Segoe UI", 9)
FONT_L = ("Segoe UI", 13, "bold")
MONO   = ("Consolas", 9)


def _card(parent, **kw):
    return tk.Frame(parent, bg=CARD,
                    highlightbackground=BORDER, highlightthickness=1, **kw)


def _sec(parent, text):
    tk.Label(parent, text=text, font=FONT_B, bg=BG2, fg=FG2,
             ).pack(anchor="w", padx=20, pady=(16, 4))


def _scrollable(parent):
    """Scrollable inner frame with clearly visible scrollbar."""
    canvas = tk.Canvas(parent, bg=BG2, highlightthickness=0)
    sb     = tk.Scrollbar(parent, orient="vertical", command=canvas.yview,
                           bg=SCROLL, troughcolor=BG2,
                           activebackground=FG3, relief="flat", width=14)
    canvas.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    inner = tk.Frame(canvas, bg=BG2)
    win   = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))
    inner.bind("<Configure>",
               lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind_all("<MouseWheel>",
                    lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))
    return inner


def _dep_row(parent, name, ok, ver, install_cmd, desc):
    row = tk.Frame(parent, bg=CARD)
    row.pack(fill="x", padx=14, pady=5)
    pill_bg = "#162916" if ok else "#2d1616"
    pill_fg = OK if ok else ERR_C
    pill    = tk.Frame(row, bg=pill_bg, padx=10, pady=4)
    pill.pack(side="left")
    tk.Label(pill, text="Installed" if ok else "Not installed",
             font=FONT_S, bg=pill_bg, fg=pill_fg).pack()
    info = tk.Frame(row, bg=CARD)
    info.pack(side="left", padx=(12, 0), fill="x", expand=True)
    nr = tk.Frame(info, bg=CARD)
    nr.pack(fill="x")
    tk.Label(nr, text=name, font=FONT_B, bg=CARD, fg=FG).pack(side="left")
    if ver:
        tk.Label(nr, text=f"  {ver}", font=FONT_S, bg=CARD, fg=FG3).pack(side="left")
    tk.Label(info, text=desc, font=FONT_S, bg=CARD, fg=FG2,
             anchor="w").pack(fill="x")
    if not ok:
        tk.Label(info, text=f"  Run:  {install_cmd}",
                 font=MONO, bg=CARD, fg=WARN_C, anchor="w").pack(fill="x", pady=(2, 0))


def _add_btn(parent, label, sublabel, cmd, accent=False):
    bg  = BLUE if accent else CARD2
    fg  = "#ffffff" if accent else FG
    fg2 = "#aaccff" if accent else FG3
    f   = tk.Frame(parent, bg=bg, cursor="hand2",
                   highlightbackground=BORDER if not accent else BLUE,
                   highlightthickness=1)
    tk.Label(f, text=label,    font=FONT_B, bg=bg, fg=fg,
             ).pack(padx=16, pady=(12, 2), anchor="w")
    tk.Label(f, text=sublabel, font=FONT_S, bg=bg, fg=fg2,
             ).pack(padx=16, pady=(0, 12), anchor="w")
    f.bind("<Button-1>", lambda _: cmd())
    for child in f.winfo_children():
        child.bind("<Button-1>", lambda _: cmd())
    return f


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME}  {APP_VERSION}")
        self.configure(bg=BG)
        self.minsize(960, 700)
        self.resizable(True, True)

        self._queue       = []
        self._out         = tk.StringVar(
            value=str(Path.home() / "Desktop" / "snapchat_organised"))
        self._do_overlay  = tk.BooleanVar(value=True)
        self._timeout_var = tk.StringVar(value="10")
        self._running     = False
        self._warn_count  = 0
        self._kill_flag   = threading.Event()
        self._grand_total = 0
        self._grand_done  = 0
        self._start_time  = None

        self._build_ui()
        self._fit_window(min_w=960, min_h=720)

    def _fit_window(self, min_w, min_h, max_w=None, max_h=None):
        """
        Size the window to fit its content, then centre on screen.
        Works on Mac and Windows at any DPI or font scale.
        """
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        cap_w = max_w or int(sw * 0.92)
        cap_h = max_h or int(sh * 0.90)
        w = max(min_w, min(self.winfo_reqwidth(),  cap_w))
        h = max(min_h, min(self.winfo_reqheight(), cap_h))
        x = (sw - w) // 2
        y = max(30, (sh - h) // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg="#111111", pady=12)
        hdr.pack(fill="x")
        hi  = tk.Frame(hdr, bg="#111111")
        hi.pack(fill="x", padx=24)
        tk.Label(hi, text=APP_NAME, font=FONT_L,
                 bg="#111111", fg=FG).pack(side="left")
        tk.Label(hi, text=f"  {APP_VERSION}",
                 font=FONT_S, bg="#111111", fg=FG3).pack(side="left", pady=(4, 0))

        all_ok = all([PILLOW_OK, PIEXIF_OK, FFMPEG_OK, FFPROBE_OK])
        dot_c  = OK if all_ok else WARN_C
        dot_t  = ("All dependencies ready"
                  if all_ok else "Some dependencies missing — check Setup")
        tk.Label(hi, text=f"●  {dot_t}",
                 font=FONT_S, bg="#111111", fg=dot_c).pack(side="right")

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Main.TNotebook", background=BG, borderwidth=0,
                        tabmargins=[0, 0, 0, 0])
        style.configure("Main.TNotebook.Tab",
                        background="#1a1a1a", foreground=FG3,
                        padding=[22, 10], font=FONT_S, borderwidth=0)
        style.map("Main.TNotebook.Tab",
                  background=[("selected", BG2)],
                  foreground=[("selected", FG)])
        style.configure("Sub.TNotebook", background=BG2, borderwidth=0)
        style.configure("Sub.TNotebook.Tab",
                        background=CARD, foreground=FG3,
                        padding=[14, 6], font=FONT_S, borderwidth=0)
        style.map("Sub.TNotebook.Tab",
                  background=[("selected", CARD2)],
                  foreground=[("selected", FG)])
        for s, c in [("Y.Horizontal.TProgressbar", ACCENT),
                     ("B.Horizontal.TProgressbar", INFO_C)]:
            style.configure(s, troughcolor=CARD2, background=c,
                            bordercolor=BORDER, lightcolor=c,
                            darkcolor=c, thickness=14)

        self._nb = ttk.Notebook(self, style="Main.TNotebook")
        self._nb.pack(fill="both", expand=True)

        for title, attr in [("  Setup  ",             "_tab_setup"),
                             ("  Files  ",             "_tab_files"),
                             ("  Progress  ",          "_tab_progress"),
                             ("  Log  ",               "_tab_log"),
                             ("  Memories Summary  ",  "_tab_summary")]:
            f = tk.Frame(self._nb, bg=BG2)
            setattr(self, attr, f)
            self._nb.add(f, text=title)

        self._build_setup()
        self._build_files()
        self._build_progress()
        self._build_log()
        self._build_summary()
        self._build_footer()

    # ── Setup tab ─────────────────────────────────────────────────────────────

    def _build_setup(self):
        inner = _scrollable(self._tab_setup)

        _sec(inner, "About this app")
        about = _card(inner)
        about.pack(fill="x", padx=20, pady=(0, 6))
        tk.Label(about,
                 text=(
                     "Snapchat Memories Organiser helps you take back control of your Snapchat memories.\n\n"
                     "When Snapchat exports your data, photos and videos are stripped of overlays, stickers,\n"
                     "and metadata. Files appear as if created today when imported into Apple Photos or\n"
                     "Google Photos — not when you actually took them.\n\n"
                     "This app fixes that by:\n"
                     "  •  Merging Snapchat filter overlays back onto your photos and videos\n"
                     "  •  Restoring the original creation date and time from the master JSON\n"
                     "  •  Embedding GPS coordinates where available\n"
                     "  •  Producing clean, organised files ready to import anywhere\n\n"
                     "How matching works:\n"
                     "  1.  All ZIPs are pre-scanned to find memories_history.json (in ZIP 1).\n"
                     "       This master file contains exact UTC times and GPS for every memory.\n"
                     "  2.  Each ZIP's memories_history.html is parsed to get its media files\n"
                     "       listed by UUID filename in document order.\n"
                     "  3.  Each UUID is cross-referenced against the master JSON by position —\n"
                     "       giving a guaranteed correct time and GPS match for every single file,\n"
                     "       even on days where many memories were taken.\n"
                     "  4.  If no HTML is found in a ZIP, positional matching by date and type\n"
                     "       is used as a fallback.\n\n"
                     "Note on timestamps:\n"
                     "  •  Dates will be accurate for all years.\n"
                     "  •  Times (UTC) may be inconsistent for some memories — Snapchat has\n"
                     "     changed how it stored timezones over time, and early exports did not\n"
                     "     always record times accurately."
                 ),
                 font=FONT_S, bg=CARD, fg=FG2,
                 justify="left", anchor="w", wraplength=860,
                 ).pack(padx=16, pady=14, anchor="w")

        _sec(inner, "How to use this app")
        sc = _card(inner)
        sc.pack(fill="x", padx=20, pady=(0, 6))
        steps = [
            ("1  Request your Snapchat data",
             "Go to accounts.snapchat.com → My Data → Export your Memories.\n"
             "Tick 'Export JSON Files for data portability purposes' then submit.\n"
             "Snapchat emails you a link — download every ZIP file provided."),
            ("2  Put all ZIPs in one folder",
             "Create a folder (e.g. Snapchat ZIPs on your Desktop).\n"
             "Move all ZIPs into it. Do not extract them — keep them as ZIPs."),
            ("3  Check all dependencies",
             "See the Dependencies section below. All four must show Installed.\n"
             f"Open {'Terminal' if _IS_MAC else 'Command Prompt'} and run the "
             f"install commands shown for anything missing, then restart this app."),
            ("4  Go to the Files tab",
             "Click 'Add a folder of ZIPs', select your ZIPs folder.\n"
             "Choose an output folder, set your timeout preference, then click Process."),
            ("5  Monitor progress",
             "Progress tab shows live stats, progress bars, and estimated time remaining.\n"
             "Log tab shows full activity detail and any warnings.\n"
             "Use the Skip button to jump past any video that is taking too long."),
            ("6  Import your organised memories",
             "Your output folder contains properly restored media with original timestamps\n"
             "and GPS. Import into Apple Photos, Google Photos, Android, NAS, or anywhere.\n"
             "Dates are accurate for all years. Times may vary for older memories."),
        ]
        for title, detail in steps:
            row = tk.Frame(sc, bg=CARD)
            row.pack(fill="x", padx=16, pady=6)
            tk.Label(row, text=title, font=FONT_B, bg=CARD, fg=FG,
                     anchor="w").pack(fill="x")
            tk.Label(row, text=detail, font=FONT_S, bg=CARD, fg=FG2,
                     justify="left", anchor="w", wraplength=860,
                     ).pack(fill="x", pady=(2, 0))
        tk.Frame(sc, bg=CARD, height=8).pack()

        _sec(inner, "Dependencies")
        dc = _card(inner)
        dc.pack(fill="x", padx=20, pady=(0, 6))
        for args in [
            ("Pillow",  PILLOW_OK,  PILLOW_VER,  _INST_PILLOW,
             "Required — applies Snapchat filter overlays onto photos."),
            ("piexif",  PIEXIF_OK,  None,         _INST_PIEXIF,
             "Required — embeds GPS and timestamps into JPEG photos."),
            ("ffmpeg",  FFMPEG_OK,  FFMPEG_VER,   _INST_FFMPEG,
             "Required — applies overlays onto videos and embeds GPS into MP4 files."),
            ("ffprobe", FFPROBE_OK, FFPROBE_VER,  _INST_FFPROBE,
             "Required — reads video dimensions and frame counts."),
        ]:
            _dep_row(dc, *args)
        tk.Frame(dc, bg=CARD, height=8).pack()

        tk.Frame(inner, bg=BG2, height=12).pack()
        nb = tk.Frame(inner, bg=BG2)
        nb.pack(fill="x", padx=20, pady=(0, 24))
        tk.Button(nb, text="Next: Add your files  →",
                  font=("Segoe UI", 11, "bold"),
                  bg=ACCENT, fg="#000", relief="flat",
                  padx=28, pady=12, cursor="hand2",
                  activebackground=ACCENT2, activeforeground="#000",
                  command=lambda: self._nb.select(1)).pack(side="left")

    # ── Files tab ─────────────────────────────────────────────────────────────

    def _build_files(self):
        inner = _scrollable(self._tab_files)

        _sec(inner, "Add your Snapchat ZIP files")
        fc = _card(inner)
        fc.pack(fill="x", padx=20, pady=(0, 6))
        tk.Label(fc,
                 text="  Choose one of the options below. You can select multiple ZIPs at once.",
                 font=FONT_S, bg=CARD, fg=FG2).pack(anchor="w", pady=(12, 8))

        btns = tk.Frame(fc, bg=CARD)
        btns.pack(fill="x", padx=14, pady=(0, 10))
        _add_btn(btns, "Add a folder of ZIPs",
                 "Recommended — finds all ZIPs in the folder automatically",
                 self._add_zip_folder, accent=True).pack(side="left", padx=(0, 10))
        _add_btn(btns, "Add individual ZIPs",
                 "Pick one or more specific ZIP files",
                 self._add_zips).pack(side="left", padx=(0, 10))
        _add_btn(btns, "Add an unzipped folder",
                 "If you have already extracted a ZIP",
                 self._add_folder).pack(side="left")

        qhdr = tk.Frame(fc, bg=CARD)
        qhdr.pack(fill="x", padx=14, pady=(8, 2))
        self._queue_lbl = tk.Label(qhdr, text="No files queued yet",
                                    font=FONT_S, bg=CARD, fg=FG3)
        self._queue_lbl.pack(side="left")
        tk.Button(qhdr, text="Clear all", font=FONT_S, bg=CARD2, fg=FG2,
                  relief="flat", padx=10, pady=3, cursor="hand2",
                  activebackground=BORDER, activeforeground=FG,
                  command=self._clear_queue).pack(side="right")

        self._queue_box = tk.Listbox(
            fc, bg="#0d0d0d", fg=FG2, font=MONO,
            selectbackground=CARD2, activestyle="none",
            height=6, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, bd=0, selectforeground=FG)
        self._queue_box.pack(fill="x", padx=14, pady=(4, 14))

        _sec(inner, "Output folder")
        oc = _card(inner)
        oc.pack(fill="x", padx=20, pady=(0, 6))
        oi = tk.Frame(oc, bg=CARD, pady=12)
        oi.pack(fill="x", padx=14)
        tk.Label(oi, text="Where do you want to save your organised memories?",
                 font=FONT_S, bg=CARD, fg=FG2).pack(anchor="w", pady=(0, 6))
        pr = tk.Frame(oi, bg=CARD)
        pr.pack(fill="x")
        tk.Entry(pr, textvariable=self._out, bg="#111111", fg=FG,
                 insertbackground=FG, relief="flat", font=FONT,
                 highlightbackground=BORDER, highlightthickness=1,
                 highlightcolor=INFO_C,
                 ).pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 10))
        tk.Button(pr, text="Browse...", font=FONT_S, bg=CARD2, fg=FG,
                  relief="flat", padx=14, pady=6, cursor="hand2",
                  activebackground=BORDER, activeforeground=FG,
                  command=self._browse_out).pack(side="left")
        tk.Label(oi, text="Tip: choose a new empty folder. A folder on your Desktop works well.",
                 font=FONT_S, bg=CARD, fg=FG3).pack(anchor="w", pady=(6, 0))

        _sec(inner, "Options")
        opt = _card(inner)
        opt.pack(fill="x", padx=20, pady=(0, 6))
        oi2 = tk.Frame(opt, bg=CARD, pady=12)
        oi2.pack(fill="x", padx=14)

        tk.Checkbutton(oi2, variable=self._do_overlay,
                       text="Apply Snapchat filter overlays onto photos and videos",
                       bg=CARD, fg=FG, selectcolor=BG2,
                       activebackground=CARD, activeforeground=FG,
                       font=FONT, cursor="hand2", highlightthickness=0,
                       ).pack(anchor="w")
        tk.Label(oi2,
                 text="Uses Pillow for photos and ffmpeg for videos. "
                      "Disable to skip overlay merging and run faster.",
                 font=FONT_S, bg=CARD, fg=FG3).pack(anchor="w", pady=(2, 10))

        tk.Frame(oi2, bg=BORDER, height=1).pack(fill="x", pady=(0, 10))

        tr = tk.Frame(oi2, bg=CARD)
        tr.pack(fill="x", pady=(0, 6))
        tk.Label(tr, text="ffmpeg hard timeout per video:",
                 font=FONT_S, bg=CARD, fg=FG2).pack(side="left")
        tk.Entry(tr, textvariable=self._timeout_var, bg="#111111", fg=FG,
                 insertbackground=FG, relief="flat", font=FONT,
                 highlightbackground=BORDER, highlightthickness=1,
                 width=5).pack(side="left", padx=(10, 6), ipady=4)
        tk.Label(tr, text="minutes   (set to 0 for no limit)",
                 font=FONT_S, bg=CARD, fg=FG3).pack(side="left")
        tk.Label(oi2,
                 text="If a video takes longer than this to encode, the process is killed and\n"
                      "the original file is saved instead. Use the Skip button in the Progress\n"
                      "tab to manually skip a specific video during processing.",
                 font=FONT_S, bg=CARD, fg=FG3, justify="left").pack(anchor="w")

        tk.Frame(inner, bg=BG2, height=12).pack()
        bo = tk.Frame(inner, bg=BG2)
        bo.pack(fill="x", padx=20, pady=(0, 24))
        self._start_btn = tk.Button(
            bo, text="Process All ZIPs  →",
            font=("Segoe UI", 12, "bold"),
            bg=ACCENT, fg="#000", relief="flat",
            padx=32, pady=14, cursor="hand2",
            activebackground=ACCENT2, activeforeground="#000",
            command=self._start)
        self._start_btn.pack(side="left")
        self._start_note = tk.Label(bo, text="",
                                     font=FONT_S, bg=BG2, fg=FG3)
        self._start_note.pack(side="left", padx=16)

    # ── Progress tab ──────────────────────────────────────────────────────────

    def _build_progress(self):
        p = self._tab_progress

        # Make the progress tab scrollable so the breakdown table fits
        outer = tk.Frame(p, bg=BG2)
        outer.pack(fill="both", expand=True)
        p = _scrollable(outer)   # inner scrollable frame

        _sec(p, "Live statistics")
        cr = tk.Frame(p, bg=BG2)
        cr.pack(fill="x", padx=20, pady=(0, 12))
        self._stat_vars = {}
        for key, lbl, tip in [
            ("zip",      "ZIP",      "Current ZIP"),
            ("saved",    "Saved",    "Files saved"),
            ("overlays", "Overlays", "Overlays applied"),
            ("gps",      "GPS",      "GPS embedded"),
            ("skipped",  "Skipped",  "Files skipped"),
        ]:
            c = tk.Frame(cr, bg=CARD,
                         highlightbackground=BORDER, highlightthickness=1)
            c.pack(side="left", fill="both", expand=True, padx=(0, 6))
            var = tk.StringVar(value="—")
            self._stat_vars[key] = var
            tk.Label(c, textvariable=var,
                     font=("Segoe UI", 18, "bold"), bg=CARD, fg=FG
                     ).pack(pady=(14, 2))
            tk.Label(c, text=lbl,  font=FONT_S,           bg=CARD, fg=FG3).pack()
            tk.Label(c, text=tip,  font=("Segoe UI", 8),
                     bg=CARD, fg=FG3, wraplength=110).pack(pady=(0, 12))

        _sec(p, "Current file")
        cc = _card(p)
        cc.pack(fill="x", padx=20, pady=(0, 10))
        ci = tk.Frame(cc, bg=CARD, pady=12)
        ci.pack(fill="x", padx=16)
        self._cur_file = tk.StringVar(value="Waiting to start...")
        self._cur_note = tk.StringVar(value="")
        tk.Label(ci, textvariable=self._cur_file,
                 font=FONT_B, bg=CARD, fg=FG, anchor="w").pack(fill="x")
        tk.Label(ci, textvariable=self._cur_note,
                 font=FONT_S, bg=CARD, fg=FG2, anchor="w"
                 ).pack(fill="x", pady=(2, 10))

        skip_row = tk.Frame(ci, bg=CARD)
        skip_row.pack(fill="x")
        self._skip_btn = tk.Button(
            skip_row, text="Skip this video",
            font=FONT_S, bg="#2d1616", fg=ERR_C,
            relief="flat", padx=14, pady=6, cursor="hand2",
            activebackground="#3d2020", activeforeground=ERR_C,
            state="disabled", command=self._skip_current)
        self._skip_btn.pack(side="left")
        tk.Label(skip_row,
                 text="  Kills the current ffmpeg job — original file saved in its place.",
                 font=FONT_S, bg=CARD, fg=FG3).pack(side="left")

        _sec(p, "Overall progress")
        pc = _card(p)
        pc.pack(fill="x", padx=20, pady=(0, 10))
        pi = tk.Frame(pc, bg=CARD, pady=16)
        pi.pack(fill="x", padx=16)

        for lbl, bar_a, lbl_a, sty in [
            ("Total files",    "_grand_bar", "_grand_lbl", "Y.Horizontal.TProgressbar"),
            ("ZIPs",           "_zip_bar",   "_zip_lbl",   "Y.Horizontal.TProgressbar"),
            ("Video encoding", "_vbar",      "_vpct",      "B.Horizontal.TProgressbar"),
        ]:
            row = tk.Frame(pi, bg=CARD)
            row.pack(fill="x", pady=(0, 10))
            tk.Label(row, text=lbl, font=FONT_S, bg=CARD, fg=FG2,
                     width=14, anchor="w").pack(side="left")
            bar = ttk.Progressbar(row, style=sty, mode="determinate")
            bar.pack(side="left", fill="x", expand=True, padx=(0, 12))
            lw = tk.Label(row, text="", font=FONT_S, bg=CARD, fg=FG2,
                          width=30, anchor="w")
            lw.pack(side="left")
            setattr(self, bar_a, bar)
            setattr(self, lbl_a, lw)

        _sec(p, "Timing")
        tc = _card(p)
        tc.pack(fill="x", padx=20, pady=(0, 10))
        ti = tk.Frame(tc, bg=CARD, pady=12)
        ti.pack(fill="x", padx=16)
        self._elapsed_var = tk.StringVar(value="Not started")
        self._rate_var    = tk.StringVar(value="")
        self._eta_var     = tk.StringVar(value="")
        tk.Label(ti, textvariable=self._elapsed_var,
                 font=FONT_S, bg=CARD, fg=FG2).pack(anchor="w")
        tk.Label(ti, textvariable=self._rate_var,
                 font=FONT_S, bg=CARD, fg=FG3).pack(anchor="w", pady=(3, 0))
        tk.Label(ti, textvariable=self._eta_var,
                 font=FONT_S, bg=CARD, fg=FG3).pack(anchor="w")

        # ── Year / month breakdown ────────────────────────────────────────
        _sec(p, "Memories by year and month")
        bc = _card(p)
        bc.pack(fill="x", padx=20, pady=(0, 10))
        bi = tk.Frame(bc, bg=CARD, pady=12)
        bi.pack(fill="x", padx=16)

        # Header row: label + year selector
        yr_hdr = tk.Frame(bi, bg=CARD)
        yr_hdr.pack(fill="x", pady=(0, 10))
        tk.Label(yr_hdr,
                 text="Select a year to see monthly breakdown:",
                 font=FONT_S, bg=CARD, fg=FG2).pack(side="left")

        self._year_var    = tk.StringVar(value="—")
        self._year_menu   = ttk.OptionMenu(yr_hdr, self._year_var, "—",
                                            command=lambda _: self._update_breakdown())
        self._year_menu.config(width=8)
        self._year_menu.pack(side="left", padx=(10, 16))

        self._year_total_lbl = tk.Label(yr_hdr, text="", font=FONT_B,
                                         bg=CARD, fg=ACCENT)
        self._year_total_lbl.pack(side="left")

        # Month table — 12 rows, 3 columns: Month | Count | Bar
        MONTHS = ["January","February","March","April","May","June",
                  "July","August","September","October","November","December"]
        self._month_rows = []
        tbl = tk.Frame(bi, bg=CARD)
        tbl.pack(fill="x")

        # Column headers
        for col, (txt, w) in enumerate([("Month", 12), ("Snaps", 8), ("", 0)]):
            tk.Label(tbl, text=txt, font=FONT_B, bg=CARD, fg=FG3,
                     width=w, anchor="w").grid(
                row=0, column=col, sticky="w", padx=(0, 12), pady=(0, 4))

        for mi, month in enumerate(MONTHS, 1):
            cnt_var = tk.StringVar(value="")
            bar_var = tk.IntVar(value=0)

            tk.Label(tbl, text=month, font=FONT_S, bg=CARD, fg=FG2,
                     width=12, anchor="w").grid(
                row=mi, column=0, sticky="w", padx=(0, 12), pady=2)

            tk.Label(tbl, textvariable=cnt_var, font=FONT_S, bg=CARD, fg=FG,
                     width=8, anchor="w").grid(
                row=mi, column=1, sticky="w", padx=(0, 12), pady=2)

            bar = ttk.Progressbar(tbl, style="Y.Horizontal.TProgressbar",
                                   mode="determinate", length=280,
                                   variable=bar_var)
            bar.grid(row=mi, column=2, sticky="w", pady=2)

            self._month_rows.append((cnt_var, bar_var))

        # Storage for breakdown data: {year: {month_int: count}}
        self._breakdown_data = {}

        # Note
        note = tk.Frame(p, bg=BG2)
        note.pack(fill="x", padx=20, pady=(4, 12))
        tk.Label(note,
                 text="Please note: Dates will be accurate for all years. Times (UTC) may be inconsistent "
                      "for some memories — Snapchat has changed how it stored timezones over time and early "
                      "exports did not always record times accurately.",
                 font=("Segoe UI", 8), bg=BG2, fg=FG3,
                 justify="left", anchor="w", wraplength=900,
                 ).pack(anchor="w")

    # ── Summary tab ───────────────────────────────────────────────────────────

    def _build_summary(self):
        p = self._tab_summary

        # Header bar
        hdr = tk.Frame(p, bg=CARD, pady=10)
        hdr.pack(fill="x")
        hdr_i = tk.Frame(hdr, bg=CARD)
        hdr_i.pack(fill="x", padx=20)
        tk.Label(hdr_i, text="Memories by Year & Month",
                 font=FONT_B, bg=CARD, fg=FG).pack(side="left")
        tk.Label(hdr_i,
                 text="Updates live as memories are processed.",
                 font=FONT_S, bg=CARD, fg=FG3).pack(side="left", padx=(12, 0))
        tk.Frame(p, bg=BORDER, height=1).pack(fill="x")

        # Scrollable content area
        self._summary_inner = _scrollable(p)

        # Placeholder shown before processing starts
        self._summary_placeholder = tk.Label(
            self._summary_inner,
            text="No memories processed yet.\nStart processing to see your memories summary here.",
            font=FONT_S, bg=BG2, fg=FG3, justify="center")
        self._summary_placeholder.pack(pady=60)

        # We store year frames here for incremental updates
        self._summary_year_frames = {}   # year -> (frame, month_widgets dict)

    def _refresh_summary(self):
        """Rebuild just the changed parts of the summary. Called per file."""
        # Remove placeholder if present
        if self._summary_placeholder and self._summary_placeholder.winfo_exists():
            try:
                self._summary_placeholder.destroy()
                self._summary_placeholder = None
            except Exception:
                pass

        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

        for year in sorted(self._summary.keys()):
            year_data = self._summary[year]

            if year not in self._summary_year_frames:
                # Create year section
                yr_outer = tk.Frame(self._summary_inner, bg=BG2)
                yr_outer.pack(fill="x", padx=20, pady=(12, 0))

                # Year header row
                yr_hdr = tk.Frame(yr_outer, bg=BG2)
                yr_hdr.pack(fill="x", pady=(0, 6))

                yr_photos = sum(m["photos"] for m in year_data.values())
                yr_videos = sum(m["videos"] for m in year_data.values())
                yr_total  = yr_photos + yr_videos
                yr_gps    = sum(m["gps"]    for m in year_data.values())

                yr_lbl = tk.Label(yr_hdr,
                                  text=f"{year}",
                                  font=("Segoe UI", 13, "bold"),
                                  bg=BG2, fg=ACCENT)
                yr_lbl.pack(side="left")

                yr_summary_lbl = tk.Label(yr_hdr,
                                          text=f"  {yr_total} memories",
                                          font=FONT_S, bg=BG2, fg=FG3)
                yr_summary_lbl.pack(side="left", pady=(3, 0))

                # Column headers
                col_hdr = tk.Frame(yr_outer, bg=CARD,
                                   highlightbackground=BORDER, highlightthickness=1)
                col_hdr.pack(fill="x")
                for txt, w, anchor in [
                    ("Month",   10, "w"),
                    ("Photos",  8,  "center"),
                    ("Videos",  8,  "center"),
                    ("Total",   8,  "center"),
                    ("GPS",     8,  "center"),
                ]:
                    tk.Label(col_hdr, text=txt, font=FONT_S, bg=CARD2,
                             fg=FG3, width=w, anchor=anchor,
                             pady=5).pack(side="left", padx=(8, 0))

                # Month rows container
                rows_frame = tk.Frame(yr_outer, bg=CARD,
                                      highlightbackground=BORDER,
                                      highlightthickness=1)
                rows_frame.pack(fill="x")

                # Year total row at bottom
                total_frame = tk.Frame(yr_outer, bg=CARD2,
                                       highlightbackground=BORDER,
                                       highlightthickness=1)
                total_frame.pack(fill="x", pady=(0, 4))

                self._summary_year_frames[year] = {
                    "yr_lbl":        yr_lbl,
                    "yr_summary":    yr_summary_lbl,
                    "rows_frame":    rows_frame,
                    "total_frame":   total_frame,
                    "month_rows":    {},   # month_num -> label widgets
                    "total_widgets": None,
                }

            yf = self._summary_year_frames[year]
            months_data = year_data

            # Update or create each month row
            for month_num in sorted(months_data.keys()):
                m_data  = months_data[month_num]
                photos  = m_data["photos"]
                videos  = m_data["videos"]
                total   = photos + videos
                gps     = m_data["gps"]
                m_name  = months[month_num - 1]
                row_bg  = CARD if month_num % 2 == 0 else "#202020"

                if month_num not in yf["month_rows"]:
                    # Create new month row
                    row = tk.Frame(yf["rows_frame"], bg=row_bg)
                    row.pack(fill="x")
                    widgets = {}
                    for key, txt, w, anchor, fg_col in [
                        ("month",  m_name,         10, "w",      FG2),
                        ("photos", str(photos),     8,  "center", FG2),
                        ("videos", str(videos),     8,  "center", FG2),
                        ("total",  str(total),      8,  "center", FG),
                        ("gps",    str(gps),        8,  "center", OK if gps else FG3),
                    ]:
                        lbl = tk.Label(row, text=txt, font=FONT_S, bg=row_bg,
                                       fg=fg_col, width=w, anchor=anchor, pady=4)
                        lbl.pack(side="left", padx=(8, 0))
                        widgets[key] = lbl
                    yf["month_rows"][month_num] = widgets
                else:
                    # Update existing month row
                    w = yf["month_rows"][month_num]
                    w["photos"].config(text=str(photos))
                    w["videos"].config(text=str(videos))
                    w["total"].config(text=str(total))
                    w["gps"].config(text=str(gps),
                                    fg=OK if gps else FG3)

            # Update year header totals
            yr_photos = sum(m["photos"] for m in months_data.values())
            yr_videos = sum(m["videos"] for m in months_data.values())
            yr_total  = yr_photos + yr_videos
            yr_gps    = sum(m["gps"]    for m in months_data.values())
            yf["yr_summary"].config(
                text=f"  {yr_total:,} memories  ·  {yr_photos:,} photos  "
                     f"·  {yr_videos:,} videos  ·  {yr_gps:,} GPS")

            # Update or create year total row
            tf = yf["total_frame"]
            if yf["total_widgets"] is None:
                tw = {}
                for key, txt, w, anchor, fg_col in [
                    ("month",  "Total",           10, "w",      FG2),
                    ("photos", str(yr_photos),    8,  "center", FG2),
                    ("videos", str(yr_videos),    8,  "center", FG2),
                    ("total",  str(yr_total),     8,  "center", ACCENT),
                    ("gps",    str(yr_gps),       8,  "center", OK if yr_gps else FG3),
                ]:
                    lbl = tk.Label(tf, text=txt, font=FONT_B, bg=CARD2,
                                   fg=fg_col, width=w, anchor=anchor, pady=5)
                    lbl.pack(side="left", padx=(8, 0))
                    tw[key] = lbl
                yf["total_widgets"] = tw
            else:
                tw = yf["total_widgets"]
                tw["photos"].config(text=str(yr_photos))
                tw["videos"].config(text=str(yr_videos))
                tw["total"].config(text=str(yr_total))
                tw["gps"].config(text=str(yr_gps),
                                 fg=OK if yr_gps else FG3)

    # ── Log tab ───────────────────────────────────────────────────────────────

    def _build_log(self):
        self._log_nb = ttk.Notebook(self._tab_log, style="Sub.TNotebook")
        self._log_nb.pack(fill="both", expand=True)

        for attr, title in [("_log_box",  "  Activity Log  "),
                             ("_warn_box", "  Warnings & Errors  ")]:
            frame = tk.Frame(self._log_nb, bg=BG2)
            self._log_nb.add(frame, text=title)
            toolbar = tk.Frame(frame, bg=CARD, pady=8)
            toolbar.pack(fill="x")
            tk.Label(toolbar, text="  Scroll down for the latest entries",
                     font=FONT_S, bg=CARD, fg=FG3).pack(side="left", padx=10)
            tk.Button(toolbar, text="Clear", font=FONT_S, bg=CARD2, fg=FG2,
                      relief="flat", padx=12, pady=3, cursor="hand2",
                      activebackground=BORDER, activeforeground=FG,
                      command=lambda f=frame: self._clear_log_tab(f)
                      ).pack(side="right", padx=10)
            box = scrolledtext.ScrolledText(
                frame, bg="#0d1117", fg="#c9d1d9", font=MONO,
                relief="flat", bd=0, wrap="word", state="disabled",
                insertbackground=FG, selectbackground=CARD2)
            box.pack(fill="both", expand=True)
            setattr(self, attr, box)

        self._log_box.tag_config("ok",   foreground=OK)
        self._log_box.tag_config("info", foreground=INFO_C)
        self._log_box.tag_config("dim",  foreground=FG3)
        self._log_box.tag_config("flag", foreground=WARN_C)
        self._warn_box.tag_config("warn", foreground=WARN_C)
        self._warn_box.tag_config("err",  foreground=ERR_C)

    def _clear_log_tab(self, frame):
        for w in frame.winfo_children():
            if isinstance(w, scrolledtext.ScrolledText):
                w.config(state="normal")
                w.delete("1.0", "end")
                w.config(state="disabled")
        self._log_nb.tab(1, text="  Warnings & Errors  ")
        self._warn_count = 0

    # ── Footer ────────────────────────────────────────────────────────────────

    def _build_footer(self):
        footer = tk.Frame(self, bg="#0a0a0a", pady=0)
        footer.pack(fill="x", side="bottom")
        tk.Frame(footer, bg=BORDER, height=1).pack(fill="x")
        fi = tk.Frame(footer, bg="#0a0a0a")
        fi.pack(fill="x", padx=20, pady=6)
        tk.Label(fi,
                 text=f"Made by {APP_AUTHOR}   |   {APP_GITHUB}   |   {APP_DATE}",
                 font=("Segoe UI", 8), bg="#0a0a0a", fg=FG3).pack(side="left")
        tk.Label(fi,
                 text="Dates accurate for all years  •  Times may vary for older memories.",
                 font=("Segoe UI", 8), bg="#0a0a0a", fg=FG3).pack(side="right")

    # ── Queue ─────────────────────────────────────────────────────────────────

    def _add_zip_folder(self):
        p = filedialog.askdirectory(
            title="Select the folder containing your Snapchat ZIPs")
        if not p:
            return
        zips  = sorted(Path(p).glob("*.zip"))
        added = 0
        for z in zips:
            if z not in self._queue:
                self._queue.append(z)
                self._queue_box.insert("end", f"  {z.name}")
                added += 1
        self._upd_qlabel()
        if added:
            self._log(f"Added {added} ZIP file(s) from: {Path(p).name}", "info")
        else:
            self._warn("No ZIP files found in that folder.")

    def _add_zips(self):
        paths = filedialog.askopenfilenames(
            title="Select Snapchat ZIP files — hold Ctrl or Shift for multiple",
            filetypes=[("ZIP files", "*.zip"), ("All files", "*.*")])
        for p in paths:
            path = Path(p)
            if path not in self._queue:
                self._queue.append(path)
                self._queue_box.insert("end", f"  {path.name}")
        self._upd_qlabel()

    def _add_folder(self):
        p = filedialog.askdirectory(title="Select an unzipped Snapchat folder")
        if p:
            path = Path(p)
            if path not in self._queue:
                self._queue.append(path)
                self._queue_box.insert("end", f"  {path.name}  [folder]")
            self._upd_qlabel()

    def _upd_qlabel(self):
        n = len(self._queue)
        self._queue_lbl.config(
            text=(f"{n} source{'s' if n != 1 else ''} queued — ready to process"
                  if n else "No files queued yet"))

    def _clear_queue(self):
        self._queue.clear()
        self._queue_box.delete(0, "end")
        self._queue_lbl.config(text="No files queued yet")

    def _browse_out(self):
        p = filedialog.askdirectory(title="Select output folder")
        if p: self._out.set(p)

    # ── Logging ───────────────────────────────────────────────────────────────

    def _write(self, box, msg, tag=None):
        box.config(state="normal")
        box.insert("end", msg + "\n", tag or "")
        box.see("end")
        box.config(state="disabled")
        self.update_idletasks()

    def _log(self, msg, tag=None):
        self._write(self._log_box, msg, tag)

    def _warn(self, msg):
        self._warn_count += 1
        self._write(self._warn_box, f"  {msg}", "warn")
        self.after(0, lambda: self._log_nb.tab(
            1, text=f"  Warnings & Errors ({self._warn_count})  "))

    def _safe_log(self, msg, tag=None):
        self.after(0, lambda m=msg, t=tag: self._log(m, t))

    def _safe_warn(self, msg):
        self.after(0, lambda m=msg: self._warn(m))

    # ── Progress ──────────────────────────────────────────────────────────────

    def _set_stat(self, key, val):
        self._stat_vars[key].set(str(val))
        self.update_idletasks()

    def _safe_stat(self, key, val):
        self.after(0, lambda: self._set_stat(key, val))

    def _set_cur(self, f, note=""):
        self._cur_file.set(f)
        self._cur_note.set(note)
        self.update_idletasks()

    def _safe_cur(self, f, note=""):
        self.after(0, lambda: self._set_cur(f, note))

    def _set_zip_progress(self, cur, total):
        pct = int(cur / total * 100) if total else 0
        self._zip_bar["value"] = pct
        self._zip_lbl.config(text=f"ZIP {cur} of {total}  ({pct}%)")
        self.update_idletasks()

    def _increment_grand(self, saved=1, overlay=0, gps=0, skipped=0,
                         year=0, month=0, is_video=False):
        self._grand_done    += saved + skipped
        self._grand_saved    = getattr(self, "_grand_saved",    0) + saved
        self._grand_overlays = getattr(self, "_grand_overlays", 0) + overlay
        self._grand_gps      = getattr(self, "_grand_gps",      0) + gps
        self._grand_skipped  = getattr(self, "_grand_skipped",  0) + skipped

        # Update stat cards immediately
        if saved:    self._set_stat("saved",    self._grand_saved)
        if overlay:  self._set_stat("overlays", self._grand_overlays)
        if gps:      self._set_stat("gps",      self._grand_gps)
        if skipped:  self._set_stat("skipped",  self._grand_skipped)

        # Track summary by year/month
        if saved and year > 0:
            y = self._summary.setdefault(year, {})
            m = y.setdefault(month, {"photos": 0, "videos": 0, "gps": 0})
            if is_video:
                m["videos"] += 1
            else:
                m["photos"] += 1
            if gps:
                m["gps"] += 1
            self._refresh_summary()

        # Update grand progress bar
        done  = self._grand_done
        total = self._grand_total
        if total > 0:
            pct       = int(done / total * 100)
            remaining = total - done
            self._grand_bar["value"] = pct
            self._grand_lbl.config(
                text=f"{done:,} of {total:,}  —  {remaining:,} remaining  ({pct}%)")
            if self._start_time and done > 5:
                elapsed = time.time() - self._start_time
                rate    = done / elapsed
                eta_s   = int(remaining / rate) if rate > 0 else 0
                h, rem  = divmod(eta_s, 3600)
                m_t, s  = divmod(rem, 60)
                eta_txt = (f"{h}h {m_t:02d}m {s:02d}s" if h
                           else f"{m_t}m {s:02d}s" if m_t else f"{s}s")
                self._eta_var.set(f"Estimated time remaining: {eta_txt}")
                self._rate_var.set(f"Processing rate: {rate * 60:.1f} files / min")
        else:
            self._grand_lbl.config(text=f"{done:,} files processed")
        self.update_idletasks()

    def _safe_increment_grand(self, saved=1, overlay=0, gps=0, skipped=0,
                               year=0, month=0, is_video=False):
        self.after(0, lambda: self._increment_grand(
            saved=saved, overlay=overlay, gps=gps, skipped=skipped,
            year=year, month=month, is_video=is_video))

    def _safe_file_progress(self, cur, total, remaining):
        pass

    def _set_vprogress(self, name, frame, total_f, pct, speed=""):
        self._vbar["value"] = pct
        short = (name[:26] + "...") if len(name) > 28 else name
        self._vpct.config(
            text=f"{short}  {pct}%{(f'  {speed}') if speed else ''}")
        self.update_idletasks()

    def _safe_vprogress(self, name, frame, total_f, pct, speed=""):
        self.after(0, lambda: self._set_vprogress(
            name, frame, total_f, pct, speed))

    def _reset_vbar(self):
        self._vbar["value"] = 0
        self._vpct.config(text="")

    def _populate_breakdown(self, json_by_dt):
        """
        Build year/month counts from JSON and populate the year selector.
        Called once after JSON is loaded, before processing starts.
        """
        data = {}  # {year: {month: count}}
        for entry in json_by_dt.values():
            dt = entry.get("date")
            if not dt:
                continue
            y, m = dt.year, dt.month
            data.setdefault(y, {})
            data[y][m] = data[y].get(m, 0) + 1
        self._breakdown_data = data

        years = sorted(data.keys(), reverse=True)
        menu  = self._year_menu["menu"]
        menu.delete(0, "end")
        for yr in years:
            total = sum(data[yr].values())
            menu.add_command(
                label=str(yr),
                command=lambda y=yr: (self._year_var.set(str(y)),
                                      self._update_breakdown()))
        if years:
            self._year_var.set(str(years[0]))
            self._update_breakdown()

    def _update_breakdown(self):
        """Refresh the month table for the selected year."""
        try:
            year = int(self._year_var.get())
        except ValueError:
            return

        year_data = self._breakdown_data.get(year, {})
        year_total = sum(year_data.values())
        max_count  = max(year_data.values()) if year_data else 1

        self._year_total_lbl.config(
            text=f"{year_total:,} memories in {year}")

        for mi, (cnt_var, bar_var) in enumerate(self._month_rows, 1):
            count = year_data.get(mi, 0)
            cnt_var.set(str(count) if count else "—")
            bar_var.set(int(count / max_count * 100) if max_count > 0 else 0)

    def _skip_current(self):
        self._kill_flag.set()
        self._skip_btn.config(state="disabled", text="Skipping...")
        self._safe_log("Skip requested — stopping current video encode...", "dim")

    def _tick_elapsed(self):
        if not self._running or self._start_time is None:
            return
        elapsed = int(time.time() - self._start_time)
        h, rem  = divmod(elapsed, 3600)
        m, s    = divmod(rem, 60)
        self._elapsed_var.set(
            f"Elapsed: {h}h {m:02d}m {s:02d}s" if h
            else f"Elapsed: {m}m {s:02d}s")
        self.after(1000, self._tick_elapsed)

    # ── Main run ──────────────────────────────────────────────────────────────

    def _start(self):
        if self._running:
            return
        if not self._queue:
            self._warn("No ZIP files queued. Use the Add buttons on the Files tab.")
            return
        out = self._out.get().strip()
        if not out:
            self._warn("Please choose an output folder on the Files tab.")
            return

        self._running     = True
        self._warn_count  = 0
        self._start_time  = time.time()
        self._grand_done  = 0
        self._grand_total = 0
        self._log_nb.tab(1, text="  Warnings & Errors  ")
        self._kill_flag.clear()

        self._start_btn.config(state="disabled", bg=FG3, text="Processing...")
        self._start_note.config(
            text="Switch to the Progress or Log tab to monitor.")
        self._skip_btn.config(state="disabled", text="Skip this video")
        self._nb.select(2)

        self._grand_saved    = 0
        self._grand_overlays = 0
        self._grand_gps      = 0
        self._grand_skipped  = 0
        self._summary        = {}
        self._summary_year_frames = {}
        # Clear and rebuild summary tab
        for w in self._summary_inner.winfo_children():
            w.destroy()
        self._summary_placeholder = tk.Label(
            self._summary_inner,
            text="Processing — summary will appear here as memories are saved.",
            font=FONT_S, bg=BG2, fg=FG3, justify="center")
        self._summary_placeholder.pack(pady=60)
        # {year: {month: {"photos": 0, "videos": 0, "gps": 0}}}
        self._summary        = {}
        for key in self._stat_vars:
            self._stat_vars[key].set("—")
        self._elapsed_var.set("Starting...")
        self._rate_var.set("")
        self._eta_var.set("")
        self._set_cur("Starting up...")
        self._grand_bar["value"] = 0
        self._grand_lbl.config(text="")
        self._tick_elapsed()

        def run():
            total_zips     = len(self._queue)
            grand_matched  = 0
            grand_overlays = 0
            grand_skipped  = 0
            grand_gps      = 0
            all_failed     = []

            try:
                mins         = int(self._timeout_var.get())
                hard_timeout = mins * 60 if mins > 0 else 999_999
            except Exception:
                hard_timeout = 600

            # Step 1: Find master JSON
            self._safe_cur("Scanning for master JSON...",
                           "Checking all ZIPs before any media is processed.")
            self._safe_log(f"\n{'─'*62}", "dim")
            self._safe_log(
                "  Pre-scan: searching for master JSON file...", "info")

            _, json_by_dt = find_json_in_queue(
                self._queue, self._safe_log, self._safe_warn)

            if json_by_dt:
                self._grand_total = len(json_by_dt)
                self.after(0, lambda: self._grand_bar.config(value=0))
                self.after(0, lambda jd=json_by_dt: self._populate_breakdown(jd))
                self._safe_log(
                    f"\n  Strategy: each ZIP's HTML is cross-referenced against\n"
                    f"  the master JSON to get the correct time and GPS for every file.\n",
                    "ok")
            else:
                self._safe_log(
                    "\n  No master JSON found — files saved with date-only timestamps.\n"
                    "  Make sure ZIP 1 (the one without a number in the filename) is included.\n",
                    "dim")

            # Step 2: Process each ZIP
            for zi, src_path in enumerate(self._queue, 1):
                self.after(0, lambda z=zi, t=total_zips:
                           self._set_zip_progress(z, t))
                self._safe_stat("zip", f"{zi}/{total_zips}")
                self.after(0, self._reset_vbar)
                self._safe_cur(src_path.name, f"ZIP {zi} of {total_zips}")

                self._safe_log(f"\n{'─'*62}", "dim")
                self._safe_log(
                    f"  ZIP {zi} of {total_zips}:  {src_path.name}", "info")
                self._safe_log(f"{'─'*62}", "dim")

                self.after(0, lambda: self._skip_btn.config(
                    state="normal", text="Skip this video"))
                self._kill_flag.clear()

                matched, ovs, skipped, gps, failed = process_zip(
                    source_path    = src_path,
                    out_dir        = out,
                    json_by_dt     = json_by_dt,
                    log            = self._safe_log,
                    warn           = self._safe_warn,
                    progress       = self._safe_file_progress,
                    video_progress = self._safe_vprogress,
                    do_overlay     = self._do_overlay.get(),
                    kill_flag      = self._kill_flag,
                    hard_timeout   = hard_timeout,
                    on_file_done   = self._safe_increment_grand,
                )
                self._kill_flag.clear()
                all_failed.extend(failed)

                grand_matched  += matched
                grand_overlays += ovs
                grand_skipped  += skipped
                grand_gps      += gps

                self._safe_log(
                    f"\n  ZIP {zi} done — {matched} saved, {ovs} overlays, "
                    f"{gps} GPS, {skipped} skipped.\n", "ok")

            self.after(0, lambda: self._skip_btn.config(
                state="disabled", text="Skip this video"))

            # Failed files summary
            if all_failed:
                self._safe_log(f"\n{'─'*62}", "dim")
                self._safe_log(
                    f"  {len(all_failed)} file(s) had issues — "
                    f"see Warnings & Errors tab.", "dim")
                for ff in all_failed:
                    self._safe_warn("─" * 44)
                    self._safe_warn(f"  Output  : {ff['dest']}")
                    self._safe_warn(f"  Reason  : {ff['reason']}")
                    self._safe_warn(f"  Source  : {ff['source']}")
                    if ff.get("overlay"):
                        self._safe_warn(f"  Overlay : {ff['overlay']}")
                    if ff.get("json_entry"):
                        e = ff["json_entry"]
                        dts = (e["date"].strftime("%Y-%m-%d %H:%M:%S UTC")
                               if e.get("date") else "unknown")
                        self._safe_warn(f"  Date    : {dts}")
                        if e.get("lat") and e.get("lon"):
                            self._safe_warn(
                                f"  GPS     : {e['lat']}, {e['lon']}")

            # Final summary
            self._safe_cur(
                "Complete!",
                f"All {total_zips} ZIP{'s' if total_zips != 1 else ''} processed.")
            self._safe_log(f"\n{'='*62}", "dim")
            self._safe_log(
                f"  All done!\n"
                f"  {grand_matched:,} memories organised\n"
                f"  {grand_overlays:,} overlays applied\n"
                f"  {grand_gps:,} GPS tags embedded\n"
                f"  {grand_skipped:,} skipped", "ok")
            self._safe_log(f"\n  Output folder: {out}", "dim")

            self._running = False
            self.after(0, lambda: self._start_btn.config(
                state="normal", bg=ACCENT, text="Process All ZIPs  →"))
            self.after(0, lambda: self._start_note.config(text=""))
            self.after(0, lambda: self._skip_btn.config(
                state="disabled", text="Skip this video"))
            self.after(0, self._reset_vbar)

        threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    App().mainloop()
