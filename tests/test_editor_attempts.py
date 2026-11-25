"""Isolated regressions for immutable editor attempts and safe publication."""
import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = Path(tempfile.mkdtemp(prefix="qlipit-attempt-test-"))
for key in list(os.environ):
    if key.startswith(("QLIPIT_", "PEAKCUT_")):
        del os.environ[key]
os.environ.update(QLIPIT_LOAD_DOTENV="0", QLIPIT_DATA=str(DATA), QLIPIT_FETCH_MODEL="0",
                  QLIPIT_UPDATE_CHECK="0", DATABASE_URL="")

from studio import account, ctx, jobs, media, render, states
REAL_PROBE = media.probe


class AttemptTests(unittest.TestCase):
    def setUp(self):
        self.context = ctx.as_user("u_test01")
        self.context.__enter__()
        self.job = jobs.create({"type": "file", "path": "", "name": "fixture.mp4"}, {}, enqueue=False)
        self.folder = Path(self.job["dir"])
        self.source = self.folder / "source.mp4"
        self.source.write_bytes(b"source fixture")
        self.job.update(status="done", source_path=str(self.source), duration=60, width=640, height=360)
        self.clip = {"id": "clip01", "status": "done", "start": 10.0, "end": 20.0,
                     "hook": "SAVED", "style": "clean", "layout": "blur", "title": "Saved title",
                     "description": "Saved description", "hashtags": [], "score": 80, "version": 1,
                     "file": "clip01.mp4", "cover": "clip01.jpg",
                     "voiceover": {"enabled": False, "script": "Retained draft"}}
        self.job["clips"] = [self.clip]
        (self.folder / "clips").mkdir()
        for ext in ("mp4", "jpg"):
            (self.folder / "clips" / ("clip01." + ext)).write_bytes(b"last good artifact")
        (self.folder / "words.json").write_text("[]", encoding="utf-8")
        self.probe = patch.object(media, "probe", return_value={"width": 640, "height": 360, "duration": 60.0,
                            "coded_width": 640, "coded_height": 360, "sar": 1.0, "rotation": 0,
                            "audio": True, "codec": "h264", "pix_fmt": "yuv420p"})
        self.probe_mock = self.probe.start()
        self.queue = patch.object(jobs._q, "put")
        self.queue.start()
        self.caption = patch.object(jobs.brain, "english_captions", side_effect=lambda words, *a, **k: words)
        self.caption.start()

    def tearDown(self):
        self.caption.stop()
        self.queue.stop()
        self.probe.stop()
        jobs._jobs.pop(self.job["id"], None)
        jobs._by_user.get(self.job["user"], {}).pop(self.job["id"], None)
        self.context.__exit__(None, None, None)
        shutil.rmtree(self.folder, ignore_errors=True)

    def attempt(self, changes=None, preview=False):
        result = jobs.request_render(self.job["id"], "clip01", changes or {}, preview=preview)
        return self.job["attempts"][result["attempt"]["id"]]

    def encoder(self, src, out_dir, name, clip, words, *args, **kwargs):
        out_dir.mkdir(parents=True, exist_ok=True)
        for ext in ("mp4", "jpg", "ass"):
            (out_dir / (name + "." + ext)).write_bytes(b"new artifact")
        return {"file": name + ".mp4", "cover": name + ".jpg", "layout": clip["layout"]}

    def test_pending_settings_do_not_replace_success(self):
        old = copy.deepcopy(self.clip)
        changes = {"hook": "NEW", "voiceover": {"enabled": True, "script": "New narration", "mode": "mute"}}
        attempt = self.attempt(changes)
        changes["voiceover"]["script"] = "Changed after submission"
        self.assertEqual(self.clip["hook"], old["hook"])
        self.assertEqual(self.clip["voiceover"], old["voiceover"])
        self.assertEqual(self.clip["file"], old["file"])
        self.assertEqual(self.clip["status"], "done")
        self.assertEqual(attempt["settings"]["voiceover"]["script"], "New narration")

    def test_final_interval_is_rejected_not_silently_clamped(self):
        with self.assertRaises(ValueError):
            self.attempt({"start": 55, "end": 70})
        self.assertEqual(self.clip["end"], 20)

    def test_piece_uses_its_actual_geometry_and_global_time_bounds(self):
        piece = self.folder / "clips" / "clip01.src.ts"
        piece.write_bytes(b"piece")
        self.clip.update(src=str(piece), src_offset=100, src_end=130, start=105, end=115)
        self.job.update(duration=6000, width=1920, height=1080)
        self.probe_mock.return_value.update(width=320, height=180, duration=30)
        source = jobs.resolve_clip_source(self.job, self.clip)
        self.assertEqual(source["meta"]["width"], 320)
        self.assertEqual((source["start"], source["end"], source["offset"]), (100, 130, 100))
        with self.assertRaises(ValueError):
            self.attempt({"start": 90, "end": 115})

    def test_owner_check_applies_to_source_and_render(self):
        with ctx.as_user("u_other01"):
            with self.assertRaises((PermissionError, ValueError)):
                jobs.resolve_clip_source(self.job, self.clip)
            with self.assertRaises((PermissionError, ValueError)):
                self.attempt({"hook": "Other user"})

    def test_cancelled_result_cannot_publish(self):
        attempt = self.attempt({"hook": "CANCELLED"})
        jobs.cancel_attempt(self.job["id"], "clip01", attempt["id"])
        with patch.object(render, "render_clip", side_effect=self.encoder):
            jobs.render_one(self.job, self.clip, [], attempt=attempt)
        self.assertEqual(self.clip["file"], "clip01.mp4")
        self.assertEqual(self.clip["hook"], "SAVED")
        self.assertEqual(attempt["outcome"], "cancelled")

    def test_preview_is_temporary_and_apply_is_versioned(self):
        attempt = self.attempt({"hook": "PREVIEW"}, preview=True)
        with patch.object(render, "render_clip", side_effect=self.encoder):
            jobs.render_one(self.job, self.clip, [], attempt=attempt)
        self.assertEqual(self.clip["file"], "clip01.mp4")
        self.assertEqual(self.clip["hook"], "SAVED")
        self.assertEqual(self.clip["version"], 1)
        self.assertTrue(attempt["file"].endswith(".mp4"))
        applied = self.attempt({"hook": "APPLIED"})
        with patch.object(render, "render_clip", side_effect=self.encoder):
            jobs.render_one(self.job, self.clip, [], attempt=applied)
        self.assertEqual(self.clip["hook"], "APPLIED")
        self.assertNotEqual(self.clip["file"], "clip01.mp4")
        self.assertEqual(self.clip["artifact_revision"], applied["revision"])
        self.assertEqual(self.clip["published_attempt"], applied["id"])

    def test_failed_remake_keeps_download_and_settings(self):
        attempt = self.attempt({"hook": "BAD"})
        with patch.object(render, "render_clip", side_effect=RuntimeError("encoder failed")):
            jobs.render_one(self.job, self.clip, [], attempt=attempt)
        self.assertEqual(self.clip["status"], "done")
        self.assertEqual(self.clip["hook"], "SAVED")
        self.assertEqual(self.clip["file"], "clip01.mp4")
        self.assertEqual(attempt["outcome"], "failed")

    def test_revision_conflict_does_not_queue(self):
        with self.assertRaises(jobs.AttemptConflict):
            self.attempt({"expected_revision": 0})

    def test_deletion_is_busy_even_with_a_done_parent(self):
        attempt = self.attempt()
        self.assertTrue(jobs.has_active_work(self.job))
        with self.assertRaises(account.Busy):
            account.delete_clip(self.job["id"], "clip01")
        jobs.cancel_attempt(self.job["id"], "clip01", attempt["id"])
        self.assertFalse(jobs.has_active_work(self.job))

    def test_deleted_last_clip_never_reuses_an_allocated_id(self):
        self.job["clip_id_high_water"] = 8
        self.assertTrue(account.delete_clip(self.job["id"], "clip01"))
        self.assertEqual(jobs._next_clip_id(self.job), "clip09")
        self.assertEqual(jobs._next_clip_id(self.job), "clip10")

    def test_public_attempt_hides_nested_inputs_and_paths(self):
        self.attempt({"hook": "SAFE"})
        value = json.dumps(jobs.public(self.job))
        self.assertNotIn(str(self.folder), value)
        self.assertNotIn('"settings"', value)
        self.assertNotIn('"attempts"', value)

    def test_publication_requires_cover_as_well_as_video(self):
        attempt = self.attempt()
        def incomplete(src, out_dir, name, *a, **k):
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / (name + ".mp4")).write_bytes(b"not enough")
            return {"file": name + ".mp4", "cover": name + ".jpg"}
        with patch.object(render, "render_clip", side_effect=incomplete):
            jobs.render_one(self.job, self.clip, [], attempt=attempt)
        self.assertEqual(attempt["outcome"], "failed")
        self.assertEqual(self.clip["file"], "clip01.mp4")

    def test_cancel_during_encode_prevents_late_worker_publication(self):
        attempt = self.attempt({"hook": "LATE"})
        entered, release = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(3)
            return self.encoder(*args, **kwargs)
        with patch.object(render, "render_clip", side_effect=delayed):
            worker = threading.Thread(target=ctx.bind(jobs.render_one), args=(self.job, self.clip, []),
                                      kwargs={"attempt": attempt})
            worker.start()
            self.assertTrue(entered.wait(2))
            jobs.cancel_attempt(self.job["id"], "clip01", attempt["id"])
            self.assertTrue(jobs.has_active_work(self.job))
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.clip["hook"], "SAVED")
        self.assertEqual(self.clip["file"], "clip01.mp4")
        self.assertFalse(jobs.has_active_work(self.job))

    def test_missing_and_corrupt_transcript_are_visible_terminal_failures(self):
        for content in (None, "{broken", '{"words": null}'):
            with self.subTest(content=content):
                path = self.folder / "words.json"
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(content, encoding="utf-8")
                attempt = self.attempt()
                jobs.render_one(self.job, self.clip, attempt=attempt)
                self.assertEqual(attempt["status"], "error")
                self.assertEqual(attempt["outcome"], "failed")
                self.assertTrue(attempt["error"])
                self.assertEqual(self.clip["file"], "clip01.mp4")
                self.assertFalse(jobs.has_active_work(self.job))

    def test_save_failure_does_not_hide_terminal_state_or_replace_artifact(self):
        attempt = self.attempt({"hook": "NOT SAVED"})
        with patch.object(jobs, "save", side_effect=OSError("disk full")):
            jobs.render_one(self.job, self.clip, [], attempt=attempt)
        self.assertEqual(attempt["outcome"], "failed")
        self.assertEqual(self.clip["hook"], "SAVED")
        self.assertFalse(jobs.has_active_work(self.job))

    def test_expired_and_superseded_attempts_cannot_publish(self):
        attempt = self.attempt()
        attempt["deadline"] = time.time() - 1
        jobs.render_one(self.job, self.clip, [], attempt=attempt)
        self.assertEqual(attempt["outcome"], "failed")
        self.assertIn("too long", attempt["error"])
        old = self.attempt({"hook": "OLD"})
        jobs._reserve_attempt(self.job, self.clip, jobs._clip_settings(self.clip))
        jobs.render_one(self.job, self.clip, [], attempt=old)
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(self.clip["file"], "clip01.mp4")

    def test_progress_ticks_are_monotonic_and_only_checkpoints_are_persisted(self):
        attempt = self.attempt()
        with patch.object(jobs, "save") as saved:
            jobs.report_progress(self.job, attempt, phase="Encoding", completed=0, total=10, unit="seconds")
            for n in range(100):
                jobs.report_progress(self.job, attempt, phase="Encoding", completed=n / 10, total=10, unit="seconds")
            last_advance = attempt["progress"]["last_advance"]
            jobs.report_progress(self.job, attempt, phase="Encoding", completed=2, total=10, unit="seconds")
            self.assertEqual(attempt["progress"]["completed"], 9.9)
            self.assertEqual(attempt["progress"]["last_advance"], last_advance)
            self.assertEqual(saved.call_count, 1)
            jobs.report_progress(self.job, attempt, phase="Encoding", completed=0, total=10, unit="seconds", phase_attempt=2)
            self.assertEqual(attempt["progress"]["completed"], 0)
            self.assertEqual(saved.call_count, 2)
        self.assertEqual(self.clip["version"], 1)
        self.assertEqual(attempt["status"], "queued")

    def test_loading_old_successful_clip_does_not_rewrite_its_saved_json(self):
        jobs.save(self.job)
        path = self.folder / "job.json"
        before, modified = path.read_bytes(), path.stat().st_mtime_ns
        jobs._load_file(path, self.job["user"])
        jobs._pick_up_after_restart()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_source_range_allows_a_short_late_selection_in_a_long_source(self):
        with patch.object(jobs.config, "MAX_VIDEO_HOURS", 0.01):
            self.assertEqual(jobs._source_range(3600, {"range_start": 2010, "range_end": 2040}), (2010, 2040))
            with self.assertRaises(ValueError):
                jobs._source_range(3600, {"range_start": 3590, "range_end": 3700})

    def test_pipeline_extracts_the_late_span_and_offsets_transcript_once(self):
        import numpy as np
        self.job.update(status="queued", source={"type": "file", "path": str(self.source), "name": "long.mp4"})
        self.job["options"].update(range_start=2010, range_end=2040, max_clips=1, min_len=5, max_len=10)
        self.probe_mock.return_value.update(duration=3600)
        words = [{"w": "word", "s": n + 10, "e": n + 10.4} for n in range(15)]
        def extract(src, wav, **kwargs):
            wav.write_bytes(b"audio fixture")
        def choose(local, levels, duration, *args, **kwargs):
            self.assertEqual(local[0]["s"], 10)
            return [{"start": 18, "end": 27, "hook": "LATE", "score": 80}], {"tokens": 0}
        with patch.object(jobs.config, "MAX_VIDEO_HOURS", 0.01), \
                patch.object(media, "extract_audio", side_effect=extract) as decoded, \
                patch.object(media, "load_audio", return_value=np.zeros(30 * 16000, dtype=np.int16)), \
                patch.object(media, "save_thumb", return_value=False), \
                patch.object(jobs.transcribe, "model_ready", return_value=True), \
                patch.object(jobs.transcribe, "transcribe", return_value=words), \
                patch.object(jobs.brain, "find_clips", side_effect=choose), \
                patch.object(jobs.creators, "log", side_effect=OSError("final logging failed")), \
                patch.object(render, "render_clip", side_effect=self.encoder):
            jobs._run_job(self.job)
        self.assertEqual(decoded.call_args.kwargs["start"], 2010)
        self.assertEqual(decoded.call_args.kwargs["end"], 2040)
        saved = json.loads((self.folder / "words.json").read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["s"], 2020)
        self.assertEqual((self.job["clips"][0]["start"], self.job["clips"][0]["end"]), (2028, 2037))
        self.assertEqual(self.job["outcome"], "success")

    def test_worker_survives_missing_transcript_and_logging_failure(self):
        first = self.attempt()
        other = copy.deepcopy(self.clip)
        other.update(id="clip02", file="clip02.mp4", cover="clip02.jpg")
        other.pop("attempt", None)
        self.job["clips"].append(other)
        second = jobs._reserve_attempt(self.job, other, jobs._clip_settings(other))
        path = self.folder / "words.json"
        path.unlink()
        messages = iter([("attempt", self.job["id"], first["id"]),
                         ("fn", lambda: path.write_text("[]", encoding="utf-8"), None),
                         ("attempt", self.job["id"], second["id"])])
        def next_work(*args):
            try:
                return next(messages)
            except StopIteration:
                raise SystemExit
        with patch.object(jobs._q, "get", side_effect=next_work), \
                patch.object(render, "render_clip", side_effect=self.encoder), \
                patch.object(jobs.creators, "log", side_effect=OSError("log is unavailable")):
            worker = threading.Thread(target=jobs._worker)
            worker.start()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(first["outcome"], "failed")
        self.assertEqual(second["outcome"], "success")
        self.assertEqual(other["published_attempt"], second["id"])

    def test_future_exception_is_inspected_and_timeout_outcome_survives_late_cleanup(self):
        from concurrent.futures import Future
        attempt = self.attempt()
        failed = Future()
        failed.set_exception(RuntimeError("preparation failed"))
        jobs._wait_for_clips(self.job, [(failed, self.clip)], {}, 1)
        self.assertEqual(attempt["outcome"], "failed")
        next_attempt = self.attempt()
        waiting = Future()
        next_attempt.update(worker_active=True, cleanup_pending=True, status="running")
        jobs._wait_for_clips(self.job, [(waiting, self.clip)], {"clip01": time.time() - 10000}, 1)
        self.assertEqual(next_attempt["outcome"], "failed")
        jobs._finish_attempt(self.job, self.clip, next_attempt, jobs.Stopped("Stopped by you."))
        self.assertEqual(next_attempt["outcome"], "failed")
        self.assertIn("too long", next_attempt["error"])

    def test_analysis_context_overlap_suppresses_contained_duplicates_symmetrically(self):
        outer, inner = {"start": 10, "end": 40}, {"start": 20, "end": 25}
        self.assertTrue(jobs._overlaps(outer, inner))
        self.assertTrue(jobs._overlaps(inner, outer))
        self.assertFalse(jobs._overlaps(outer, {"start": 41, "end": 50}))

    def test_candidate_crosses_blocks_without_retranscribing_or_global_reranking(self):
        import numpy as np
        self.job.update(status="queued", source={"type": "file", "path": str(self.source), "name": "blocks.mp4"})
        self.job["options"].update(max_clips=2, min_len=5, max_len=10)
        self.probe_mock.return_value.update(duration=31)
        calls = []
        def extract(src, wav, **kwargs):
            wav.write_bytes(b"audio fixture")
        def recognize(audio, *args):
            return [{"w": "word", "s": n, "e": n + 0.4} for n in range(len(audio) // 16000)]
        def choose(words, levels, duration, *args, **kwargs):
            calls.append((copy.deepcopy(words), duration))
            if len(calls) == 1:
                return [{"start": 0, "end": 5, "hook": "EARLY"}], {"tokens": 0}
            return [{"start": 6, "end": 14, "hook": "CROSS BLOCK"}], {"tokens": 0}
        with patch.object(jobs, "BLOCK", 10), patch.object(jobs, "PROGRESSIVE_FROM", 20), \
                patch.object(media, "extract_audio", side_effect=extract), \
                patch.object(media, "load_audio", return_value=np.zeros(31 * 16000, dtype=np.int16)), \
                patch.object(media, "save_thumb", return_value=False), \
                patch.object(jobs.transcribe, "model_ready", return_value=True), \
                patch.object(jobs.transcribe, "transcribe", side_effect=recognize) as asr, \
                patch.object(jobs.brain, "find_clips", side_effect=choose), \
                patch.object(render, "render_clip", side_effect=self.encoder):
            jobs._run_job(self.job)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1], 20)
        self.assertEqual((calls[1][0][0]["s"], calls[1][0][-1]["s"]), (0, 19))
        self.assertEqual(asr.call_count, 4)
        self.assertEqual([(c["start"], c["end"]) for c in self.job["clips"]], [(0, 5), (6, 14)])

    def http_client(self):
        from fastapi.testclient import TestClient
        from studio import auth, config, db
        from studio.server import app
        db.init()
        user, _ = auth._create(f"editor-{time.time_ns()}@example.com", "Editor test", password_hash="x")
        jobs._by_user.get(self.job["user"], {}).pop(self.job["id"], None)
        self.job["user"] = user.id
        jobs._by_user.setdefault(user.id, {})[self.job["id"]] = self.job
        client = TestClient(app, base_url=config.PUBLIC_URL)
        client.cookies.set(auth.COOKIE, auth.start_session(user.id))
        client.headers["Origin"] = config.PUBLIC_URL
        self.addCleanup(client.close)
        return client, user

    def test_http_attempt_contract_and_temporary_preview(self):
        client, user = self.http_client()
        path = f"/api/jobs/{self.job['id']}/clips/clip01"
        with patch.object(jobs.config, "ACCOUNTS", True):
            response = client.post(path + "/preview", json={"hook": "HTTP PREVIEW", "expected_revision": 1})
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["job"]["clips"][0]["file"], "clip01.mp4")
            self.assertEqual(body["attempt"]["kind"], "preview")
            self.assertNotIn(str(self.folder), response.text)
            self.assertNotIn('"settings"', response.text)
            aid = body["attempt"]["id"]
            with ctx.as_user(user.id), patch.object(render, "render_clip", side_effect=self.encoder):
                jobs.render_one(self.job, self.clip, [], attempt=self.job["attempts"][aid])
            attempt = client.get(path + "/attempts/" + aid).json()
            self.assertEqual(attempt["status"], "done")
            self.assertEqual(attempt["revision"], body["attempt"]["revision"])
            self.assertEqual(self.clip["version"], 1)
            self.assertEqual(self.clip["file"], "clip01.mp4")
            conflict = client.post(path + "/render", json={"expected_revision": 0})
            self.assertEqual(conflict.status_code, 409)
            invalid = client.post(path + "/render", json={"start": 55, "end": 70})
            self.assertEqual(invalid.status_code, 400)

    def test_clean_source_is_owned_range_capable_and_not_compressed(self):
        client, user = self.http_client()
        path = f"/api/jobs/{self.job['id']}/clips/clip01"
        with patch.object(jobs.config, "ACCOUNTS", True):
            response = client.post(path + "/editor", json={"start": 10, "end": 20})
            self.assertEqual(response.status_code, 200, response.text)
            editor = response.json()
            self.assertEqual(editor["source"]["status"], "ready")
            self.assertTrue(editor["source"]["muted"])
            self.assertEqual((editor["visual"]["width"], editor["visual"]["height"]), (1080, 1920))
            self.assertNotIn(str(self.folder), response.text)
            media_response = client.get(editor["source"]["url"], headers={"Range": "bytes=0-5", "Accept-Encoding": "gzip"})
            self.assertEqual(media_response.status_code, 206)
            self.assertEqual(media_response.content, b"source")
            self.assertNotIn("content-encoding", media_response.headers)
            from studio import auth
            other, _ = auth._create(f"other-{time.time_ns()}@example.com", "Other", password_hash="x")
            client.cookies.set(auth.COOKIE, auth.start_session(other.id))
            self.assertEqual(client.get(editor["source"]["url"]).status_code, 404)
            self.assertEqual(client.post(path + "/editor", json={}).status_code, 404)

    def test_headline_http_bitmap_matches_resolved_canvas_and_validates_fonts(self):
        client, _ = self.http_client()
        path = f"/api/jobs/{self.job['id']}/clips/clip01/headline"
        visual = {"schema_version": 1, "headline": {"font": "anton", "position": {"x": 0, "y": 0}},
                  "framing": {"mode": "manual", "ratio": "1:1", "roi": {"x": 0, "y": 0, "width": 1, "height": 1}}}
        with patch.object(jobs.config, "ACCOUNTS", True):
            response = client.post(path, json={"hook": "CUSTOM", "visual": visual})
            self.assertEqual(response.status_code, 200, response.text)
            bitmap = response.json()
            self.assertTrue(bitmap["image"].startswith("data:image/png;base64,iVBOR"))
            self.assertEqual(bitmap["canvas_width"], bitmap["geometry"]["width"])
            self.assertEqual(bitmap["canvas_height"], bitmap["geometry"]["height"])
            self.assertGreater(bitmap["width"], 0)
            self.assertGreaterEqual(bitmap["position"]["x"], 0)
            visual["headline"]["font"] = "C:\\Windows\\Fonts\\arbitrary.ttf"
            self.assertEqual(client.post(path, json={"visual": visual}).status_code, 400)
            self.assertNotIn("visual", self.clip)

    def test_proxy_polling_uses_posted_interval_and_cancel_does_not_stay_queued(self):
        self.probe_mock.return_value["codec"] = "vp9"
        client, _ = self.http_client()
        path = f"/api/jobs/{self.job['id']}/clips/clip01"
        with patch.object(jobs.config, "ACCOUNTS", True):
            response = client.post(path + "/editor", json={"start": 12, "end": 23})
            self.assertEqual(response.status_code, 200, response.text)
            first = response.json()["source"]
            self.assertEqual(first["status"], "queued")
            self.assertEqual((first["start"], first["end"]), (4, 31))
            polled = client.get(path + "/editor").json()["source"]
            self.assertEqual(polled["attempt_id"], first["attempt_id"])
            self.assertEqual(client.delete(f"/api/jobs/{self.job['id']}").status_code, 409)
            response = client.post(path + "/attempts/" + first["attempt_id"] + "/cancel", json={})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get(path + "/editor").json()["source"]["status"], "error")

    def test_real_silent_source_visual_preview_apply_and_reedit(self):
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10:d=3",
                   "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(self.source)])
        self.job.update(duration=3, width=160, height=90)
        self.clip.update(start=0, end=3)
        visual = {"schema_version": 1, "headline": {"font": "anton", "background": None, "position": {"x": 0.5, "y": 0.3}},
                  "framing": {"mode": "manual", "ratio": "1:1", "roi": {"x": 0, "y": 0, "width": 1, "height": 1}}}
        with patch.object(media, "probe", side_effect=REAL_PROBE), patch.object(render, "_qsv_ok", False):
            render.install_fonts()
            preview = self.attempt({"visual": visual, "hook": "SQUARE"}, preview=True)
            jobs.render_one(self.job, self.clip, [], attempt=preview)
            self.assertEqual(preview["outcome"], "success", preview.get("error"))
            output = REAL_PROBE(self.folder / "clips" / preview["file"])
            self.assertEqual((output["width"], output["height"]), (90, 90))
            self.assertFalse(output["audio"])
            self.assertEqual(self.clip["file"], "clip01.mp4")
            applied = self.attempt({"visual": visual, "hook": "SQUARE"})
            jobs.render_one(self.job, self.clip, [], attempt=applied)
            self.assertEqual(applied["outcome"], "success", applied.get("error"))
            self.assertIn(self.clip["layout"], render.LAYOUTS)
            self.assertEqual(self.clip["visual"]["framing"]["ratio"], "1:1")
            reedit = self.attempt({"hook": "EDIT AGAIN"})
            self.assertEqual(reedit["settings"]["visual"]["framing"]["ratio"], "1:1")

    def test_stopping_parent_does_not_poison_new_edits_after_actual_cleanup(self):
        self.job["status"] = "running"
        self.assertTrue(jobs.stop(self.job["id"]))
        self.assertEqual(self.job["status"], "done")
        self.assertTrue(self.job["stop"])
        new = self.attempt({"hook": "AFTER STOP"})
        with patch.object(render, "render_clip", side_effect=self.encoder):
            jobs.render_one(self.job, self.clip, [], attempt=new)
        self.assertEqual(new["outcome"], "success", new.get("error"))
        self.assertEqual(self.clip["hook"], "AFTER STOP")
        self.assertTrue(self.job["stop"])

    def test_new_edit_waits_until_parent_cleanup_is_physically_finished(self):
        self.job.update(status="done", stop=True, cleanup_pending=True)
        with self.assertRaises(jobs.AttemptConflict):
            self.attempt({"hook": "TOO EARLY"})

    def test_deleted_clip_cannot_be_enqueued_during_editor_source_resolution(self):
        self.probe_mock.return_value["codec"] = "vp9"
        geometry = render.resolve_visual
        def remove_during_resolution(settings, metadata):
            account.delete_clip(self.job["id"], self.clip["id"])
            return geometry(settings, metadata)
        with patch.object(render, "resolve_visual", side_effect=remove_during_resolution):
            with self.assertRaises(jobs.AttemptConflict):
                jobs.editor_info(self.job, self.clip, prepare=True)
        self.assertFalse(self.job.get("attempts"))

    def test_cancelled_source_retry_waits_for_old_worker_cleanup(self):
        self.probe_mock.return_value["codec"] = "vp9"
        info = jobs.editor_info(self.job, self.clip, prepare=True)
        aid = info["source"]["attempt_id"]
        old = self.job["attempts"][aid]
        old.update(status="running", worker_active=True, cleanup_pending=True)
        jobs.cancel_attempt(self.job["id"], self.clip["id"], aid)
        with self.assertRaises(jobs.AttemptConflict):
            jobs.editor_info(self.job, self.clip, prepare=True)
        self.assertEqual(self.job["editor_sources"][old["asset_token"]]["attempt_id"], aid)

    def test_late_source_cleanup_never_deletes_a_newer_proxy(self):
        self.probe_mock.return_value["codec"] = "vp9"
        info = jobs.editor_info(self.job, self.clip, prepare=True)
        old = self.job["attempts"][info["source"]["attempt_id"]]
        token = old["asset_token"]
        entered, release = threading.Event(), threading.Event()
        metadata = copy.deepcopy(self.probe_mock.return_value)
        def probe(path, *args, **kwargs):
            if Path(path).name == "clean.mp4":
                entered.set()
                release.wait(3)
            return metadata
        def encode(*args, **kwargs):
            (Path(kwargs["cwd"]) / "clean.mp4").write_bytes(b"old temporary output")
        with patch.object(media, "run", side_effect=encode), patch.object(media, "probe", side_effect=probe):
            worker = threading.Thread(target=ctx.bind(jobs._prepare_editor_source), args=(self.job, self.clip, old))
            worker.start()
            self.assertTrue(entered.wait(2))
            jobs.cancel_attempt(self.job["id"], self.clip["id"], old["id"])
            newer = jobs._reserve_attempt(self.job, self.clip, jobs._clip_settings(self.clip), "source")
            newer.update(editor=True, asset_token=token, status="done", outcome="success")
            path = self.folder / "editor" / (token + ".mp4")
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"newer good proxy")
            self.job["editor_sources"][token] = {"status": "ready", "attempt_id": newer["id"], "path": str(path)}
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), b"newer good proxy")

    def test_url_piece_alignment_reference_includes_padding_before_selected_range(self):
        import numpy as np
        self.job.update(status="queued", source={"type": "url", "url": "http://example.invalid/video", "name": "late URL"})
        self.job["options"].update(range_start=2010, range_end=2040, max_clips=1, min_len=5, max_len=10)
        audio_path = self.folder / "audio_source.m4a"
        audio_path.write_bytes(b"download fixture")
        def extract(src, wav, **kwargs):
            wav.write_bytes(b"audio fixture")
        def load_audio(wav):
            return np.zeros((116 if Path(wav).name == "audio.wav" else 26) * 16000, dtype=np.int16)
        def download_section(url, folder, stem, start, end, quality, **kwargs):
            path = folder / (stem + ".mp4")
            path.write_bytes(b"retained source fixture")
            return path
        def probe(path, *args, **kwargs):
            return {"width": 640, "height": 360, "duration": 26 if ".src." in Path(path).name else 3600,
                    "audio": True, "coded_width": 640, "coded_height": 360, "sar": "1:1", "rotation": 0,
                    "codec": "h264", "pix_fmt": "yuv420p"}
        def recognize(audio, *args):
            return [{"w": "word", "s": n, "e": n + 0.4} for n in range(15)]
        with patch.object(jobs, "LONG_VIDEO", 10), patch.object(jobs.config, "YT_TRANSCRIPT", False), \
                patch.object(media, "info", return_value={"kind": "video", "duration": 3600, "is_live": False, "title": "Fixture", "uploader": "Fixture"}), \
                patch.object(media, "download_audio", return_value=audio_path), \
                patch.object(media, "download_section", side_effect=download_section), \
                patch.object(media, "extract_audio", side_effect=extract) as decoded, \
                patch.object(media, "load_audio", side_effect=load_audio), \
                patch.object(media, "probe", side_effect=probe), \
                patch.object(media, "find_offset", side_effect=lambda full, part, expected: expected) as aligned, \
                patch.object(media, "save_thumb", return_value=False), \
                patch.object(jobs.transcribe, "model_ready", return_value=True), \
                patch.object(jobs.transcribe, "transcribe", side_effect=recognize) as asr, \
                patch.object(jobs.brain, "find_clips", return_value=([{"start": 0, "end": 10, "hook": "LATE URL"}], {"tokens": 0})), \
                patch.object(render, "render_clip", side_effect=self.encoder):
            jobs._run_job(self.job)
        main_decode = next(call for call in decoded.call_args_list if Path(call.args[1]).name == "audio.wav")
        self.assertEqual(main_decode.kwargs["start"], 1967)
        self.assertEqual(main_decode.kwargs["end"], 2083)
        self.assertEqual(aligned.call_args.args[2], 35)
        self.assertEqual(self.job["clips"][0]["src_offset"], 2002)
        self.assertEqual(len(asr.call_args.args[0]) / 16000, 30)

    def test_cached_interval_revisit_is_the_one_returned_by_polling(self):
        self.probe_mock.return_value["codec"] = "vp9"
        def ready(info):
            aid = info["source"]["attempt_id"]
            attempt = self.job["attempts"][aid]
            entry = self.job["editor_sources"][attempt["asset_token"]]
            path = Path(entry["path"])
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"cached proxy")
            entry["status"] = "ready"
            attempt.update(status="done", outcome="success")
            jobs._finish_attempt(self.job, self.clip, attempt)
            return aid
        first = jobs.editor_info(self.job, self.clip, {"start": 10, "end": 20}, prepare=True)
        aid_a = ready(first)
        second = jobs.editor_info(self.job, self.clip, {"start": 12, "end": 23}, prepare=True)
        aid_b = ready(second)
        revisited = jobs.editor_info(self.job, self.clip, {"start": 10, "end": 20}, prepare=True)
        polled = jobs.editor_info(self.job, self.clip)
        self.assertNotEqual(aid_a, aid_b)
        self.assertEqual(revisited["source"]["attempt_id"], aid_a)
        self.assertEqual(polled["source"]["attempt_id"], aid_a)
        self.assertEqual(polled["source"]["url"], revisited["source"]["url"])
        self.assertNotIn(str(self.folder), json.dumps(jobs.public(self.job)))

    def test_restart_retires_proxy_queue_and_preserves_committed_success(self):
        self.probe_mock.return_value["codec"] = "vp9"
        pending = jobs.editor_info(self.job, self.clip, prepare=True)
        aid = pending["source"]["attempt_id"]
        jobs._load_file(self.folder / "job.json", self.job["user"])
        loaded = jobs.get(self.job["id"])
        loaded_clip = loaded["clips"][0]
        self.assertEqual(jobs.editor_info(loaded, loaded_clip)["source"]["status"], "error")
        retry = jobs.editor_info(loaded, loaded_clip, prepare=True)
        self.assertNotEqual(retry["source"]["attempt_id"], aid)
        jobs.cancel_attempt(loaded["id"], loaded_clip["id"], retry["source"]["attempt_id"])
        self.job, self.clip = loaded, loaded_clip
        applied = self.attempt({"hook": "COMMITTED"})
        with patch.object(render, "render_clip", side_effect=self.encoder):
            jobs.render_one(self.job, self.clip, [], attempt=applied)
        applied.update(cleanup_pending=True, worker_active=True)
        jobs.save(self.job)
        jobs._load_file(self.folder / "job.json", self.job["user"])
        restored = jobs.get(self.job["id"])["clips"][0]
        self.assertEqual(restored["attempt"]["status"], "done")
        self.assertEqual(restored["attempt"]["outcome"], "success")
        self.assertEqual(restored["published_attempt"], applied["id"])

    def test_queued_cancellation_retires_physical_tickets_and_controls(self):
        fair = jobs._FairQueue()
        with patch.object(jobs, "_q", fair):
            for n in range(40):
                attempt = self.attempt({"hook": f"Attempt {n}"})
                self.assertEqual(len(fair.quick), 1)
                jobs.cancel_attempt(self.job["id"], self.clip["id"], attempt["id"])
                self.assertEqual(len(fair.quick), 0)
                self.assertNotIn(attempt["id"], jobs._controls)
            self.assertTrue(jobs.delete(self.job["id"]))
            self.assertFalse(fair.quick)

    def test_shared_source_cache_never_returns_another_clips_polling_identity(self):
        self.probe_mock.return_value["codec"] = "vp9"
        first = jobs.editor_info(self.job, self.clip, prepare=True)
        other = jobs._clip_settings(self.clip)
        other.update(id="clip02", status="done", version=1, file="clip02.mp4")
        self.job["clips"].append(other)
        second = jobs.editor_info(self.job, other, prepare=True)
        self.assertTrue(second["source"]["shared"])
        self.assertNotIn("attempt_id", second["source"])
        self.assertEqual(jobs.editor_info(self.job, other)["source"].get("attempt_id"), None)
        self.assertEqual(second["source"]["status"], first["source"]["status"])


if __name__ == "__main__":
    try:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(AttemptTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if result.wasSuccessful():
            for name in unittest.defaultTestLoader.getTestCaseNames(AttemptTests):
                print("PASS " + name)
        sys.exit(0 if result.wasSuccessful() else 1)
    finally:
        shutil.rmtree(DATA, ignore_errors=True)
