"""Getting video in: link download, probing, audio extraction, loudness."""
import contextlib
import json
import math
import queue
import re
import subprocess
import threading
import time
from collections import deque
from fractions import Fraction
from pathlib import Path

import numpy as np
import soundfile as sf

from . import config

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)

# ---------------------------------------------------------------- when YouTube refuses this server
# YouTube sometimes refuses a server's downloads altogether ("sign in to confirm you're not a bot"). It is a
# refusal of the server, not of one video, so once it happens YouTube links are turned away at once for a few
# minutes, with a clear message, instead of each one waiting to fail. Then the next link simply tries again.
YT_PAUSE = 600
_yt_refused_until = 0.0
YT_BLOCKED = ("YouTube isn't letting QlipIt Studio download videos right now. Upload the video file instead, or use a "
              "Twitch or Kick link. YouTube links will be tried again in a few minutes.")
_BOT_CHECK = re.compile(r"not a bot|confirm you.?re not|sign in to confirm(?! your age)", re.I)


class YouTubeBlocked(RuntimeError):
    pass


def is_youtube(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse((url or "").strip()).hostname or "").lower()
    return host in ("youtube.com", "youtu.be") or host.endswith((".youtube.com", ".youtu.be"))


def youtube_paused() -> bool:
    return time.time() < _yt_refused_until


def check_youtube(url: str):
    """Raise YouTubeBlocked straight away if YouTube is refusing this server right now."""
    if is_youtube(url) and youtube_paused():
        raise YouTubeBlocked(YT_BLOCKED)


def note_refusal(url: str, err) -> bool:
    """If this error is YouTube refusing the server, pause YouTube links for a while. True if it was."""
    global _yt_refused_until
    if is_youtube(url) and _BOT_CHECK.search(str(err)):
        _yt_refused_until = time.time() + YT_PAUSE
        return True
    return False


@contextlib.contextmanager
def _youtube_guard(url):
    check_youtube(url)
    try:
        yield
    except YouTubeBlocked:
        raise
    except Exception as e:  # noqa: BLE001
        if note_refusal(url, e):
            raise YouTubeBlocked(YT_BLOCKED) from None
        raise


def site_allowed(url: str) -> bool:
    """Links are fetched by the server, so only known video sites are accepted (never arbitrary addresses)."""
    from urllib.parse import urlparse
    try:
        u = urlparse(url.strip())
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and any(host == s or host.endswith("." + s) for s in config.ALLOWED_SITES)


_JOB_PROCS: dict = {}
_PROCS_LOCK = threading.Lock()


def register_proc(jid: str, proc):
    if not jid or not proc:
        return
    with _PROCS_LOCK:
        _JOB_PROCS.setdefault(str(jid), set()).add(proc)


def unregister_proc(jid: str, proc):
    if not jid or not proc:
        return
    with _PROCS_LOCK:
        if str(jid) in _JOB_PROCS:
            _JOB_PROCS[str(jid)].discard(proc)
            if not _JOB_PROCS[str(jid)]:
                _JOB_PROCS.pop(str(jid))


def kill_job_procs(jid: str):
    if not jid:
        return
    with _PROCS_LOCK:
        procs = _JOB_PROCS.pop(str(jid), set())
    for p in procs:
        _kill_reap(p)


def _kill_reap(p):
    with contextlib.suppress(OSError):
        p.kill()
    with contextlib.suppress(OSError):
        p.wait(timeout=5)


class ProcessFailed(RuntimeError):
    """A completed command failed; cancellation and timeout must not trigger encoder fallback."""


def report_progress(callback, phase, completed=None, total=None, unit=None, phase_attempt=1):
    if callback:
        try:
            callback(phase=phase, completed=completed, total=total, unit=unit, phase_attempt=phase_attempt)
        except Exception:  # a display/log callback must never invalidate an otherwise successful render
            pass


def _ffmpeg_seconds(fields):
    for key in ("out_time_us", "out_time_ms", "out_time"):
        try:
            value = fields[key]
            if key == "out_time":
                h, m, s = value.split(":")
                value = float(h) * 3600 + float(m) * 60 + float(s)
            else:
                # FFmpeg's historical out_time_ms is microseconds too, despite its name.
                value = float(value) / 1_000_000
            if math.isfinite(value) and value >= 0:
                return value
        except (KeyError, TypeError, ValueError):
            continue
    return None


def run(cmd, cwd=None, timeout=3600, jid=None, cancel=None, on_progress=None, duration=None,
        phase="media", phase_attempt=1):
    """Drain both pipes, report measured FFmpeg seconds, and always reap stopped children."""
    if cancel:
        cancel()
    total = float(duration) if duration is not None else None
    if total is not None and (not math.isfinite(total) or total <= 0):
        raise ValueError("Progress duration must be positive and finite.")
    command = list(cmd)
    measured = on_progress is not None
    if measured and Path(str(command[0])).stem.lower() == "ffmpeg" and "-progress" not in command:
        command[1:1] = ["-nostats", "-progress", "pipe:1"]
    report_progress(on_progress, phase, 0.0 if total is not None else None, total,
                    "seconds" if total is not None else None, phase_attempt)
    p = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    register_proc(jid, p)
    stderr = deque(maxlen=8)       # at most 32 KiB, even when FFmpeg prints without newlines
    stdout = deque(maxlen=8) if measured else []
    updates = queue.Queue(maxsize=64)

    def read_stderr():
        try:
            while True:
                chunk = p.stderr.read(4096)
                if not chunk:
                    break
                stderr.append(chunk)
        except (OSError, ValueError):
            pass

    def read_stdout():
        fields = {}
        try:
            while True:
                chunk = p.stdout.readline(4096) if measured else p.stdout.read(4096)
                if not chunk:
                    break
                stdout.append(chunk)
                if measured:
                    key, sep, value = chunk.strip().partition("=")
                    if sep and key in ("out_time_us", "out_time_ms", "out_time"):
                        fields[key] = value
                    if key == "progress":
                        seconds = _ffmpeg_seconds(fields)
                        fields.clear()
                        if seconds is not None:
                            if updates.full():
                                with contextlib.suppress(queue.Empty):
                                    updates.get_nowait()
                            updates.put_nowait(seconds)
        except (OSError, ValueError):
            pass

    readers = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
    last = -1.0

    def advance():
        nonlocal last
        while True:
            try:
                seconds = updates.get_nowait()
            except queue.Empty:
                break
            if total is not None:
                seconds = min(total, seconds)
            if seconds > last:
                last = seconds
                report_progress(on_progress, phase, seconds, total, "seconds", phase_attempt)

    try:
        for reader in readers:
            reader.start()
        began = time.monotonic()
        while True:
            if cancel:
                cancel()
            if timeout is not None and time.monotonic() - began >= timeout:
                raise RuntimeError(f"{Path(str(cmd[0])).name} took too long and was stopped.")
            advance()
            try:
                p.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                continue
        for reader in readers:
            reader.join(timeout=2)
        advance()
        if cancel:
            cancel()
        if p.returncode != 0:
            raise ProcessFailed(f"{Path(str(cmd[0])).name} failed: {''.join(stderr)[-1500:]}")
        return "".join(stdout)
    finally:
        if p.poll() is None:
            _kill_reap(p)
        for reader in readers:
            if reader.ident is not None:
                reader.join(timeout=2)
        p.stdout.close()
        p.stderr.close()
        unregister_proc(jid, p)


class _Quiet:
    def debug(self, m): pass
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass


def _ydl(opts):
    import yt_dlp
    # a channel or playlist link is answered as "a list" straight away, never walked video by video
    return yt_dlp.YoutubeDL({"noplaylist": True, "quiet": True, "no_warnings": True, "logger": _Quiet(),
                             "extract_flat": "in_playlist", "playlistend": 1, "socket_timeout": 20,
                             "concurrent_fragment_downloads": 4, **opts})


def _video_format(height):
    return f"bv*[height<={height}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={height}]+ba/b[height<={height}]/b"


def _biggest(job_dir: Path, stem: str) -> Path:
    files = sorted((p for p in job_dir.glob(stem + ".*") if p.suffix not in (".part", ".ytdl")),
                   key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise RuntimeError("Download finished but no file was produced.")
    return files[0]


def info(url: str) -> dict:
    """Title, channel and length, without downloading anything."""
    with _youtube_guard(url), _ydl({}) as ydl:
        i = ydl.extract_info(url, download=False)
    return {"title": i.get("title") or "", "uploader": i.get("uploader") or i.get("channel") or "",
            "duration": float(i.get("duration") or 0), "is_live": bool(i.get("is_live")),
            "thumbnail": i.get("thumbnail") or "", "kind": i.get("_type") or "video"}


def youtube_words(url: str):
    """YouTube's own transcript of a video, word by word: ([{'w', 's', 'e'}, ...], language), or None if the video
    has none. Only the transcript in the language that was spoken is used, never a translation."""
    if not is_youtube(url):
        return None
    import json
    with _youtube_guard(url), _ydl({}) as ydl:
        i = ydl.extract_info(url, download=False)
        auto = i.get("automatic_captions") or {}
        key = next((k for k in auto if k.endswith("-orig")), None)
        if not key and i.get("language") in auto:
            key = i["language"]
        track = next((f for f in auto.get(key) or [] if f.get("ext") == "json3" and f.get("url")), None) if key else None
        if not track:
            return None
        raw = ydl.urlopen(track["url"]).read(40_000_000)
    return _caption_words(json.loads(raw), source_end=float(i.get("duration") or 0) or None), key.replace("-orig", "")


def youtube_english_words(url: str):
    """YouTube's English captions of a video spoken in another language: ones a person wrote if there are any,
    otherwise YouTube's own translation of its transcript. Word list like youtube_words, or None."""
    if not is_youtube(url):
        return None
    import json
    with _youtube_guard(url), _ydl({}) as ydl:
        i = ydl.extract_info(url, download=False)
        tracks = []
        for group in (i.get("subtitles") or {}, i.get("automatic_captions") or {}):
            for k in ("en", "en-US", "en-GB"):
                tracks.append(next((f for f in group.get(k) or [] if f.get("ext") == "json3" and f.get("url")), None))
        track = next((t for t in tracks if t), None)
        if not track:
            return None
        raw = ydl.urlopen(track["url"]).read(40_000_000)
    words = _caption_words(json.loads(raw), source_end=float(i.get("duration") or 0) or None)
    return words or None


def _caption_words(data: dict, source_end=None) -> list:
    """Expand segment text within its own interval; interpolated timings are explicitly approximate."""
    if source_end is not None and (isinstance(source_end, bool) or not isinstance(source_end, (int, float))
                                   or not math.isfinite(source_end) or source_end < 0):
        raise ValueError("Caption source end must be finite and nonnegative.")

    def seconds(value):
        try:
            value = float(value or 0) / 1000
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value >= 0 else None

    events = []
    raw = data.get("events") if isinstance(data, dict) else None
    for ev in raw if isinstance(raw, list) else []:
        if not isinstance(ev, dict):
            continue
        start, duration = seconds(ev.get("tStartMs")), seconds(ev.get("dDurationMs"))
        if start is not None and duration is not None:
            events.append((start, duration, ev))
    events.sort(key=lambda ev: ev[0])
    out = []
    for index, (t0, dur, ev) in enumerate(events):
        segments = {}
        raw = ev.get("segs")
        for x in raw if isinstance(raw, list) else []:
            if not isinstance(x, dict) or not isinstance(x.get("utf8"), str):
                continue
            text, offset = x["utf8"].strip(), seconds(x.get("tOffsetMs"))
            if offset is None or not text or re.fullmatch(r"[\[(].*[\])]", text):
                continue
            parts = [w for w in text.split() if not re.fullmatch(r"[\[(].*[\])]", w) and w not in ("[", "]", "__")]
            group = segments.setdefault(offset, {"parts": [], "timed": False})
            group["parts"].extend(parts)
            group["timed"] |= "tOffsetMs" in x
        offsets = sorted(segments)
        if not offsets:
            continue
        if dur <= 0:
            next_start = next((start for start, _, _ in events[index + 1:] if start > t0), None)
            event_end = next_start if next_start is not None else t0 + max(offsets) + 0.4
        else:
            event_end = t0 + dur
        if source_end is not None:
            event_end = min(event_end, source_end)
        for i, offset in enumerate(offsets):
            group = segments[offset]
            parts, at = group["parts"], t0 + offset
            until = min(event_end, t0 + offsets[i + 1] if i + 1 < len(offsets) else event_end)
            if until <= at or not parts:
                continue
            step = (until - at) / len(parts)
            for n, w in enumerate(parts):
                start = at + n * step
                stop = min(until, at + (n + 1) * step, start + 0.6)
                out.append({"w": w, "s": start, "e": stop,
                            "timing": "json3" if group["timed"] and len(parts) == 1 else "approximate"})
    out.sort(key=lambda w: w["s"])
    for n, w in enumerate(out):
        nxt = out[n + 1]["s"] if n + 1 < len(out) else w["s"] + 0.4
        bound = source_end if source_end is not None else float("inf")
        w["s"] = min(bound, round(w["s"], 6))
        w["e"] = max(w["s"], min(bound, nxt, round(w["e"], 6)))
    return out


_PREVIEWS: dict = {}


def preview(url: str) -> dict:
    """What the user will get from a link, checked before any work starts. The thumbnail comes back inline
    (the page may only load images from this site). Cached, so retyping a link costs nothing."""
    url = url.strip()
    if url in _PREVIEWS:
        return _PREVIEWS[url]
    i = info(url)
    if i["kind"] != "video" or not i["title"]:
        raise ValueError("That link is a channel or playlist, not a single video.")
    thumb = ""
    t = i.get("thumbnail") or ""
    if t.startswith("https://"):
        import base64
        import urllib.request
        try:
            req = urllib.request.Request(t, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
                data = r.read(600_000 + 1)
            if ctype in ("image/jpeg", "image/png", "image/webp") and len(data) <= 600_000:
                thumb = f"data:{ctype};base64," + base64.b64encode(data).decode()
        except Exception:
            thumb = ""
    out = {"title": i["title"][:200], "uploader": i["uploader"][:120], "duration": i["duration"],
           "is_live": i["is_live"], "thumb": thumb}
    if len(_PREVIEWS) > 300:
        _PREVIEWS.pop(next(iter(_PREVIEWS)))
    _PREVIEWS[url] = out
    return out


def save_thumb(job_dir: Path, url: str = "", video: Path = None) -> bool:
    """A small picture for the video list: the link's own thumbnail, or a frame from an uploaded file."""
    import base64
    out = Path(job_dir) / "clips" / "thumb.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        if url:
            p = _PREVIEWS.get(url.strip()) or preview(url)
            if not p.get("thumb"):
                return False
            out.write_bytes(base64.b64decode(p["thumb"].split(",", 1)[1]))
            return True
        if video:
            d = probe(video).get("duration") or 0
            run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{min(3.0, d / 3):.2f}", "-i", str(video),
                 "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(out)])
            return out.is_file()
    except Exception:
        return False
    return False


def _download_hook(cancel, on_progress=None, *, legacy=False, phase_attempt=1):
    last, filename = 0, None

    def hook(d):
        nonlocal last, filename, phase_attempt
        if cancel:
            cancel()
        if d.get("filename") and filename != d["filename"]:
            if filename is not None:
                phase_attempt += 1
            filename, last = d["filename"], 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if d.get("status") == "downloading":
            last = max(last, d.get("downloaded_bytes") or 0)
            if legacy:
                if on_progress and total:
                    try:
                        on_progress(last / total)
                    except Exception:
                        pass
            else:
                report_progress(on_progress, "download", last, total, "bytes", phase_attempt)
        if cancel:
            cancel()
    return hook


def download_audio(url: str, job_dir: Path, on_progress=None, *, jid=None, cancel=None) -> Path:
    """Audio only: a few seconds even for long videos, and all the AI needs to pick clips."""
    if cancel:
        cancel()
    hook = _download_hook(cancel, on_progress, legacy=True)
    with _youtube_guard(url), _ydl({"format": "ba[ext=m4a]/ba/worst", "outtmpl": str(job_dir / "audio_src.%(ext)s"),
                                  "progress_hooks": [hook], "postprocessor_hooks": [hook]}) as ydl:
        ydl.download([url])
    if cancel:
        cancel()
    return _biggest(job_dir, "audio_src")


def download(url: str, job_dir: Path, height=720, *, jid=None, cancel=None, on_progress=None) -> Path:
    """The whole video. Used for shorter videos, where this is quicker than fetching pieces."""
    if cancel:
        cancel()
    hook = _download_hook(cancel, on_progress)
    report_progress(on_progress, "download")
    with _youtube_guard(url), _ydl({"format": _video_format(height), "merge_output_format": "mp4",
                                  "outtmpl": str(job_dir / "source.%(ext)s"),
                                  "progress_hooks": [hook], "postprocessor_hooks": [hook]}) as ydl:
        ydl.download([url])
    if cancel:
        cancel()
    return _biggest(job_dir, "source")


def download_section(url: str, out_dir: Path, stem: str, start: float, end: float, height=720,
                     *, jid=None, cancel=None, on_progress=None) -> Path:
    """Just one stretch of the video. The cut lands on a keyframe, so find_offset() pins the true start.

    A stretch cut out of YouTube's HLS stream comes back with the sound only (a known yt-dlp limit), so the
    piece is checked before it is used and fetched again as one plain file if it has no picture."""
    if cancel:
        cancel()
    out_dir.mkdir(parents=True, exist_ok=True)
    why = None
    extras = {key: val for key, val in {"jid": jid, "cancel": cancel, "on_progress": on_progress}.items() if val is not None}
    for attempt, fmt in enumerate((_video_format(height), f"b[height<={height}]/b"), 1):
        if cancel:
            cancel()
        for p in out_dir.glob(stem + ".*"):
            if cancel:
                cancel()
            p.unlink(missing_ok=True)                 # a half file from the last try would be picked up instead
        try:
            _section(url, out_dir, stem, start, end, fmt, **({**extras, "phase_attempt": attempt} if extras else {}))
            if cancel:
                cancel()
            got = _biggest(out_dir, stem)
            if _has_video(got, **{key: val for key, val in {"jid": jid, "cancel": cancel}.items() if val is not None}):
                return got
            why = "it came back without a picture"
        except Exception as e:  # noqa: BLE001 - the second format is the retry
            if cancel:
                cancel()
            why = str(e)
    raise RuntimeError(why or "no file was produced")


def _section(url: str, out_dir: Path, stem: str, start: float, end: float, fmt: str,
             *, jid=None, cancel=None, on_progress=None, phase_attempt=1):
    from yt_dlp.utils import download_range_func
    if cancel:
        cancel()
    hook = _download_hook(cancel, on_progress, phase_attempt=phase_attempt)
    report_progress(on_progress, "download", phase_attempt=phase_attempt)
    with _youtube_guard(url), _ydl({"format": fmt, "merge_output_format": "mp4",
                                  "extractor_args": {"youtube": {"skip": ["hls"]}}, "format_sort": ["proto:https"],
                                  "outtmpl": str(out_dir / (stem + ".%(ext)s")),
                                  "progress_hooks": [hook], "postprocessor_hooks": [hook],
                                  "download_ranges": download_range_func(None, [(max(0.0, start), end)])}) as ydl:
        ydl.download([url])
    if cancel:
        cancel()


def _has_video(path: Path, *, jid=None, cancel=None) -> bool:
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)], jid=jid, cancel=cancel)
    return any(s["codec_type"] == "video" for s in json.loads(out)["streams"])



def find_offset(full: np.ndarray, part: np.ndarray, expected: float, search=20.0) -> float:
    """Where in the full audio (seconds) a downloaded piece really begins, by matching the sound itself."""
    n = min(len(part), 30 * 16000)
    a0 = max(0, int((expected - search) * 16000))
    hay = full[a0: int((expected + search) * 16000) + n].astype(np.float32)
    needle = part[:n].astype(np.float32)
    if n < 16000 or len(hay) < n or float(np.abs(needle).mean()) < 20:
        return expected  # too short or silent to match: trust the requested time
    size = 1 << (len(hay) + n).bit_length()
    corr = np.fft.irfft(np.fft.rfft(hay, size) * np.conj(np.fft.rfft(needle, size)), size)[: len(hay) - n + 1]
    return (a0 + int(np.argmax(corr))) / 16000


def probe(path: Path, require_audio=False, *, jid=None, cancel=None) -> dict:
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
              jid=jid, cancel=cancel)
    data = json.loads(out)
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    if not v:
        raise RuntimeError("This file has no video track.")
    audio = any(s.get("codec_type") == "audio" for s in streams)
    if require_audio and not audio:
        raise RuntimeError("This file has no audio track, so there is nothing to transcribe.")
    w, h = int(v["width"]), int(v["height"])
    if w * h > 3840 * 2160 or w < 16 or h < 16:
        raise RuntimeError("This video's resolution isn't supported (4K is the largest).")
    try:
        sar = Fraction(str(v.get("sample_aspect_ratio") or "1:1").replace(":", "/"))
        if sar <= 0:
            sar = Fraction(1)
    except (ValueError, ZeroDivisionError):
        sar = Fraction(1)
    rotation = next((x["rotation"] for x in v.get("side_data_list") or [] if "rotation" in x),
                    (v.get("tags") or {}).get("rotate", 0))
    try:
        rotation = float(rotation)
        if not math.isfinite(rotation) or abs(rotation / 90 - round(rotation / 90)) > 0.01:
            raise ValueError
        rotation = int(round(rotation)) % 360
    except (ValueError, TypeError):
        raise RuntimeError("This video's rotation isn't supported.") from None
    dw, dh = int(round(w * sar)), h
    if rotation in (90, 270):
        dw, dh = dh, dw
    if dw < 16 or dh < 16 or dw * dh > 3840 * 2160:
        raise RuntimeError("This video's display resolution isn't supported.")
    duration = float((data.get("format") or {}).get("duration") or v.get("duration") or 0)
    if not math.isfinite(duration) or duration < 0:
        raise RuntimeError("This video's duration isn't valid.")
    return {"width": dw, "height": dh, "coded_width": w, "coded_height": h,
            "sar": f"{sar.numerator}:{sar.denominator}", "rotation": rotation, "duration": duration,
            "audio": audio, "codec": v.get("codec_name") or "", "pix_fmt": v.get("pix_fmt") or ""}


def _audio_span(start, end):
    if isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start) or start < 0:
        raise ValueError("Audio start must be finite and nonnegative.")
    if end is not None and (isinstance(end, bool) or not isinstance(end, (int, float)) or not math.isfinite(end) or end <= start):
        raise ValueError("Audio end must be finite and after its start.")


def extract_audio(src: Path, wav: Path, start=0, end=None, jid=None, cancel=None, on_progress=None):
    _audio_span(start, end)
    # first_pts=0 pads any gap before the audio starts, so second 0 of the wav is second 0 of the file
    # -t: never more audio than the longest video allowed, whatever the file claims about itself
    limit = int(config.MAX_VIDEO_HOURS * 3600) + 60
    duration = min(limit, end - start) if end is not None else None
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if start:
        cmd += ["-ss", str(start)]
    run(cmd + ["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-t", str(duration if duration is not None else limit),
               "-af", "aresample=async=1:first_pts=0", "-c:a", "pcm_s16le", str(wav)],
        jid=jid, cancel=cancel, on_progress=on_progress, duration=duration, phase="audio")


def load_audio(wav: Path, start=0, end=None) -> np.ndarray:
    _audio_span(start, end)
    with sf.SoundFile(str(wav)) as audio:
        if audio.samplerate != 16000 or audio.channels != 1:
            raise ValueError("Speech audio must be mono at 16000 Hz.")
        first = int(start * 16000)
        if first > len(audio):
            raise ValueError("Audio span starts beyond the available audio.")
        audio.seek(first)
        frames = min(len(audio), int(end * 16000)) - first if end is not None else -1
        return audio.read(frames=frames, dtype="int16")


def loudness_per_second(audio: np.ndarray) -> np.ndarray:
    """RMS level in dB for each second of audio."""
    n = len(audio) // 16000
    if n == 0:
        return np.zeros(1, dtype=np.float32)
    x = audio[: n * 16000].reshape(n, 16000).astype(np.float32) / 32768.0
    rms = np.sqrt((x ** 2).mean(axis=1) + 1e-10)
    return (20 * np.log10(rms)).astype(np.float32)
