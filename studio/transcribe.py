"""Speech to text with word timestamps, using Parakeet 0.6b through sherpa-onnx.

It runs in one of two places. On this machine's processors (the default), or, when QLIPIT_ASR_URL is set, on a
separate machine with a graphics card (studio/asr_worker.py), which does the same work many times faster. If that
machine cannot be reached, the pieces are done here instead, so transcription never stops because of it."""
import math
import os
import queue
import threading
import time

import numpy as np

from . import config

_lock = threading.Lock()
SR, MAX_PIECE, MAX_TOKENS = 16000, 16000 * 40, 4096


def _positive(name, default):
    try:
        value = int(config.env(name, str(default)))
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} must be a positive integer.") from e
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


# Several copies of the speech model, each on a few cores, instead of one copy on all of them: one copy cannot keep
# a big machine busy, and several videos transcribing at once would queue behind each other inside it. On a small
# machine (under 8 cores) this is still a single copy, as before.
_CPUS = os.cpu_count() or 4
PROVIDER = (os.environ.get("QLIPIT_ASR_PROVIDER") or os.environ.get("PEAKCUT_ASR_PROVIDER", "cpu")).strip().lower() or "cpu"      # "cuda" on a graphics card
THREADS = _positive("QLIPIT_ASR_THREADS", 4 if _CPUS >= 8 else max(2, _CPUS - 2))
COPIES = _positive("QLIPIT_ASR_COPIES", 1 if PROVIDER != "cpu" else max(1, _CPUS // THREADS) if _CPUS >= 8 else 1)
_free: "queue.LifoQueue" = queue.LifoQueue()
_made = 0

# The machine with the graphics card, if there is one.
REMOTE = (os.environ.get("QLIPIT_ASR_URL") or os.environ.get("PEAKCUT_ASR_URL", "")).strip().rstrip("/")
REMOTE_TOKEN = (os.environ.get("QLIPIT_ASR_TOKEN") or os.environ.get("PEAKCUT_ASR_TOKEN", "")).strip()
REMOTE_BATCH = _positive("QLIPIT_ASR_REMOTE_BATCH", 8)         # pieces sent together: the card does them at once
REMOTE_PARALLEL = _positive("QLIPIT_ASR_REMOTE_PARALLEL", 4)   # pieces of one video worked on at once
# The card has a ceiling: beyond a few requests at once it gets no faster. So only this many are sent to it at a
# time (all videos together), and the rest is done on this machine's own processors at the same moment: both are used.
REMOTE_SLOTS = threading.BoundedSemaphore(_positive("QLIPIT_ASR_REMOTE_INFLIGHT", 6))
_remote_down_until = 0.0


def model_ready() -> bool:
    try:
        return (config.ASR_MODEL_DIR / "tokens.txt").is_file() and bool(_model_files(config.ASR_MODEL_DIR))
    except FileNotFoundError:
        return False


def _model_files(d) -> dict:
    """The model's three parts. A graphics card wants the full-precision files; processors the compact ones."""
    kinds = ("fp16", "", "int8") if PROVIDER != "cpu" else ("int8", "", "fp16")
    for k in kinds:
        names = {p: d / (f"{p}.{k}.onnx" if k else f"{p}.onnx") for p in ("encoder", "decoder", "joiner")}
        if all(f.is_file() for f in names.values()):
            return {p: str(f) for p, f in names.items()}
    raise FileNotFoundError(f"No speech model files in {d}")


def _new():
    d = config.ASR_MODEL_DIR
    files = _model_files(d)
    if not (d / "tokens.txt").is_file():
        raise FileNotFoundError(f"The installed English speech model has no tokens.txt in {d}.")
    # Model installation is explicit upstream; acquiring a recognizer never downloads replacement weights.
    import sherpa_onnx
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        **files,
        tokens=str(d / "tokens.txt"),
        num_threads=THREADS,
        sample_rate=SR,
        feature_dim=80,
        decoding_method="greedy_search",
        model_type="nemo_transducer",
        provider=PROVIDER,
    )


def _take():
    """A free copy of the model: one that is idle, a new one while there is room for more, else wait for one."""
    while True:
        rec = _take_now()
        if rec is not None:
            return rec
        try:
            return _free.get(timeout=0.2)
        except queue.Empty:
            pass  # retry allocation if another thread's model construction failed


def _take_now():
    """A copy of the model if one is free (or can still be made) this instant, else None."""
    global _made
    if COPIES <= 0 or THREADS <= 0:
        raise ValueError("Speech recognizer pool and thread counts must be positive.")
    try:
        return _free.get_nowait()
    except queue.Empty:
        pass
    with _lock:
        make = _made < COPIES
        if make:
            _made += 1
    if not make:
        return None
    try:
        return _new()
    except Exception:
        with _lock:
            _made -= 1
        raise


def _recognizer():
    """Kept for callers that only need the model loaded (warm-up)."""
    rec = _take()
    _free.put(rec)
    return rec


def _chunks(audio: np.ndarray, lo=12.0, hi=24.0):
    """Split at the quietest point between lo and hi seconds so words are not cut in half."""
    if not (math.isfinite(lo) and math.isfinite(hi) and 0 < lo < hi <= 40):
        raise ValueError("Speech chunk bounds must be positive and ordered, at most 40 seconds.")
    sr, hop = SR, 800  # 50 ms frames
    n = len(audio)
    pos = 0
    while pos < n:
        if n - pos <= hi * sr:
            yield pos, n
            return
        a, b = (pos + int(lo * sr)) // hop, (pos + int(hi * sr)) // hop
        if b <= a:
            cut = pos + max(1, int(hi * sr))
        else:
            # Scan only the next possible seam, not a float copy of the entire source.
            x = audio[a * hop:b * hop].reshape(b - a, hop).astype(np.float32)
            rms = np.sqrt((x ** 2).mean(axis=1))
            cut = (a + int(np.argmin(rms))) * hop + hop // 2
        yield pos, cut
        pos = cut


BATCH = _positive("QLIPIT_ASR_BATCH", 1)   # pieces decoded together. Measured: no faster on CPU, since
# each piece already uses every core, so one at a time (which also keeps the words exactly as before).


def _pcm(audio, sample_rate=SR):
    if sample_rate != SR or isinstance(sample_rate, bool):
        raise ValueError("English ASR requires a 16000 Hz sample rate.")
    if not isinstance(audio, np.ndarray) or audio.ndim != 1 or audio.dtype.kind != "i" or audio.dtype.itemsize != 2:
        raise ValueError("English ASR requires mono signed int16 PCM, not stereo or pre-normalized floats.")
    return audio


def _input(pcm, benchmark_gain=False):
    seg = pcm.astype(np.float32) / 32768.0
    if benchmark_gain and len(seg):
        rms, peak = float(np.sqrt((seg ** 2).mean())), float(np.max(np.abs(seg)))
        if rms and peak:
            gain = min(4.0, 0.03 / rms, 0.95 / peak)
            if gain > 1:
                seg *= gain
    return seg


def _piece(seg):
    if (not isinstance(seg, np.ndarray) or seg.ndim != 1 or seg.dtype.kind != "f"
            or not 0 < len(seg) <= MAX_PIECE or not np.isfinite(seg).all()
            or np.max(np.abs(seg)) > 1.0):
        raise ValueError("Recognizer pieces must be finite normalized mono float PCM, at most 40 seconds.")
    return np.ascontiguousarray(seg, dtype=np.float32)


def _result(tokens, timestamps, samples):
    if not isinstance(tokens, (list, tuple)) or not isinstance(timestamps, (list, tuple)) or len(tokens) != len(timestamps):
        raise ValueError("Recognizer tokens and timestamps must be matching arrays.")
    if len(tokens) > MAX_TOKENS or any(not isinstance(t, str) or len(t) > 256 for t in tokens):
        raise ValueError("Recognizer tokens are invalid or oversized.")
    times, previous = [], 0.0
    for t in timestamps:
        if isinstance(t, (bool, np.bool_)) or not isinstance(t, (int, float, np.integer, np.floating)):
            raise ValueError("Recognizer timestamps must be finite numeric seconds.")
        value = float(t)
        if not math.isfinite(value) or not previous <= value <= samples / SR:
            raise ValueError("Recognizer timestamps must be ordered and inside their audio piece.")
        times.append(value)
        previous = value
    return list(tokens), times


def _words(tokens, timestamps, a: int, b: int) -> list:
    if (not isinstance(a, (int, np.integer)) or not isinstance(b, (int, np.integer))
            or isinstance(a, bool) or isinstance(b, bool) or not 0 <= a < b):
        raise ValueError("Word timing needs positive ordered sample bounds.")
    tokens, timestamps = _result(tokens, timestamps, b - a)
    out = []
    off, seg_end = a / SR, b / SR
    cur, gap = None, False
    for tok, t in zip(tokens, timestamps):
        new_word = tok.startswith((" ", "▁")) or gap
        piece = tok.replace("▁", " ").strip()
        if not piece:
            gap = True      # a space on its own: whatever comes next starts a new word ("in" "120", not "in120")
            continue
        gap = False
        is_punct = all(not c.isalnum() for c in piece)
        at = off + t
        word_start = max(off, round(at, 2))
        if word_start >= seg_end:
            word_start = at
        word_end = min(seg_end, round(at + 0.12, 2))
        if cur is None or (new_word and not is_punct):
            if word_start >= seg_end:
                continue
            if cur:
                if word_start <= cur["s"]:
                    cur["w"] += " " + piece  # multiple words on one timestamp frame, not invented timings
                    cur["e"] = max(cur["e"], word_end)
                    continue
                cur["e"] = min(cur["e"], word_start)
                if cur["e"] > cur["s"]:
                    out.append(cur)
            cur = {"w": piece, "s": word_start, "e": word_end}
        else:
            cur["w"] += piece
            cur["e"] = max(cur["e"], word_end)
    if cur:
        cur["e"] = min(cur["e"], seg_end)
        if cur["e"] > cur["s"]:
            out.append(cur)
    return out


def decode_pieces(pieces: list, rec=None) -> list:
    """[(tokens, timestamps), ...] for float32 pieces of audio, done on this machine with one copy of the model."""
    streams = []
    try:
        if not isinstance(pieces, list) or not 0 < len(pieces) <= 256:
            raise ValueError("Recognizer input must contain 1 to 256 audio pieces.")
        pieces = [_piece(seg) for seg in pieces]
        rec = rec if rec is not None else _take()
        for seg in pieces:
            st = rec.create_stream()
            st.accept_waveform(SR, seg)
            streams.append(st)
        rec.decode_streams(streams)
        return [_result(list(st.result.tokens), list(st.result.timestamps), len(seg)) for st, seg in zip(streams, pieces)]
    finally:
        if rec is not None:
            _free.put(rec)


def _remote_ok() -> bool:
    return bool(REMOTE) and time.time() >= _remote_down_until


def _decode_remote(pieces: list) -> list:
    """The same, on the machine with the graphics card. Audio goes as 16-bit sound, the answer comes back as text."""
    import httpx
    pieces = [_piece(seg) for seg in pieces]
    raw = [np.clip(seg * 32768.0, -32768, 32767).astype("<i2") for seg in pieces]
    r = httpx.post(REMOTE + "/decode", content=b"".join(x.tobytes() for x in raw), timeout=httpx.Timeout(120, connect=4),
                    headers={"Authorization": "Bearer " + REMOTE_TOKEN, "X-Lens": ",".join(str(len(x)) for x in raw),
                             "X-Sample-Rate": str(SR), "X-Channels": "1", "Content-Type": "application/octet-stream"})
    r.raise_for_status()
    data = r.json()
    got = data.get("pieces") if isinstance(data, dict) else None
    if not isinstance(got, list) or len(got) != len(pieces) or any(not isinstance(p, dict) for p in got):
        raise ValueError("wrong recognizer result shape or number of pieces came back")
    return [_result(p.get("tokens"), p.get("timestamps"), len(seg)) for p, seg in zip(got, pieces)]


def transcribe(audio: np.ndarray, on_progress=None, *, sample_rate=SR, benchmark_gain=False) -> list:
    """Returns [{'w': word, 's': start_sec, 'e': end_sec}, ...] for the whole audio.
    Speech is cut at quiet moments into 12-24 s pieces; digital silence is skipped.
    benchmark_gain is an explicit, OFF-by-default evaluation experiment on input copies only."""
    global _remote_down_until
    audio = _pcm(audio, sample_rate)
    if not isinstance(benchmark_gain, bool):
        raise ValueError("benchmark_gain must be an explicit boolean evaluation opt-in.")
    words, bounds = [], []
    todo = []
    for a, b in _chunks(audio):
        pcm = audio[a:b]
        if len(pcm) < 4800 or not np.any(pcm):
            continue  # no energy-only rejection of quiet speech; gain still remains benchmark-only
        todo.append((a, b))
    remote = _remote_ok()
    size = max(1, REMOTE_BATCH if remote else BATCH)
    groups = [todo[i:i + size] for i in range(0, len(todo), size)]
    done = [0]
    progress_lock = threading.Lock()

    def decode(group):
        global _remote_down_until
        segs = [_input(audio[a:b], benchmark_gain) for a, b in group]
        got = None
        while got is None:
            # whichever is free first: a place on the card's machine, or a copy of the model on this one
            if remote and _remote_ok() and REMOTE_SLOTS.acquire(blocking=False):
                try:
                    got = _decode_remote(segs)
                except Exception:  # noqa: BLE001 - the card's machine is away or busy: these pieces are done here
                    _remote_down_until = time.time() + 60
                finally:
                    REMOTE_SLOTS.release()
                continue
            rec = _take_now()
            if rec is None and not (remote and _remote_ok()):
                rec = _take()                                # nothing else to wait for: wait for a copy here
            if rec is not None:
                got = []
                step = BATCH
                try:
                    for i in range(0, len(segs), step):
                        got += decode_pieces(segs[i:i + step], rec)
                        rec = None if i + step >= len(segs) else _take()
                except Exception:
                    got = None
                    raise
                continue
            time.sleep(0.02)
        if len(got) != len(group):
            raise ValueError("Recognizer returned the wrong number of pieces.")
        out = [(_words(tok, ts, a, b), b / SR) for (a, b), (tok, ts) in zip(group, got)]
        with progress_lock:
            done[0] += 1
            if on_progress:
                on_progress(done[0] / len(groups))
        return out

    # Pieces are independent, so they go to every copy of the model that is free: a video alone on the server
    # uses all of it, and several videos share it piece by piece. The words come back in order.
    workers = REMOTE_PARALLEL if remote else COPIES
    if workers > 1 and len(groups) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(min(workers, len(groups)), thread_name_prefix="asr") as ex:
            results = list(ex.map(decode, groups))
    else:
        results = [decode(g) for g in groups]
    for out in results:
        for piece, bound in out:
            for w in piece:
                if words and words[-1]["w"] == w["w"] and words[-1]["s"] == w["s"]:
                    continue  # the model sometimes says the same word twice at the same moment
                words.append(w)
                bounds.append(bound)
    # A word lasts until the next one starts, capped so pauses stay pauses.
    for i, w in enumerate(words):
        nxt = words[i + 1]["s"] if i + 1 < len(words) else w["e"] + 0.4
        w["e"] = min(bounds[i], len(audio) / SR, nxt,
                     round(max(w["s"] + 0.08, min(nxt, max(w["e"], w["s"] + 0.6))), 2))
    return words
