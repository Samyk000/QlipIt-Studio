"""Job pipeline: get video -> audio -> transcript -> AI picks -> rendered clips. One heavy task at a time."""
import collections
import contextlib
import copy
import hashlib
import json
import os
import math
import queue
import re
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import brain, config, creators, ctx, load, media, render, roman, safe, states, transcribe, usage, voice

class _FairQueue:
    """Whose video is next. Each person has their own line and the lines take turns, so someone who adds ten
    videos does not make everyone else wait behind all ten. Small tasks (re-making one clip) go first."""

    def __init__(self):
        self.cv = threading.Condition()
        self.quick = collections.deque()
        self.lines = {}                       # user -> their waiting videos, oldest first
        self.turn = collections.deque()       # users with something waiting, in the order they will be served
        self.epoch = 0                        # goes up whenever the line moves
        self._pos = (0.0, {})
        self.running = 0                      # videos being worked on right now
        self.last_start = 0.0
        self.held = False                     # videos are waiting only because the machine is near full

    def _room(self) -> bool:
        """May one more video start? One always may. More only while the machine has room, and a few seconds
        after the last one started, so its weight is already showing in the measurements."""
        if self.running == 0:
            return True
        return self.running < config.JOB_WORKERS and time.time() - self.last_start > 6 and not load.busy()

    def done(self):
        with self.cv:
            self.running = max(0, self.running - 1)
            self.cv.notify_all()

    def put(self, item, user=None):
        with self.cv:
            if user is None:
                self.quick.append(item)
            else:
                if user not in self.lines:
                    self.lines[user] = collections.deque()
                    self.turn.append(user)
                self.lines[user].append(item)
            self.epoch += 1
            self.cv.notify()

    def discard_attempt(self, jid, aid):
        with self.cv:
            before = len(self.quick)
            self.quick = collections.deque(item for item in self.quick if not (
                item[0] in ("attempt", "source") and item[1] == jid and item[2] == aid))
            removed = len(self.quick) != before
            if removed:
                self.epoch += 1
                self.cv.notify_all()
            return removed

    def discard_job(self, jid):
        with self.cv:
            self.quick = collections.deque(item for item in self.quick if item[1] != jid)
            for uid, pending in list(self.lines.items()):
                remaining = collections.deque(item for item in pending if item[1] != jid)
                if remaining:
                    self.lines[uid] = remaining
                else:
                    self.lines.pop(uid)
            self.turn = collections.deque(uid for uid in self.turn if uid in self.lines)
            self._pos = (0.0, {})
            self.epoch += 1
            self.cv.notify_all()

    def get(self, quick_only=False):
        with self.cv:
            while True:
                if self.quick:
                    self.epoch += 1
                    return self.quick.popleft()
                if not quick_only and self.turn:
                    if self._room():
                        break
                    if not self.held:
                        self.held = True
                        self.epoch += 1
                elif self.held:
                    self.held = False
                    self.epoch += 1
                self.cv.wait(1.0)             # looked at again every second: the line moves the moment there is room
            self.epoch += 1
            self.held = False
            self.running += 1
            self.last_start = time.time()
            user = self.turn.popleft()
            item = self.lines[user].popleft()
            if self.lines[user]:
                self.turn.append(user)
            else:
                del self.lines[user]
            return item

    def ahead(self) -> dict:
        """job id -> how many videos will start before it. Worked out at most once a second."""
        with self.cv:
            if time.time() - self._pos[0] < 1:
                return self._pos[1]
            out, lines, order, n = {}, {u: list(q) for u, q in self.lines.items()}, list(self.turn), 0
            depth = 0
            while order:
                nxt = []
                for u in order:
                    if depth < len(lines[u]):
                        out[lines[u][depth][1]] = n
                        n += 1
                        if depth + 1 < len(lines[u]):
                            nxt.append(u)
                order, depth = nxt, depth + 1
            self._pos = (time.time(), out)
            return out


_q = _FairQueue()
_live_q: "queue.Queue" = queue.Queue()   # live clips never wait behind a long video
_jobs: dict = {}
_by_user: dict = {}                       # user -> {job id: job}: listing one person's videos never scans everyone's
_lock = threading.RLock()
_controls: dict = {}                       # attempt ID -> irreversible cancellation event; never serialized
_source_cache: dict = {}
_progress_sent: dict = {}
_background_work: dict = collections.defaultdict(int)
_main_work: set = set()
_deferred_slots: set = set()
_job_pools: dict = {}
_render_limit = max(1, int(config.penv("QLIPIT_MAX_RENDERS") or (1 if (os.cpu_count() or 4) < 6 else 2)))
_render_slots = threading.BoundedSemaphore(_render_limit)
ATTEMPT_ACTIVE = ("queued", "running")

DEFAULTS = {"max_clips": 6, "min_len": 15, "max_len": 60, "style": "bounce", "layout": "blur", "quality": 720,
            "range_start": None, "range_end": None}


LIMITS = {"max_clips": (1, 20), "min_len": (5, 120), "max_len": (10, 180), "quality": (360, 1080)}


def _next_clip_id(job) -> str:
    """The next free id, by the highest one ever used, not by how many clips are left. A clip deleted while the
    video is still being cut must not have its id (and so its files) reused by the next clip."""
    with _lock:
        used = [int(c["id"][4:]) for c in job["clips"] if re.fullmatch(r"clip\d+", str(c.get("id", "")))]
        for f in (Path(job["dir"]) / "clips").glob("clip*"):
            match = re.match(r"clip(\d+)(?:\.|-)", f.name)
            if match:
                used.append(int(match[1]))
        n = max(int(job.get("clip_id_high_water", 0)), max(used, default=0)) + 1
        job["clip_id_high_water"] = n
        return f"clip{n:02d}"


def clean_options(options) -> dict:
    """Only known settings, each inside its allowed range. Raises ValueError on nonsense."""
    out = {}
    for k, v in (options if isinstance(options, dict) else {}).items():
        if k not in DEFAULTS or v in ("", None):
            continue
        if k in LIMITS:
            lo, hi = LIMITS[k]
            v = max(lo, min(hi, int(float(v))))
        elif k == "style":
            v = v if v in render.STYLES else DEFAULTS["style"]
        elif k == "layout":
            v = v if v in render.LAYOUTS else DEFAULTS["layout"]
        else:                                    # range_start, range_end: seconds into the video
            v = float(v)
            if not math.isfinite(v) or v < 0:
                raise ValueError("That part of the video isn't valid.")
        out[k] = v
    if out.get("range_end") is not None and out["range_end"] <= out.get("range_start", 0):
        raise ValueError("The end of the chosen part must be after its start.")
    return out


def job_dir(jid) -> Path:
    return Path(_jobs[jid]["dir"])


HIDDEN = ("dir", "user", "source_path", "recover", "stop", "attempts", "editor_sources")
CLIP_HIDDEN = ("src", "fixed_words", "fixed_for", "posts", "voice_asset", "settings", "worker_active", "editor_selection")
ATTEMPT_PUBLIC = ("id", "clip_id", "kind", "status", "revision", "base_revision", "created", "started",
                  "finished", "deadline", "outcome", "cancel_requested", "cleanup_pending", "progress",
                  "file", "cover", "error", "asset_token", "source_key")


def public_attempt(attempt):
    return copy.deepcopy({k: attempt[k] for k in ATTEMPT_PUBLIC if k in attempt})


def has_active_work(job, clip=None):
    attempts = list((job.get("attempts") or {}).values())
    if clip is not None:
        attempts = [a for a in attempts if a.get("clip_id") == clip["id"]]
        return clip.get("status") in ("queued", "rendering") or any(
            a.get("status") in ATTEMPT_ACTIVE or a.get("cleanup_pending") for a in attempts)
    return job.get("status") in ("queued", "running", "live") or bool(job.get("cleanup_pending")) or any(
        a.get("status") in ATTEMPT_ACTIVE or a.get("cleanup_pending") for a in attempts)


def _counts(job):
    out = dict(ready=0, queued=0, working=0, failed=0, cancelled=0)
    for clip in job.get("clips", []):
        if clip.get("status") == "done" and clip.get("file"):
            out["ready"] += 1
        elif clip.get("status") == "error":
            out["cancelled" if clip.get("outcome") == "cancelled" else "failed"] += 1
        elif clip.get("status") == "queued":
            out["queued"] += 1
        elif clip.get("status") == "rendering":
            out["working"] += 1
    for clip in job.get("clips", []):
        a = clip.get("attempt") or {}
        if clip.get("status") == "done":
            if a.get("status") == "queued":
                out["queued"] += 1
            elif a.get("status") == "running":
                out["working"] += 1
            elif a.get("status") == "error":
                out["failed"] += 1
            elif a.get("status") == "cancelled":
                out["cancelled"] += 1
        source = clip.get("source_attempt") or {}
        if source.get("status") == "queued":
            out["queued"] += 1
        elif source.get("status") == "running":
            out["working"] += 1
    return out


def public(job) -> dict:
    """What the browser may see: no server paths, no owner id."""
    with _lock:
        out = copy.deepcopy({k: v for k, v in job.items() if k not in HIDDEN and k != "clips"})
    out["source"] = {k: v for k, v in job["source"].items() if k != "path"}
    out["clips"] = []
    for c in job["clips"]:
        item = copy.deepcopy({k: v for k, v in c.items() if k not in CLIP_HIDDEN and k not in ("attempt", "source_attempt")})
        for key in ("attempt", "source_attempt"):
            if c.get(key):
                item[key] = public_attempt(c[key])
        item["active_work"] = has_active_work(job, c)
        out["clips"].append(item)
    out.update(active_work=has_active_work(job), counts=_counts(job))
    ai = job.get("ai") or {}
    out["ai"] = {"tokens": ai.get("tokens", 0), "note": ai.get("note", ""), "used": bool(ai.get("model"))}   # never the model, never raw errors
    if job.get("status") == "queued":
        n = _q.ahead().get(job["id"])
        if n:
            out["stage"] = (f"High demand right now: {n} video{'s' if n != 1 else ''} ahead of yours" if _q.held else
                            f"Waiting in line: {n} video{'s' if n != 1 else ''} ahead of yours")
        elif n == 0 and _q.held:
            out["stage"] = "High demand right now: yours is next"
    return out


def save(job):
    ctx.bump(job.get("user"))
    with _lock:
        d = Path(job["dir"])
        for _ in range(5):
            try:
                text = json.dumps(job, indent=1)
                break
            except RuntimeError:  # changed mid-dump by another thread
                time.sleep(0.05)
        else:
            return
        tmp = d / "job.json.tmp"
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(d / "job.json")


def _set(job, **kw):
    with _lock:
        job.update(kw)
    save(job)


def report_progress(job, attempt=None, *, phase, completed=None, total=None, unit=None, phase_attempt=1, **unused):
    """Live measurements are cheap; only phase checkpoints and terminal states reach disk."""
    now = time.time()
    owner = attempt if attempt is not None else job
    key = (job["id"], owner.get("id"), phase, phase_attempt)
    identity = attempt["id"] if attempt is not None else job.get("processing_attempt", job["id"])
    with _lock:
        old = owner.get("progress" if attempt is not None else "progress_detail") or {}
        changed = old.get("phase") != phase or old.get("phase_attempt") != phase_attempt
        if completed is not None:
            completed = max(0.0, float(completed))
            if not math.isfinite(completed):
                completed = None
        if total is not None:
            total = float(total)
            if not math.isfinite(total) or total <= 0:
                total = None
        if not changed and completed is not None and old.get("completed") is not None:
            completed = max(completed, old["completed"])
        if total is not None and completed is not None:
            completed = min(completed, total)
        advancing = changed or (completed is not None and completed != old.get("completed"))
        detail = {"id": f"{identity}:{phase}:{phase_attempt}", "phase": phase,
                  "phase_attempt": phase_attempt, "completed": completed, "total": total, "unit": unit,
                  "phase_start": now if changed else old.get("phase_start", now),
                  "last_advance": now if advancing else old.get("last_advance", now), "heartbeat": now}
        owner["progress" if attempt is not None else "progress_detail"] = detail
        if attempt is None:
            job["stage"] = phase
        last = _progress_sent.get(key, 0)
        publish = changed or now - last >= 0.4
        if publish:
            _progress_sent[key] = now
    if changed:
        save(job)
    elif publish:
        ctx.bump(job.get("user"))


def _remap(path, folder: Path):
    """Paths saved before data moved into per-user folders (or on another machine): find the file by name."""
    if path and not Path(path).exists():
        cand = folder / Path(str(path).replace("\\", "/")).name
        if cand.exists():
            return str(cand)
    return path


def _load_file(f: Path, uid: str):
    try:
        job = json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    job.update(dir=str(f.parent), user=uid)
    job.setdefault("kind", "video")
    job.setdefault("creator", None)
    if job.get("source_path"):
        job["source_path"] = _remap(job["source_path"], f.parent)
    if job.get("source", {}).get("path"):
        job["source"]["path"] = _remap(job["source"]["path"], f.parent)
    if job.get("status") in (states.JobStatus.QUEUED, states.JobStatus.RUNNING) and job.get("kind") != "live":
        # Picked up again as soon as the server is back (jobs.start), from the last finished part of the transcript.
        job.update(status=states.JobStatus.QUEUED, stage="Picking up where it stopped", error=None, resume=True)
    if job.get("status") == "live":
        job.update(status=states.JobStatus.DONE, stage="Recording paused by a server update", was_live=True)
    buffered = job.get("kind") == "live" and any((f.parent / "buffer").glob("*.ts"))
    for attempt in (job.get("attempts") or {}).values():
        if attempt.get("status") in ATTEMPT_ACTIVE:
            attempt.update(status="error", outcome="failed",
                           finished=time.time(), error="Interrupted by a restart. The last saved clip is unchanged.")
        attempt.update(worker_active=False, cleanup_pending=False)
        _controls.pop(attempt.get("id"), None)
    for entry in (job.get("editor_sources") or {}).values():
        if entry.get("status") in ATTEMPT_ACTIVE:
            entry.update(status="error", error="Clean source preparation was interrupted. Retry it; the saved clip is unchanged.")
    job["cleanup_pending"] = False
    for c in job.get("clips", []):
        c.pop("posts", None)
        if c.get("src"):
            c["src"] = _remap(c["src"], f.parent / "clips")
        for key in ("attempt", "source_attempt"):
            if c.get(key) and c[key].get("id") in (job.get("attempts") or {}):
                c[key] = job["attempts"][c[key]["id"]]
        if c.get("status") in (states.ClipStatus.RENDERING, states.ClipStatus.QUEUED):
            if buffered and not c.get("src"):
                c["status"] = states.ClipStatus.QUEUED  # the stream is still on disk: live.recover() finishes these
                job["recover"] = True
            elif job.get("resume"):
                c["status"] = states.ClipStatus.QUEUED  # the video is being picked up again: this clip is made then
            elif job.get("kind") != "live" and (f.parent / "words.json").is_file():
                c["status"] = states.ClipStatus.QUEUED  # a single clip being re-made: jobs.start() makes it again
                job.setdefault("remake", []).append(c["id"])
            else:
                c.update(status=states.ClipStatus.ERROR, error="Interrupted by a restart. Open it and press Save and re-make.")
    _jobs[job["id"]] = job
    _by_user.setdefault(uid, {})[job["id"]] = job


def load_user(uid: str):
    d = config.USERS_DIR / uid
    for f in list((d / "jobs").glob("*/job.json")) + list((d / "creators").glob("*/*/*/job.json")):
        _load_file(f, uid)


def load_all():
    for d in config.USERS_DIR.iterdir():
        if d.is_dir():
            load_user(d.name)


def all_jobs() -> list:
    """Every user's jobs. Only for background work; never send this to a browser."""
    with _lock:
        return list(_jobs.values())


def list_jobs():
    u = ctx.uid()
    with _lock:
        return sorted((dict(j) for j in _by_user.get(u, {}).values()), key=lambda j: -j["created"])


def get(jid):
    """The job, only if it belongs to whoever this code is running for."""
    job = _jobs.get(jid)
    return job if job and job["user"] == ctx.uid() else None


def busy_count() -> int:
    u = ctx.uid()
    return sum(1 for j in list(_by_user.get(u, {}).values()) if has_active_work(j))


def waiting_total() -> int:
    return sum(1 for j in list(_jobs.values()) if j["kind"] == "video" and j["status"] == "queued")


def queue_epoch() -> int:
    return _q.epoch


def running_now() -> int:
    return _q.running


def create(source: dict, options: dict, enqueue=True, creator=None, kind="video") -> dict:
    jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:10]
    c = creators.get(creator) if creator else None
    if c:
        d = creators.root(c["id"]) / ("live" if kind == "live" else "videos") / jid
        base = {**DEFAULTS, **{k: c[k] for k in ("max_clips", "min_len", "max_len", "style", "layout")}}
    else:
        d, base, creator = config.user_dir(ctx.uid()) / "jobs" / jid, dict(DEFAULTS), None
    d.mkdir(parents=True)
    opts = {**base, **clean_options(options)}
    if opts["min_len"] >= opts["max_len"]:
        opts["min_len"] = max(5, opts["max_len"] - 5)
    job = {"id": jid, "user": ctx.uid(), "kind": kind, "creator": creator, "dir": str(d), "created": time.time(), "source": source,
           "options": opts, "status": "queued", "stage": "Waiting", "progress": 0.0,
           "title": source.get("name", ""), "uploader": "", "duration": 0, "clips": [], "error": None, "ai": {},
           "processing_attempt": uuid.uuid4().hex, "outcome": None, "cancel_requested": False, "cleanup_pending": False,
           "clip_id_high_water": 0}
    with _lock:
        _jobs[jid] = job
        _by_user.setdefault(job["user"], {})[jid] = job
    save(job)
    if enqueue:
        _q.put(("job", jid, None), user=job["user"])
    return job


def enqueue(job):
    save(job)
    _q.put(("job", job["id"], None), user=job["user"])


def stop(jid) -> bool:
    """Stop a video that is waiting or being made. Clips already finished are kept."""
    job = get(jid)
    if not job or not has_active_work(job):
        return False
    processing = job.get("status") in ("queued", "running")
    if processing:
        job.update(stop=True, cancel_requested=True, outcome="cancelled")
        _q.discard_job(jid)
    media.kill_job_procs(jid)
    for attempt in list((job.get("attempts") or {}).values()):
        if attempt.get("status") in ATTEMPT_ACTIVE:
            cancel_attempt(jid, attempt["clip_id"], attempt["id"])
    for c in job.get("clips", []):
        if c.get("status") in (states.ClipStatus.QUEUED, states.ClipStatus.RENDERING):
            states.transition_clip_status(c, states.ClipStatus.ERROR)
            c.update(error="Stopped by you.", outcome="cancelled")
    if processing:
        states.transition_job_status(job, states.JobStatus.DONE)
        job.update(stage="Stopped by you", progress=0.0, error=None)
    save(job)
    return True


class Stopped(RuntimeError):
    """The person who started this video pressed stop."""


class AttemptConflict(RuntimeError):
    """The editor's successful revision changed, or this clip is already working."""


def submit(fn):
    """Run any heavy task (a render, a cleanup) on the single worker, in order."""
    _q.put(("fn", ctx.bind(fn), None))


def submit_live(fn):
    """Live-stream renders and cleanups: their own worker, so a long video can never hold them up."""
    _live_q.put(ctx.bind(fn))


def _live_worker():
    while True:
        fn = _live_q.get()
        try:
            fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def delete(jid, force=False):
    with _lock:
        if not force and _jobs.get(jid) and has_active_work(_jobs[jid]):
            return False
        job = _jobs.pop(jid, None)
        if job:
            _by_user.get(job["user"], {}).pop(jid, None)
            _q.discard_job(jid)
            for aid in (job.get("attempts") or {}):
                _controls.pop(aid, None)
    if job:
        ctx.bump(job["user"])
        shutil.rmtree(job["dir"], ignore_errors=True)
    return bool(job)


def resolve_clip_source(job, clip, cancel=None):
    """One owned, display-oriented source and global/local time mapping, never the exported MP4."""
    if job.get("user") != ctx.uid() or _jobs.get(job.get("id")) is not job:
        raise PermissionError("This video does not belong to the current user.")
    if cancel:
        cancel()
    path = Path(clip.get("src") or job.get("source_path") or (job.get("source") or {}).get("path") or "")
    folder = Path(job["dir"]).resolve()
    if not path.is_file() or not path.resolve().is_relative_to(folder):
        raise ValueError("The original source is unavailable. Your saved clip can still be played or downloaded. Restore the original source before editing.")
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    with _lock:
        meta = _source_cache.get(key)
    if meta is None:
        meta = media.probe(path)
        with _lock:
            if len(_source_cache) >= 128:
                _source_cache.pop(next(iter(_source_cache)))
            _source_cache[key] = dict(meta)
    duration = float(meta["duration"])
    offset = float(clip.get("src_offset", 0)) if clip.get("src") else 0.0
    end = offset + duration
    if clip.get("src") and clip.get("src_end") is not None:
        end = min(end, float(clip["src_end"]))
    if job.get("duration"):
        end = min(end, float(job["duration"]))
    if not all(math.isfinite(v) for v in (duration, offset, end)) or duration <= 0 or offset < 0 or end <= offset:
        raise ValueError("The retained source interval isn't valid.")
    if cancel:
        cancel()
    return {"path": path.resolve(), "meta": dict(meta), "offset": offset, "start": offset, "end": end,
            "identity": hashlib.sha256(repr(key).encode()).hexdigest()}


def _clip_settings(clip):
    return copy.deepcopy({k: v for k, v in clip.items() if k not in (
        "attempt", "source_attempt", "active_work", "cleanup_pending", "file", "cover", "version",
        "artifact_revision", "attempt_revision", "published_attempt", "voice_asset", "outcome", "error", "status", "editor_selection")})


def _validate_edit(job, clip, changes, source):
    if not isinstance(changes, dict):
        raise ValueError("Clip settings must be an object.")
    new = _clip_settings(clip)
    for key in ("start", "end"):
        value = changes.get(key, new.get(key))
        if isinstance(value, bool):
            raise ValueError("Those times aren't valid.")
        new[key] = round(float(value), 2)
    if not all(math.isfinite(new[k]) for k in ("start", "end")):
        raise ValueError("Those times aren't valid.")
    if new["start"] < source["start"] - 0.001 or new["end"] > source["end"] + 0.001:
        raise ValueError(f"The clip must stay within the available source ({source['start']:.2f} to {source['end']:.2f} seconds).")
    if not 3 <= new["end"] - new["start"] <= 180:
        raise ValueError("A clip can be 3 to 180 seconds long.")
    for key, limit in (("hook", 60), ("title", 100), ("description", 2000)):
        if key in changes:
            value = changes[key]
            if not isinstance(value, str):
                raise ValueError(f"The {key} must be text.")
            if value != clip.get(key) and len(value) > limit:
                raise ValueError(f"The {key} can have at most {limit} characters.")
            new[key] = value
    for key, allowed in (("style", render.STYLES), ("layout", render.LAYOUTS)):
        value = changes.get(key, new.get(key, DEFAULTS[key]))
        if value not in allowed:
            raise ValueError(f"That {key} is not supported.")
        new[key] = value
    if "visual" in changes:
        new["visual"] = render.validate_visual(changes["visual"], clip.get("visual"))
    if "voiceover" in changes:
        value = changes["voiceover"]
        if value is None:
            new["voiceover"] = None
        elif isinstance(value, dict):
            vo = {"enabled": False, "script": "", "voice": voice.DEFAULT_VOICE, "mode": "mute", "rate": "+0%",
                  **(clip.get("voiceover") or {}), **value}
            if not isinstance(vo["enabled"], bool) or not isinstance(vo["script"], str) or len(vo["script"]) > 2000:
                raise ValueError("The narration settings aren't valid.")
            if vo["enabled"] and not vo["script"].strip():
                raise ValueError("A narration script is required.")
            if vo["mode"] not in ("mute", "duck") or vo["voice"] not in voice.VOICE_MAP or not re.fullmatch(r"[+-](?:\d{1,2}|100)%", str(vo["rate"])):
                raise ValueError("That narration voice, mode or rate is not supported.")
            new["voiceover"] = {k: vo[k] for k in ("enabled", "script", "voice", "mode", "rate")}
        else:
            raise ValueError("Narration settings must be an object or null.")
    return new


def _reserve_attempt(job, clip, settings, kind="render"):
    with _lock:
        revision = max(int(clip.get("attempt_revision", 0)), int(clip.get("artifact_revision", clip.get("version", 0)))) + 1
        clip["attempt_revision"] = revision
        aid = uuid.uuid4().hex
        attempt = {"id": aid, "clip_id": clip["id"], "kind": kind, "status": "queued", "revision": revision,
                   "base_revision": int(clip.get("artifact_revision", clip.get("version", 0))),
                   "created": time.time(), "settings": copy.deepcopy(settings), "outcome": None,
                   "cancel_requested": False, "cleanup_pending": False, "worker_active": False}
        job.setdefault("attempts", {})[aid] = attempt
        clip["source_attempt" if kind == "source" else "attempt"] = attempt
        _controls[aid] = threading.Event()
    return attempt


def request_render(jid, cid, changes: dict, preview=False):
    job = get(jid)
    if not job:
        raise PermissionError("This video does not belong to the current user.")
    clip = next((c for c in job["clips"] if c["id"] == cid), None)
    if not clip:
        raise ValueError("No such clip.")
    source = resolve_clip_source(job, clip)
    new = _validate_edit(job, clip, changes, source)
    with _lock:
        if _jobs.get(jid) is not job or clip not in job["clips"]:
            raise AttemptConflict("This clip is no longer available.")
        if job.get("cleanup_pending"):
            raise AttemptConflict("The previous video work is still cleaning up. Wait for it to finish before editing.")
        revision = int(clip.get("artifact_revision", clip.get("version", 0)))
        expected = changes.get("expected_revision", revision)
        if isinstance(expected, bool) or not isinstance(expected, (int, float)) or expected != revision:
            raise AttemptConflict("The saved clip changed. Refresh it before applying these edits.")
        if job.get("status") in ("queued", "running") or clip.get("status") in ("queued", "rendering") or any(
                a.get("clip_id") == cid and a.get("kind") != "source" and
                (a.get("status") in ATTEMPT_ACTIVE or a.get("cleanup_pending")) for a in (job.get("attempts") or {}).values()):
            raise AttemptConflict("This clip is still being made. Wait for it to finish, or cancel the current attempt.")
        _prune_editor(job)
        _editor_capacity(job)
        attempt = _reserve_attempt(job, clip, new, "preview" if preview else "render")
        attempt["editor"] = True
        try:
            save(job)
        except Exception:
            attempt.update(status="error", outcome="failed", error="Could not save the render request.", finished=time.time())
            _controls.pop(attempt["id"], None)
            raise
        _q.put(("attempt", jid, attempt["id"]))
        return {"attempt": public_attempt(attempt), "job": public(job)}


def get_attempt(jid, cid, aid):
    job = get(jid)
    attempt = (job.get("attempts") or {}).get(aid) if job else None
    return attempt if attempt and attempt.get("clip_id") == cid else None


def cancel_attempt(jid, cid, aid):
    attempt = get_attempt(jid, cid, aid)
    if not attempt:
        return False
    job = get(jid)
    with _lock:
        if attempt.get("status") not in ATTEMPT_ACTIVE:
            return False
        _controls.setdefault(aid, threading.Event()).set()
        attempt.update(cancel_requested=True, status="cancelled", outcome="cancelled", finished=time.time(),
                       cleanup_pending=bool(attempt.get("worker_active")), error="Stopped by you.")
        if attempt["kind"] == "source":
            entry = (job.get("editor_sources") or {}).get(attempt.get("asset_token"))
            if entry:
                entry.update(status="error", error="Source preparation was cancelled.")
        clip = next((c for c in job["clips"] if c["id"] == cid), None)
        if clip and clip.get("status") in ("queued", "rendering"):
            clip.update(status="error", outcome="cancelled", error="Stopped by you.")
        _q.discard_attempt(jid, aid)
        if not attempt.get("worker_active"):
            _controls.pop(aid, None)
    media.kill_job_procs(f"{jid}:{aid}")
    save(job)
    return True


def _attempt_check(job, clip, attempt):
    event = _controls.setdefault(attempt["id"], threading.Event())
    parent_stopped = (job.get("stop") or job.get("cancel_requested")) and not attempt.get("editor")
    if parent_stopped or attempt.get("cancel_requested") or event.is_set():
        event.set()
        raise Stopped("Stopped by you.")
    if attempt.get("deadline") and time.time() >= attempt["deadline"]:
        event.set()
        attempt["cancel_requested"] = True
        media.kill_job_procs(f"{job['id']}:{attempt['id']}")
        raise TimeoutError("This clip took too long and was stopped. Open it and press Save and re-make to try again.")
    key = "source_attempt" if attempt["kind"] == "source" else "attempt"
    if _jobs.get(job["id"]) is not job or clip not in job["clips"] or (clip.get(key) or {}).get("id") != attempt["id"]:
        event.set()
        attempt.update(status="superseded", outcome="cancelled", cancel_requested=True)
        raise Stopped("This attempt was superseded. The saved clip is unchanged.")


def _finish_attempt(job, clip, attempt, error=None):
    if error is not None:
        if attempt.get("status") not in ("error", "cancelled", "superseded"):
            attempt.update(status="cancelled" if isinstance(error, Stopped) else "error",
                           outcome="cancelled" if isinstance(error, Stopped) else "failed")
        attempt["error"] = attempt.get("error") or safe.friendly(error, 400)
        if clip.get("status") != "done" and (clip.get("attempt") or {}).get("id") == attempt["id"] and attempt["kind"] != "source":
            clip.update(status="error", error=attempt["error"], outcome=attempt["outcome"])
    attempt.update(worker_active=False, cleanup_pending=False, finished=time.time())
    _controls.pop(attempt["id"], None)
    for key in list(_progress_sent):
        if key[0] == job["id"] and key[1] == attempt["id"]:
            _progress_sent.pop(key, None)
    try:
        save(job)
    except Exception:
        ctx.bump(job.get("user"))             # disk failure must not hide a terminal in-memory result or kill workers


def _successful_voice_asset(job, clip, settings):
    vo = clip.get("voiceover") or {}
    new = settings.get("voiceover") or {}
    if not vo.get("enabled") or not new.get("enabled") or render.voice_fingerprint(vo) != render.voice_fingerprint(new):
        return None
    asset = clip.get("voice_asset") or {}
    stem = Path(clip.get("file") or f"{clip['id']}.mp4").stem
    path = Path(job["dir"]) / "clips" / Path(asset.get("file") or f"{stem}_vo.mp3").name
    if not path.is_file():
        raise ValueError("The saved narration audio is unavailable. Restore it before making visual-only edits; it will not be silently re-synthesized.")
    return {"path": path, "ass": path.with_name(f"{stem}.ass"), "words": copy.deepcopy(asset.get("words") or []),
            "fingerprint": asset.get("fingerprint") or render.voice_fingerprint(vo)}


@contextlib.contextmanager
def _render_slot(check):
    while not _render_slots.acquire(timeout=0.2):
        check()
    try:
        check()
        yield
    finally:
        _render_slots.release()


def render_one(job, clip, words=None, attempt=None):
    """Stage one immutable revision and publish only while its attempt is still current."""
    attempt = attempt or _reserve_attempt(job, clip, _clip_settings(clip))
    name = f"{clip['id']}-{'p' if attempt['kind'] == 'preview' else 'r'}{attempt['revision']:06d}-{attempt['id'][:8]}"
    folder = Path(job["dir"]) / "clips"
    published = False
    error = None
    try:
        _attempt_check(job, clip, attempt)
        attempt.update(status="running", started=attempt.get("started") or time.time(),
                       deadline=attempt.get("deadline") or time.time() + CLIP_MINUTES * 60,
                       worker_active=True, cleanup_pending=True)
        if clip.get("status") != "done" and attempt["kind"] == "render":
            clip["status"] = "rendering"
        save(job)
        check = lambda: _attempt_check(job, clip, attempt)
        progress = lambda **kw: report_progress(job, attempt, **kw)
        progress(phase="Preparing source")
        settings = copy.deepcopy(attempt["settings"])
        for key in ("src", "src_offset", "src_end"):
            if key in clip:
                settings[key] = clip[key]
        source = resolve_clip_source(job, settings, check)
        settings = _validate_edit(job, settings, {}, source)
        if words is None:
            progress(phase="Reading saved transcript")
            words = json.loads((Path(job["dir"]) / "words.json").read_text(encoding="utf-8"))
        if not isinstance(words, list) or any(not isinstance(w, dict) or not all(k in w for k in ("w", "s", "e")) or
                not all(isinstance(w[k], (int, float)) and math.isfinite(w[k]) for k in ("s", "e")) for w in words):
            raise ValueError("The saved transcript is corrupt. Restore it or process the original video again.")
        check()
        span = [settings["start"], settings["end"], "en2"]
        if settings.get("fixed_for") == span and settings.get("fixed_words") and not any(
                roman.needs_roman(w.get("w", "")) for w in settings["fixed_words"]):
            raw = settings["fixed_words"]
        else:
            raw = [w for w in words if settings["start"] - 1 <= w["s"] <= settings["end"] + 1]
            progress(phase="Preparing captions")
            try:
                usage.set_context(job.get("creator"), job.get("title") or "")
                raw = brain.english_captions(raw, job.get("lang") or "", job.get("title") or "",
                                           youtube_english=lambda: _english_part(job, settings))
            except Exception:
                check()
            settings.update(fixed_words=raw, fixed_for=span)
        check()
        asset = _successful_voice_asset(job, clip, settings)
        off = source["offset"]
        local = dict(settings, start=settings["start"] - off, end=settings["end"] - off,
                     hook=roman.to_roman(settings.get("hook") or ""))
        near = [dict(w, s=w["s"] - off, e=w["e"] - off) for w in roman.words(raw)]
        progress(phase="Waiting for render slot")
        with _render_slot(check):
            out = render.render_clip(source["path"], folder, name, local, near,
                                     source["meta"]["width"], source["meta"]["height"],
                                     jid=f"{job['id']}:{attempt['id']}", source_meta=source["meta"],
                                     on_progress=progress, cancel=check, voice_asset=asset)
        check()
        progress(phase="Validating and publishing")
        for key in ("file", "cover"):
            value = out.get(key)
            if not value or Path(value).name != value or not (folder / value).is_file() or not (folder / value).stat().st_size:
                raise RuntimeError("The render did not produce a valid MP4 and cover. The saved clip is unchanged.")
        with _lock:
            check()
            before = copy.deepcopy(clip)
            if attempt["kind"] == "render":
                # Render geometry reports the effective mode; the editor keeps the requested legacy recipe.
                artifacts = {k: v for k, v in out.items() if k != "layout"}
                clip.update(settings, **artifacts, version=attempt["revision"], artifact_revision=attempt["revision"],
                            published_attempt=attempt["id"], status="done", outcome="success", error=None)
                clip.setdefault("made_at", time.time())
            attempt.update(status="done", outcome="success", file=out["file"], cover=out["cover"])
            try:
                save(job)
            except Exception:
                clip.clear()
                clip.update(before)
                clip["attempt"] = attempt
                raise
            published = True
        if attempt["kind"] == "render":
            try:
                post = {k: clip.get(k) for k in ("title", "description", "hashtags", "hook", "start", "end", "score")}
                (folder / f"{name}.json").write_text(json.dumps(post, indent=1), encoding="utf-8")
                creators.log(job.get("creator"), f"Clip ready: {clip.get('title') or clip.get('hook') or clip['id']}")
            except Exception:
                pass
    except Exception as exc:
        error = exc
    finally:
        try:
            media.kill_job_procs(f"{job['id']}:{attempt['id']}")
            if not published:
                for f in folder.glob(name + ".*"):
                    f.unlink(missing_ok=True)
                (folder / f"{name}_vo.mp3").unlink(missing_ok=True)
        except Exception:
            pass
        finally:
            _finish_attempt(job, clip, attempt, error)


def _editor_capacity(job):
    active = [a for j in _jobs.values() for a in (j.get("attempts") or {}).values()
              if a.get("editor") and (a.get("status") in ATTEMPT_ACTIVE or a.get("cleanup_pending"))]
    if len(active) >= 64 or sum(1 for j in _by_user.get(job["user"], {}).values()
                               for a in (j.get("attempts") or {}).values() if a in active) >= 4:
        raise AttemptConflict("The editor work queue is full. Wait for an existing preview or re-make to finish.")


def _prune_editor(job):
    """Only disposable previews/proxies expire; successful exports and originals are never pruned here."""
    now = time.time()
    protected = {(c.get(key) or {}).get("id") for c in job["clips"] for key in ("attempt", "source_attempt")}
    protected.update(c.get("published_attempt") for c in job["clips"])
    attempts = job.get("attempts") or {}
    for aid, attempt in list(attempts.items()):
        if aid in protected or attempt.get("status") in ATTEMPT_ACTIVE or attempt.get("cleanup_pending"):
            continue
        if now - attempt.get("finished", now) < 3600:
            continue
        if attempt.get("kind") == "preview" and attempt.get("file"):
            stem = Path(attempt["file"]).stem
            for suffix in (".mp4", ".jpg", ".ass", "_vo.mp3"):
                (Path(job["dir"]) / "clips" / (stem + suffix)).unlink(missing_ok=True)
        attempts.pop(aid, None)
    sources = job.get("editor_sources") or {}
    tokens = {(c.get("source_attempt") or {}).get("asset_token") for c in job["clips"]}
    tokens.update((c.get("editor_selection") or {}).get("key") for c in job["clips"])
    for token, entry in list(sources.items()):
        if token in tokens or entry.get("status") in ATTEMPT_ACTIVE or now - entry.get("used_at", now) < 3600:
            continue
        path = Path(entry.get("path") or "")
        if path.is_file() and path.parent.resolve() == (Path(job["dir"]) / "editor").resolve():
            path.unlink(missing_ok=True)
        sources.pop(token, None)


def editor_info(job, clip, changes=None, prepare=False):
    source = resolve_clip_source(job, clip)
    if changes is None and clip.get("editor_selection"):
        changes = clip["editor_selection"].get("settings")
    elif changes is None and clip.get("source_attempt"):
        changes = clip["source_attempt"].get("settings")
    settings = _validate_edit(job, clip, changes or {}, source)
    geometry = render.resolve_visual(settings, source["meta"])
    direct = source["path"].suffix.lower() == ".mp4" and source["meta"].get("codec") == "h264" and source["meta"].get("pix_fmt") in ("yuv420p", "yuvj420p")
    start = source["start"] if direct else max(source["start"], settings["start"] - PAD)
    end = source["end"] if direct else min(source["end"], settings["end"] + PAD)
    key = hashlib.sha256(f"{source['identity']}:{start:.2f}:{end:.2f}:clean-v1".encode()).hexdigest()[:32]
    with _lock:
        if _jobs.get(job["id"]) is not job or job.get("user") != ctx.uid() or clip not in job["clips"]:
            raise AttemptConflict("This clip is no longer available.")
        if prepare and (clip.get("source_attempt") or {}).get("cleanup_pending"):
            raise AttemptConflict("The previous source preparation is still cleaning up. Wait before retrying.")
        created_attempt = None
        cache = job.setdefault("editor_sources", {})
        if prepare:
            _prune_editor(job)
        entry = cache.get(key)
        if entry and entry.get("status") == "error" and prepare:
            old_path = Path(entry.get("path") or "")
            if old_path.is_file() and old_path.parent.resolve() == (Path(job["dir"]) / "editor").resolve():
                old_path.unlink(missing_ok=True)
            cache.pop(key)
            entry = None
        if entry and entry.get("status") == "ready" and not Path(entry.get("path", "")).is_file():
            cache.pop(key)
            entry = None
        if not entry and direct:
            entry = {"status": "ready", "path": str(source["path"]), "offset": source["offset"],
                     "start": start, "end": end, "width": source["meta"]["width"], "height": source["meta"]["height"]}
            cache[key] = entry
        if not entry and prepare:
            if job.get("cleanup_pending"):
                raise AttemptConflict("The previous video work is still cleaning up. Wait before preparing another editing source.")
            _editor_capacity(job)
            if len(cache) >= 32:
                raise AttemptConflict("This video's temporary source cache is full. Reuse an existing interval or wait for older previews to expire.")
            previous = clip.get("source_attempt")
            if previous and previous.get("status") in ATTEMPT_ACTIVE:
                cancel_attempt(job["id"], clip["id"], previous["id"])
            attempt = _reserve_attempt(job, clip, settings, "source")
            attempt.update(editor=True, asset_token=key)
            entry = {"status": "queued", "attempt_id": attempt["id"], "offset": start, "start": start, "end": end,
                     "width": source["meta"]["width"], "height": source["meta"]["height"],
                     "path": str(Path(job["dir"]) / "editor" / f"{key}-{attempt['id'][:12]}.mp4")}
            cache[key] = entry
            created_attempt = attempt
        if entry:
            entry["used_at"] = time.time()
        if prepare and entry:
            selected = (job.get("attempts") or {}).get(entry.get("attempt_id"))
            previous = clip.get("source_attempt")
            if previous and previous.get("id") != entry.get("attempt_id") and previous.get("status") in ATTEMPT_ACTIVE:
                cancel_attempt(job["id"], clip["id"], previous["id"])
            if selected and selected.get("clip_id") == clip["id"]:
                clip["source_attempt"] = selected
            else:
                clip.pop("source_attempt", None)
            clip["editor_selection"] = {"key": key, "settings": copy.deepcopy(settings)}
            try:
                save(job)
            except Exception as exc:
                if created_attempt:
                    entry.update(status="error", error="Could not save the source preparation request.")
                    _finish_attempt(job, clip, created_attempt, exc)
                raise
            if created_attempt:
                _q.put(("source", job["id"], created_attempt["id"]))
        entry = entry or {"status": "queued", "start": start, "end": end, "offset": start,
                          "width": source["meta"]["width"], "height": source["meta"]["height"]}
        visible = {k: v for k, v in entry.items() if k != "path"}
        owner = (job.get("attempts") or {}).get(entry.get("attempt_id"))
        if entry.get("attempt_id") and (not owner or owner.get("clip_id") != clip["id"]):
            visible.pop("attempt_id", None)
            visible["shared"] = True
        visible.update(url=f"/editor-media/{job['id']}/{key}" if entry["status"] == "ready" else None, muted=True)
    return {"source": visible, "visual": geometry, "catalog": render.visual_catalog(),
            "revision": int(clip.get("artifact_revision", clip.get("version", 0)))}


def _prepare_editor_source(job, clip, attempt):
    token = attempt["asset_token"]
    entry = (job.get("editor_sources") or {}).get(token) or {}
    if entry.get("attempt_id") != attempt["id"]:
        entry = {}                         # never mutate a newer request's record
    path = Path(job["dir"]) / "editor" / f"{token}-{attempt['id'][:12]}.mp4"
    work = None
    error = None
    success = False
    try:
        _attempt_check(job, clip, attempt)
        if not entry:
            raise Stopped("This source preparation was superseded.")
        attempt.update(status="running", worker_active=True, cleanup_pending=True, started=time.time(),
                       deadline=time.time() + CLIP_MINUTES * 60)
        entry["status"] = "running"
        save(job)
        check = lambda: _attempt_check(job, clip, attempt)
        progress = lambda **kw: report_progress(job, attempt, **kw)
        progress(phase="Preparing clean editing source")
        source = resolve_clip_source(job, attempt["settings"], check)
        import tempfile
        work = Path(tempfile.mkdtemp(prefix="pkedit"))
        duration = entry["end"] - entry["start"]
        local = entry["start"] - source["offset"]
        width, height = (max(2, int(source["meta"][k]) // 2 * 2) for k in ("width", "height"))
        with _render_slot(check):
            media.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{local:.2f}", "-i", str(source["path"]),
                       "-t", f"{duration:.2f}", "-map", "0:v:0", "-an", "-vf", f"scale={width}:{height},setsar=1",
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                       "-movflags", "+faststart", "-metadata:s:v:0", "rotate=0", "clean.mp4"], cwd=work,
                      jid=f"{job['id']}:{attempt['id']}", cancel=check, on_progress=progress, duration=duration,
                      phase="Encoding clean editing source")
        check()
        meta = media.probe(work / "clean.mp4")
        with _lock:
            check()
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(work / "clean.mp4"), str(path))
            entry.update(status="ready", path=str(path), width=meta["width"], height=meta["height"])
            attempt.update(status="done", outcome="success")
            save(job)
            success = True
    except Exception as exc:
        error = exc
        entry.update(status="error", error=safe.friendly(exc, 300))
    finally:
        try:
            media.kill_job_procs(f"{job['id']}:{attempt['id']}")
            if work:
                shutil.rmtree(work, ignore_errors=True)
            if not success:
                path.unlink(missing_ok=True)
        except Exception:
            pass
        finally:
            _finish_attempt(job, clip, attempt, error)


CLIP_MINUTES = float(config.env("QLIPIT_CLIP_MINUTES", "15"))     # the longest one clip may take to make


def _wait_for_clips(job, futures, began, workers):
    """Wait for a video's clips, but not for ever. A clip that hangs (a download that stalls, an encoder that
    freezes) used to keep the whole video "in progress", and no other clip could be re-made. Now it is marked as
    failed after CLIP_MINUTES and the video finishes with the clips that did work."""
    left, given_up, total, said = list(futures), 0, len(futures), -1
    late = "This clip took too long and was stopped. Open it and press Save and re-make to try again."
    while left:
        if job.get("stop"):
            for f, clip in left:
                f.cancel()
                attempt = clip.get("attempt")
                if attempt and attempt.get("status") in ATTEMPT_ACTIVE:
                    cancel_attempt(job["id"], clip["id"], attempt["id"])
                if clip.get("status") in (states.ClipStatus.QUEUED, states.ClipStatus.RENDERING):
                    states.transition_clip_status(clip, states.ClipStatus.ERROR)
                    clip.update(error="Stopped by you.")
            save(job)
            raise Stopped()
        still = []
        for f, clip in left:
            if f.done():
                try:
                    f.result()
                except Exception as exc:
                    attempt = clip.get("attempt")
                    if attempt:
                        _finish_attempt(job, clip, attempt, exc)
                    else:
                        clip.update(status="error", outcome="failed", error=safe.friendly(exc, 400))
                        save(job)
                continue
            t = began.get(clip["id"])
            if (t and time.time() - t > CLIP_MINUTES * 60) or (not t and given_up >= workers):
                given_up += 1
                f.cancel()
                attempt = clip.get("attempt")
                if attempt:
                    _controls.setdefault(attempt["id"], threading.Event()).set()
                    attempt.update(cancel_requested=True, status="error", outcome="failed", error=late,
                                   cleanup_pending=bool(attempt.get("worker_active")), finished=time.time())
                    media.kill_job_procs(f"{job['id']}:{attempt['id']}")
                if clip.get("status") != "done":
                    clip.update(status="error", error=late, outcome="failed")
                try:
                    creators.log(job.get("creator"), f"Clip stopped after {CLIP_MINUTES:g} minutes: {clip.get('title') or clip.get('hook') or clip['id']}")
                except Exception:
                    pass
                save(job)
                continue
            still.append((f, clip))
        left = still
        done = total - len(left)
        if done != said:                            # one line per clip, so the last step is seen to move
            said = done
            report_progress(job, phase=f"Making clip {min(done + 1, total)} of {total}",
                            completed=done, total=total, unit="clips")
        if left:
            time.sleep(1)
    if job.get("stop"):
        raise Stopped()


def _english_part(job, clip):
    """YouTube's English captions for just this clip, for a video in another language. Fetched once per video."""
    if (job.get("source") or {}).get("type") != "url":
        return None
    f = Path(job["dir"]) / "words_en.json"
    if f.is_file():
        en = json.loads(f.read_text(encoding="utf-8"))
    else:
        try:
            en = media.youtube_english_words(job["source"]["url"]) or []
        except Exception:  # noqa: BLE001 - YouTube often refuses its translations: the AI translates instead
            en = []
        f.write_text(json.dumps(en), encoding="utf-8")       # asked once per video, refused or not
    return [w for w in en if clip["start"] - 0.5 <= w["s"] <= clip["end"] + 0.5]


LONG_VIDEO = 40 * 60   # above this, fetch only the pieces that become clips
PAD = 8                # seconds kept either side of a fetched piece, so clips can be re-trimmed


def _fetch_piece(job, clip, full_audio, cancel=None, audio_origin=0):
    """Long videos: download just this clip's stretch and pin down exactly where it starts."""
    d = Path(job["dir"])
    o = job["options"]
    try:
        want = max(0.0, clip["start"] - PAD)
        piece = media.download_section(job["source"]["url"], d / "clips", f"{clip['id']}.src", want,
                                       min(job["duration"], clip["end"] + PAD), int(o["quality"]),
                                       jid=job["id"], cancel=cancel)
        wav = d / "clips" / f"{clip['id']}.wav"
        media.extract_audio(piece, wav, jid=job["id"], cancel=cancel)
        part = media.load_audio(wav)
        wav.unlink(missing_ok=True)
        meta = media.probe(piece)
        off = media.find_offset(full_audio, part, max(0, want - audio_origin)) + audio_origin
        clip.update(src=str(piece), src_offset=round(off, 2), src_end=round(off + meta["duration"], 2))
        clip["start"], clip["end"] = max(clip["start"], clip["src_offset"]), min(clip["end"], clip["src_end"])
        job.setdefault("width", meta["width"])
        job.setdefault("height", meta["height"])
    except Stopped:
        raise
    except Exception as e:  # noqa: BLE001
        states.transition_clip_status(clip, states.ClipStatus.ERROR)
        clip.update(error=f"Could not fetch this part of the video: {safe.friendly(e, 250)}")


BLOCK = 10 * 60         # long videos are transcribed and clipped one block at a time
PROGRESSIVE_FROM = 20 * 60


def _source_range(duration, options):
    start = float(options["range_start"] if options.get("range_start") is not None else 0)
    end = float(options["range_end"] if options.get("range_end") is not None else duration)
    if not all(math.isfinite(v) for v in (duration, start, end)) or duration <= 0 or not 0 <= start < end <= duration + 0.01:
        raise ValueError("The selected range must stay inside the source video.")
    if end - start > config.MAX_VIDEO_HOURS * 3600:
        raise ValueError(f"The selected part is longer than {config.MAX_VIDEO_HOURS:g} hours. Choose a shorter part under Options.")
    return start, min(end, duration)


def _overlaps(candidate, existing):
    intersection = max(0, min(candidate["end"], existing["end"]) - max(candidate["start"], existing["start"]))
    return intersection > 0.3 * min(candidate["end"] - candidate["start"], existing["end"] - existing["start"])


def _covers(words: list, start: float, end: float) -> bool:
    """Is a borrowed transcript good enough to find clips with? It has to reach the end of the part being clipped
    and have a normal amount of talking in it; otherwise our own model writes the transcript."""
    inside = [w for w in words if start <= w["s"] <= end]
    minutes = max(0.5, (end - start) / 60)
    return len(inside) >= 25 * minutes and inside[-1]["s"] >= start + 0.85 * (end - start)


def _job_check(job):
    if job.get("stop") or job.get("cancel_requested"):
        raise Stopped("Stopped by you.")


def _pool(job, *args, **kwargs):
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(*args, **kwargs)
    _job_pools.setdefault(job["id"], []).append(pool)
    return pool


def _tracked(job, fn, *args, **kwargs):
    with _lock:
        _background_work[job["id"]] += 1
        job["cleanup_pending"] = True
    try:
        _job_check(job)
        return fn(*args, **kwargs)
    finally:
        with _lock:
            _background_work[job["id"]] -= 1
            if not _background_work[job["id"]] and job["id"] not in _main_work:
                job["cleanup_pending"] = False
                if job["id"] in _deferred_slots:
                    _deferred_slots.remove(job["id"])
                    _q.done()
        try:
            save(job)
        except Exception:
            ctx.bump(job.get("user"))


def _run_job(job):
    _main_work.add(job["id"])
    job["cleanup_pending"] = True
    try:
        _job_check(job)
        return _process_job(job)
    finally:
        for pool in _job_pools.pop(job["id"], []):
            pool.shutdown(wait=False, cancel_futures=True)
        media.kill_job_procs(job["id"])
        with _lock:
            _main_work.discard(job["id"])
            job["cleanup_pending"] = bool(_background_work[job["id"]])
        try:
            save(job)
        except Exception:
            ctx.bump(job.get("user"))


def _process_job(job):
    import math
    _job_check(job)
    d = Path(job["dir"])
    o = job["options"]
    src = job["source"]
    t0 = time.time()
    notes = []
    usage.set_context(job["creator"], job["title"] or src.get("name"))
    resuming = bool(job.pop("resume", False)) and (d / "words.json").is_file()
    states.transition_job_status(job, states.JobStatus.RUNNING)
    if resuming:   # after a restart: keep the transcript parts, clips and AI answers already made, and carry on
        _set(job, stage="Picking up where it stopped", error=None)
    else:
        _set(job, stage="Getting the audio", progress=0.0, error=None, clips=[], ai={}, done_blocks=0,
             outcome=None, cancel_requested=False)
    if not transcribe.model_ready() and not transcribe.REMOTE:
        from . import fetch_model
        if config.env("QLIPIT_FETCH_MODEL", "1") == "0":
            raise RuntimeError("The installed English speech model is unavailable. Restore it or download it explicitly from Settings; automatic model downloads are disabled.")
        def _model_cb(pct, stage, dl, total):
            _job_check(job)
            report_progress(job, phase=stage or "Getting speech model", completed=dl, total=total or None, unit="bytes")
        report_progress(job, phase="Waiting for installed speech model")
        fetch_model.ensure(on_progress=_model_cb)
        _set(job, stage="Getting the audio")
    fetch = _pool(job, 1)
    video = None
    if src["type"] == "url":
        meta = media.info(src["url"])
        if meta["kind"] != "video":
            raise RuntimeError("That link is a channel or playlist, not a single video. Paste the link of one video.")
        if meta["duration"] > 0:
            _source_range(meta["duration"], o)
        if meta["is_live"]:
            raise RuntimeError("This link is a live stream, so it would never finish downloading. Open the creator, "
                               "put this link under Setup, and press Start recording instead.")
        _set(job, title=meta["title"], uploader=meta["uploader"], duration=meta["duration"])
        if not job.get("thumb") and media.save_thumb(d, url=src["url"]):
            _set(job, thumb=True)
        usage.set_context(job["creator"], meta["title"])
        span = (o["range_end"] or meta["duration"]) - (o["range_start"] or 0)
        pieces = span > LONG_VIDEO or meta["duration"] <= 0
        if not pieces:  # shorter video: fetch all of it in the background while the audio is worked on
            video = fetch.submit(ctx.bind(_tracked), job, media.download, src["url"], d, int(o["quality"]),
                                 jid=job["id"], cancel=lambda: _job_check(job))
        def download_progress(p):
            _job_check(job)
            report_progress(job, phase="Downloading audio", completed=p, total=1, unit="download fraction")
        report_progress(job, phase="Downloading audio")
        audio_file = media.download_audio(src["url"], d, download_progress, jid=job["id"], cancel=lambda: _job_check(job))
    else:
        pieces, audio_file = False, Path(src["path"])
        info = media.probe(audio_file)
        _source_range(info["duration"], o)
        if not info.get("audio", True):
            raise RuntimeError("This video has no audio track to transcribe. Existing silent clips can still be edited and exported.")
        _set(job, source_path=str(audio_file), width=info["width"], height=info["height"], duration=info["duration"])
        if not job.get("thumb") and media.save_thumb(d, video=audio_file):
            _set(job, thumb=True)
    wav = d / "audio.wav"
    selected_start = float(o.get("range_start") or 0)
    selected_end = float(o.get("range_end") or job.get("duration") or 0) or None
    # Retained URL pieces may start before the selected range or on an earlier keyframe.
    alignment_padding = PAD + 35 if src["type"] == "url" and pieces else 0
    audio_origin = max(0, selected_start - alignment_padding)
    audio_end = selected_end + alignment_padding if selected_end is not None else None
    if audio_end is not None and job.get("duration"):
        audio_end = min(audio_end, job["duration"])
    report_progress(job, phase="Decoding source audio")
    media.extract_audio(audio_file, wav, start=audio_origin, end=audio_end, jid=job["id"],
                        cancel=lambda: _job_check(job))
    audio = media.load_audio(wav)
    wav.unlink(missing_ok=True)
    if src["type"] == "url":
        audio_file.unlink(missing_ok=True)
    if not job["duration"]:
        job["duration"] = audio_origin + len(audio) / 16000
    _source_range(job["duration"], o)
    lo = int((selected_start - audio_origin) * 16000)
    hi = min(len(audio), int((selected_end - audio_origin) * 16000)) if selected_end is not None else len(audio)
    if hi <= lo:
        raise RuntimeError("No decodable audio was found in the selected source interval.")
    span = (hi - lo) / 16000
    db = media.loudness_per_second(audio[lo:hi])

    vlock, vstate = threading.Lock(), {}

    def whole_video():
        """Wait for the background download once; retry it, or give up and use pieces."""
        with vlock:
            if "path" not in vstate:
                path = None
                try:
                    path = video.result()
                except Exception as e:  # noqa: BLE001 - sites sometimes refuse a download
                    notes.append(f"Video download refused once ({str(e)[-80:]}), retrying")
                    try:
                        _job_check(job)
                        path = media.download(src["url"], d, int(o["quality"]), jid=job["id"], cancel=lambda: _job_check(job))
                    except Exception as e2:  # noqa: BLE001
                        notes.append(f"Whole-video download failed ({str(e2)[-80:]}), fetching clip parts instead")
                if path:
                    info = media.probe(path)
                    job.update(source_path=str(path), width=info["width"], height=info["height"])
                vstate["path"] = path
            return vstate["path"]

    exact = {}      # clip id -> (from, to, words): our own model's words for just that clip

    began = {}      # clip id -> when work on it started

    def make(clip, words_now):
        attempt = clip.get("attempt")
        if not attempt or attempt.get("status") not in ATTEMPT_ACTIVE:
            if job.get("stop") or clip.get("outcome") == "cancelled":
                return
            attempt = _reserve_attempt(job, clip, _clip_settings(clip))
        try:
            check = lambda: _attempt_check(job, clip, attempt)
            check()
            began[clip["id"]] = time.time()
            attempt.update(status="running", worker_active=True, cleanup_pending=True, started=began[clip["id"]],
                           deadline=began[clip["id"]] + CLIP_MINUTES * 60)
            report_progress(job, attempt, phase="Preparing source")
            if src["type"] == "url" and (pieces or not whole_video()):
                check()
                _fetch_piece(job, clip, audio, cancel=check, audio_origin=audio_origin)
            check()
            if clip["status"] == "error":
                raise RuntimeError(clip.get("error") or "Source preparation failed.")
            if yt and yt[1].lower().startswith("en"):
                # Imported timings find candidates; the retained English recognizer refines only the clip span.
                a0 = max(lo, int((clip["start"] - audio_origin - 1.5) * 16000))
                b0 = min(hi, int((clip["end"] - audio_origin + 1.5) * 16000))
                try:
                    report_progress(job, attempt, phase="Refining English word timings")
                    tw = [dict(w, s=round(w["s"] + audio_origin + a0 / 16000, 2),
                               e=round(min(w["e"] + audio_origin + a0 / 16000, audio_origin + b0 / 16000), 2))
                          for w in transcribe.transcribe(audio[a0:b0])]
                except Exception:
                    check()
                    tw = []
                if len(tw) >= 3:
                    exact[clip["id"]] = (audio_origin + a0 / 16000, audio_origin + b0 / 16000, tw)
                    words_now = tw
            check()
            render_one(job, clip, words_now, attempt=attempt)
        except Exception as exc:
            _finish_attempt(job, clip, attempt, exc)

    cpus = os.cpu_count() or 4
    env_renders = config.penv("QLIPIT_MAX_RENDERS")
    max_renders = int(env_renders) if env_renders else (1 if cpus < 6 else 2)
    renders = _pool(job, max(1, max_renders))  # capped on laptops to prevent thermal throttling and fan noise
    futures = []
    step = BLOCK * 16000 if span > PROGRESSIVE_FROM else hi - lo
    blocks = [(x, min(hi, x + step)) for x in range(lo, hi, step)] or [(lo, hi)]
    job["blocks"] = len(blocks)
    words, tokens, model = [], 0, None
    done_blocks = 0
    yt = None
    if src["type"] == "url" and config.YT_TRANSCRIPT and media.is_youtube(src["url"]):
        try:
            yt = media.youtube_words(src["url"])
        except Exception:  # noqa: BLE001 - no transcript to borrow: ours is written as usual
            yt = None
        if yt and not _covers(yt[0], audio_origin + lo / 16000, audio_origin + hi / 16000):
            yt = None
        job["transcript_from"] = "youtube" if yt else "own"
        job["lang"] = yt[1] if yt else ""          # the language spoken, when YouTube says (decides the captions)
    if resuming:
        try:
            words = json.loads((d / "words.json").read_text(encoding="utf-8"))
        except ValueError:
            words = []
        done_blocks = min(int(job.get("done_blocks", 0)), len(blocks)) if words else 0
        tokens, model = int((job.get("ai") or {}).get("tokens", 0)), (job.get("ai") or {}).get("model")
        if done_blocks < len(blocks):     # words past the last finished part are transcribed again
            cut = audio_origin + blocks[done_blocks][0] / 16000
            words = [w for w in words if w["s"] < cut]
        for c in job["clips"]:            # clips found before the restart that were not finished yet
            if c.get("status") not in ("done", "error"):
                c["status"] = "queued"
                futures.append((renders.submit(ctx.bind(_tracked), job, make, c, list(words)), c))
        save(job)
    # The transcript of the next part is written while the AI is still reading this one (the AI is a wait on the
    # network, the transcript is work on this machine), so neither waits for the other.
    ahead = _pool(job, 1, thread_name_prefix="ahead")
    heard, now_on = {}, [done_blocks]

    def hear(i):
        if i < len(blocks) and i >= done_blocks and i not in heard:
            a_, b_ = blocks[i]
            last = [0.0]

            def tp(p, i=i, a_=a_, b_=b_):
                if now_on[0] == i and p - last[0] > 0.05:      # only the part being shown moves the bar
                    last[0] = p
                    _job_check(job)
                    report_progress(job, phase="Writing the transcript", completed=p * (b_ - a_) / 16000,
                                    total=(b_ - a_) / 16000, unit="seconds", phase_attempt=i + 1)
            heard[i] = ahead.submit(ctx.bind(_tracked), job, transcribe.transcribe, audio[a_:b_], tp)

    for bi, (a, b) in enumerate(blocks):
        if job.get("stop"):
            raise Stopped()
        if bi < done_blocks:
            continue                      # transcribed and checked by the AI before the restart
        shift = audio_origin + a / 16000
        where = f" (minute {round((a - lo) / 16000 / 60)} of {round(span / 60)})" if len(blocks) > 1 else ""
        if yt:
            _set(job, stage="Reading the transcript" + where)
            bw = [dict(w, s=round(w["s"] - shift, 2), e=round(min(w["e"], audio_origin + b / 16000) - shift, 2))
                  for w in yt[0] if shift <= w["s"] < audio_origin + b / 16000]
        else:
            _set(job, stage="Writing the transcript" + where)
            now_on[0] = bi
            hear(bi)
            hear(bi + 1)
            bw = heard.pop(bi).result()
        _job_check(job)
        report_progress(job, phase="Writing the transcript", completed=(b - a) / 16000,
                        total=(b - a) / 16000, unit="seconds", phase_attempt=bi + 1)
        words += [dict(w, s=round(w["s"] + shift, 2), e=round(min(w["e"] + shift, audio_origin + b / 16000), 2)) for w in bw]
        (d / "words.json").write_text(json.dumps(words), encoding="utf-8")
        _set(job, word_count=len(words))
        report_progress(job, phase="Finding the best moments" + where)
        left = int(o["max_clips"]) - len(job["clips"])
        if left <= 0 or len(bw) < 8:
            job["done_blocks"] = bi + 1
            save(job)
            continue
        quota = max(1, math.ceil(left / (len(blocks) - bi)))
        context_start = max(lo, a - int(o["max_len"]) * 16000) if bi else a
        context_shift = audio_origin + context_start / 16000
        context_end = audio_origin + b / 16000
        context_words = [dict(w, s=round(w["s"] - context_shift, 2), e=round(min(w["e"], context_end) - context_shift, 2))
                         for w in words if context_shift <= w["s"] < context_end]
        s0 = int((context_start - lo) / 16000)
        found, ai = brain.find_clips(context_words, db[s0: s0 + int((b - context_start) / 16000) + 1],
                                     (b - context_start) / 16000, job["title"],
                                     job["uploader"], quota, int(o["min_len"]), int(o["max_len"]), notes.append,
                                     progress=lambda f: report_progress(
                                         job, phase="Finding the best moments" + where,
                                         completed=f, total=1.0, unit="steps", phase_attempt=bi + 1))
        _job_check(job)
        tokens += ai.get("tokens", 0)
        model = ai.get("model") or model
        if ai.get("note"):
            notes.append(ai["note"])
        for c in found:
            c.update(start=round(c["start"] + context_shift, 2), end=round(c["end"] + context_shift, 2))
            if not all(math.isfinite(c[k]) for k in ("start", "end")) or not audio_origin <= c["start"] < c["end"] <= audio_origin + hi / 16000 + 0.01:
                continue
            if any(_overlaps(c, old) for old in job["clips"]):
                continue
            c.update(id=_next_clip_id(job), style=o["style"], layout=o["layout"], status="queued", version=0)
            job["clips"].append(c)
            _reserve_attempt(job, c, _clip_settings(c))
            futures.append((renders.submit(ctx.bind(_tracked), job, make, c, list(words)), c))
        job["ai"] = {"model": model, "tokens": tokens, "note": "", "log": notes[-5:]}
        job["done_blocks"] = bi + 1       # a restart from here on carries on with the next part
        save(job)
    if futures:
        report_progress(job, phase="Finishing the clips")
    try:
        _wait_for_clips(job, futures, began, renders._max_workers)
    finally:                 # a stopped video must not leave its workers running behind it
        renders.shutdown(wait=False, cancel_futures=True)
        ahead.shutdown(wait=False, cancel_futures=True)
        fetch.shutdown(wait=False, cancel_futures=True)
        media.kill_job_procs(job["id"])
    if exact:           # keep our own words for the clips in the saved transcript, so editing a clip later uses them
        for a_s, b_s, tw in exact.values():
            words = [w for w in words if not a_s <= w["s"] < b_s] + tw
        words.sort(key=lambda w: w["s"])
        (d / "words.json").write_text(json.dumps(words), encoding="utf-8")
    del audio
    clips = job["clips"]
    ready = sum(c.get("status") == "done" and bool(c.get("file")) for c in clips)
    outcome = "empty" if not clips else "success" if ready == len(clips) else "partial" if ready else "failed"
    job["ai"] = {"model": model, "tokens": tokens, "log": notes[-5:],
                 "note": "The AI was unavailable, so clips were picked by loudness only." if clips and not model else ""}
    stage = "Ready" if outcome == "success" else "Nothing clip-worthy was found" if outcome == "empty" else f"{ready} of {len(clips)} clips ready"
    states.transition_job_status(job, states.JobStatus.DONE)
    _set(job, stage=stage, outcome=outcome, progress=1.0 if outcome == "success" else 0.0, took=round(time.time() - t0))
    try:
        from . import notices
        name = job.get("title") or job["source"].get("name") or "your video"
        notices.add(job.get("user"), "clips", f"{ready} clip{'s' if ready != 1 else ''} ready" if ready else "Nothing clip-worthy found" if not clips else "No clips could be rendered",
                    name, f"/app/creators/{job['creator']}" if job.get("creator") else "/app/videos")
    except Exception:  # noqa: BLE001
        pass
    try:
        creators.log(job["creator"], f"Video finished with {ready} ready of {len(clips)} clips in {round(time.time() - t0)}s: {job['title']}")
    except Exception:
        pass


def _worker(quick_only=False):
    while True:
        kind, a, cid = _q.get(quick_only)
        if kind == "fn":
            try:
                a()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            continue
        job = _jobs.get(a)
        if not job:
            if kind == "job":
                _q.done()
            continue
        token = ctx._uid.set(job["user"])  # everything this job does runs as its owner
        try:
            if kind == "job":
                _run_job(job)
            elif kind in ("attempt", "source"):
                attempt = (job.get("attempts") or {}).get(cid)
                clip = next((c for c in job["clips"] if attempt and c["id"] == attempt["clip_id"]), None)
                if attempt and clip:
                    if kind == "source":
                        _prepare_editor_source(job, clip, attempt)
                    else:
                        render_one(job, clip, attempt=attempt)
            else:
                clip = next(c for c in job["clips"] if c["id"] == cid)
                render_one(job, clip)
        except Stopped:
            for c in job["clips"]:
                if c.get("status") in (states.ClipStatus.QUEUED, states.ClipStatus.RENDERING):
                    states.transition_clip_status(c, states.ClipStatus.ERROR)
                    c.update(error="Stopped by you.")
            job.update(status="done", stage="Stopped by you", progress=0.0, error=None, outcome="cancelled", cancel_requested=True)
            try:
                save(job)
                creators.log(job.get("creator"), f"Video stopped by you: {job.get('title', '')}")
            except Exception:
                ctx.bump(job.get("user"))
        except Exception as e:  # noqa: BLE001
            try:
                (Path(job["dir"]) / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
            except Exception:
                pass
            if kind == "job":
                ready = any(c.get("status") == "done" and c.get("file") for c in job["clips"])
                job.update(stop=True, cancel_requested=True, outcome="partial" if ready else "failed")
                for attempt in (job.get("attempts") or {}).values():
                    if attempt.get("status") in ATTEMPT_ACTIVE:
                        _controls.setdefault(attempt["id"], threading.Event()).set()
                        attempt.update(cancel_requested=True, status="error", outcome="failed", error="The video stopped before this clip was made.",
                                       cleanup_pending=bool(attempt.get("worker_active")))
                        media.kill_job_procs(f"{job['id']}:{attempt['id']}")
                for c in job["clips"]:
                    if c.get("status") in (states.ClipStatus.QUEUED, states.ClipStatus.RENDERING):
                        states.transition_clip_status(c, states.ClipStatus.ERROR)
                        c.update(error="The video stopped before this clip was made.")
                job.update(status="error", error=safe.friendly(e), stage="Stopped")
                try:
                    save(job)
                    creators.log(job.get("creator"), f"Video failed: {safe.friendly(e)[:200]}")
                except Exception:
                    ctx.bump(job.get("user"))
            elif kind in ("attempt", "source"):
                attempt = (job.get("attempts") or {}).get(cid)
                clip = next((c for c in job["clips"] if attempt and c["id"] == attempt["clip_id"]), None)
                if attempt and clip:
                    _finish_attempt(job, clip, attempt, e)
        finally:
            ctx._uid.reset(token)
            if kind == "job":
                with _lock:
                    if _background_work[job["id"]]:
                        _deferred_slots.add(job["id"])
                    else:
                        _q.done()


def _pick_up_after_restart():
    """Videos and single clips that were being made when the server stopped: queued again, oldest first."""
    waiting = sorted((j for j in all_jobs() if j.get("status") == "queued" and j.get("resume")), key=lambda j: j["created"])
    for j in waiting:
        _q.put(("job", j["id"], None), user=j["user"])
    for j in all_jobs():
        for cid in j.pop("remake", []):
            _q.put(("clip", j["id"], cid))


def _heartbeat_worker():
    while True:
        time.sleep(2)
        for job in all_jobs():
            changed = False
            now = time.time()
            with _lock:
                if (job.get("status") == "running" or job.get("cleanup_pending")) and job.get("progress_detail"):
                    job["progress_detail"]["heartbeat"] = now
                    changed = True
                for attempt in (job.get("attempts") or {}).values():
                    if (attempt.get("status") == "running" or attempt.get("cleanup_pending")) and attempt.get("progress"):
                        attempt["progress"]["heartbeat"] = now
                        changed = True
            if changed:
                ctx.bump(job.get("user"))


def start():
    load.start()
    load_all()
    _pick_up_after_restart()
    for _ in range(max(1, config.JOB_WORKERS)):
        threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_worker, args=(True,), daemon=True).start()   # re-making a single clip never waits behind whole videos
    threading.Thread(target=_live_worker, daemon=True).start()
    threading.Thread(target=_heartbeat_worker, daemon=True).start()
