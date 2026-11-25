"""Deterministic selection/English-ASR regressions. No providers, model files or inference.

Run directly with Python; all app storage is temporary and every external boundary is mocked.
"""
import atexit
import asyncio
import json
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

_original_env = {k: v for k, v in os.environ.items() if k.startswith(("QLIPIT_", "PEAKCUT_"))}
for _key in _original_env:
    os.environ.pop(_key)
_data = tempfile.TemporaryDirectory(prefix="pc-selection-asr-")
os.environ.update(QLIPIT_LOAD_DOTENV="0", QLIPIT_DATA=_data.name, DATABASE_URL="",
                  QLIPIT_FETCH_MODEL="0", QLIPIT_UPDATE_CHECK="0", QLIPIT_ASR_COPIES="1",
                  QLIPIT_ASR_THREADS="1", QLIPIT_AI_PROVIDER="openai", QLIPIT_AI_MODELS="model-a,model-b",
                  QLIPIT_AI_KEY="mock-key-only", QLIPIT_JOB_WORKERS="2")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi import HTTPException  # noqa: E402
from studio import ai, asr_worker, brain, config, db, fetch_model, transcribe  # noqa: E402
_new_impl = transcribe._new


def _cleanup():
    db.engine.dispose()
    _data.cleanup()
    for key in list(os.environ):
        if key.startswith(("QLIPIT_", "PEAKCUT_")):
            os.environ.pop(key)
    os.environ.update(_original_env)


atexit.register(_cleanup)
CLIP = {"start": 1, "end": 20, "score": 80, "hook": "A TRUE HOOK", "title": "A complete explanation",
        "description": "The setup and payoff are spoken.", "hashtags": ["explanation"], "reason": "Complete thought."}


def speech(count=160, start=0.0):
    return [{"w": f"word{i}" + ("." if i % 8 == 7 else ""), "s": start + i * 0.5,
             "e": start + i * 0.5 + 0.4} for i in range(count)]


class Reply:
    def __init__(self, status, data):
        self.status_code, self.data, self.text = status, data, json.dumps(data)

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")


def chat_reply(value):
    return Reply(200, {"choices": [{"message": {"content": json.dumps(value)}}],
                       "usage": {"prompt_tokens": 12, "completion_tokens": 3}})


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(ai.httpx, "post", side_effect=AssertionError("No real HTTP in regressions")).start()
        patch.object(fetch_model, "ensure", side_effect=AssertionError("No model downloads in regressions")).start()
        patch.object(transcribe, "_new", side_effect=AssertionError("No real recognizers in regressions")).start()
        patch.object(brain.usage, "record").start()
        patch.object(ai, "note_problem").start()
        patch.object(ai, "clear_problem").start()
        patch.object(ai, "save", side_effect=AssertionError("No user configuration writes in regressions")).start()
        patch.object(transcribe, "_free", queue.LifoQueue()).start()
        patch.object(transcribe, "_made", 0).start()


class ProviderTests(IsolatedTest):
    def test_null_items_is_not_a_valid_abstention(self):
        ai.httpx.post.return_value = chat_reply({"items": None})
        ai.httpx.post.side_effect = None
        with self.assertRaises(ai.AIError):
            ai.call("model-a", "Pick clips", brain.SCHEMA)

    def test_empty_native_and_wrapped_arrays_are_valid_abstentions(self):
        for value in ([], {"items": []}):
            with self.subTest(value=value):
                ai.httpx.post.side_effect = None
                ai.httpx.post.return_value = chat_reply(value)
                self.assertEqual(ai.call("model-a", "Pick clips", brain.SCHEMA), ([], 12, 3))

    def test_complete_schema_rejects_missing_wrong_and_mixed_fields(self):
        bad = [dict(CLIP, score="80"), dict(CLIP, hashtags="tag"), dict(CLIP, start=True),
               {k: v for k, v in CLIP.items() if k != "hook"}, None, "clip"]
        for value in bad:
            with self.subTest(value=value):
                ai.httpx.post.side_effect = None
                ai.httpx.post.return_value = chat_reply({"items": [value]})
                with self.assertRaises(ai.AIError):
                    ai.call("model-a", "Pick clips", brain.SCHEMA)

    def test_nonfinite_provider_numbers_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                ai.httpx.post.side_effect = None
                ai.httpx.post.return_value = chat_reply([dict(CLIP, end=value)])
                with self.assertRaises(ai.AIError):
                    ai.call("model-a", "Pick clips", brain.SCHEMA)

    def test_malformed_provider_envelope_raises_adapter_error(self):
        for data in ({"choices": None}, {"choices": []}, {"choices": [{"message": {"content": None}}]}):
            with self.subTest(data=data):
                ai.httpx.post.side_effect = None
                ai.httpx.post.return_value = Reply(200, data)
                with self.assertRaises(ai.AIError):
                    ai.call("model-a", "Pick clips", brain.SCHEMA)

    def test_bad_json_inside_prose_raises_adapter_error(self):
        with self.assertRaises(ai.AIError):
            ai._parse('Here: [{"start": broken}]')

    def test_truncated_provider_answers_cannot_be_mistaken_for_abstention(self):
        cases = [("openai", {"choices": [{"finish_reason": "length", "message": {"content": "[]"}}]}),
                 ("gemini", {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "[]"}]}}]}),
                 ("anthropic", {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "[]"}]})]
        for provider, data in cases:
            with self.subTest(provider=provider), patch.object(ai, "_from_dashboard", return_value={"provider": provider, "models": ["model-a"], "key": "mock-key"}):
                ai.httpx.post.side_effect = None
                ai.httpx.post.return_value = Reply(200, data)
                with self.assertRaises(ai.AIError):
                    ai.call("model-a", "Pick clips", brain.SCHEMA)

    def test_known_billed_tokens_survive_truncated_ordered_fallback(self):
        bad = {"choices": [{"finish_reason": "length", "message": {"content": "[]"}}],
               "usage": {"prompt_tokens": 9, "completion_tokens": 5}}
        ai.httpx.post.side_effect = [Reply(200, bad), chat_reply([CLIP])]
        out, model, spent = brain.ask_ai("Pick clips")
        self.assertEqual((out, model, spent), ([CLIP], "model-b", 29))
        self.assertEqual(brain.usage.record.call_count, 2)

    def test_all_provider_envelopes_and_token_usage_are_checked(self):
        cases = [("openai", {"choices": [{"message": {"content": "[]"}}], "usage": {"prompt_tokens": -1}}),
                 ("openai", {"choices": [{"message": {"content": "[]"}}], "usage": [1]}),
                 ("gemini", {"candidates": [{"content": {"parts": [None]}}]}),
                 ("anthropic", {"content": [None]})]
        for provider, data in cases:
            with self.subTest(provider=provider, data=data), patch.object(ai, "_from_dashboard", return_value={"provider": provider, "models": ["model-a"], "key": "mock-key"}):
                ai.httpx.post.side_effect = None
                ai.httpx.post.return_value = Reply(200, data)
                with self.assertRaises(ai.AIError):
                    ai.call("model-a", "Pick clips", brain.SCHEMA)

    def test_object_and_caption_schemas_reject_wrong_complete_shapes(self):
        for schema, value in ((brain.VOICEOVER_SCHEMA, {"script": None}), (brain.VOICEOVER_SCHEMA, {}),
                              (brain.FIX_SCHEMA, ["good", None])):
            with self.subTest(schema=schema, value=value):
                ai.httpx.post.side_effect = None
                ai.httpx.post.return_value = chat_reply({"items": value})
                with self.assertRaises(ai.AIError):
                    ai.call("model-a", "JSON", schema)

    def test_only_transient_errors_are_recoverable(self):
        for error in (ai.httpx.LocalProtocolError("bad request"), ai.httpx.UnsupportedProtocol("bad URL"),
                      ai.AIError("HTTP 501 unavailable method"), ai.AITimeoutError("deadline")):
            with self.subTest(error=error):
                self.assertFalse(ai.recoverable(error))
        self.assertTrue(ai.recoverable(ai.httpx.ConnectError("connection reset")))
        self.assertTrue(ai.recoverable(ai.AIError("HTTP 429 rate limited")))

    def test_all_fallbacks_share_one_wall_time_deadline(self):
        clock = [10.0]

        def call(*_args, **kwargs):
            self.assertEqual(kwargs["deadline"], 10.25)
            clock[0] = 10.3
            raise ai.AIError("HTTP 503 overloaded")

        patch.object(config, "ai_ready", return_value=True).start()
        patch.object(ai, "models", return_value=["a", "b", "c"]).start()
        invoke = patch.object(ai, "call", side_effect=call).start()
        patch.object(brain.time, "monotonic", side_effect=lambda: clock[0]).start()
        patch.dict(os.environ, QLIPIT_AI_SECONDS="0.25").start()
        with self.assertRaises(RuntimeError):
            brain.ask_ai("Pick clips")
        self.assertEqual(invoke.call_count, 1)

    def test_inflight_call_cannot_mix_new_provider_address_or_key(self):
        saved = {"provider": "openai", "models": ["chosen-a"], "base_url": "https://chosen.invalid/v1", "key": "old-mock-key"}
        original = ai._openai

        def changed(*args):
            saved.update(provider="custom", models=["chosen-b"], base_url="https://changed.invalid/v1", key="new-mock-key")
            return original(*args)

        patch.object(ai, "_from_dashboard", return_value=saved).start()
        patch.object(ai, "_openai", side_effect=changed).start()
        ai.httpx.post.side_effect = None
        ai.httpx.post.return_value = chat_reply([CLIP])
        ai.call("chosen-a", "Pick clips", brain.SCHEMA)
        sent = ai.httpx.post.call_args
        self.assertEqual(sent.args[0], "https://chosen.invalid/v1/chat/completions")
        self.assertEqual(sent.kwargs["headers"]["Authorization"], "Bearer old-mock-key")

    def test_ordered_fallbacks_keep_the_same_readonly_provider_snapshot(self):
        saved = {"provider": "openai", "models": ["chosen-a", "chosen-b"],
                 "base_url": "https://chosen.invalid/v1", "key": "mock-key"}
        patch.object(ai, "_from_dashboard", return_value=saved).start()
        patch.object(config, "ai_ready", return_value=True).start()

        def call(model, *_args, **kwargs):
            self.assertEqual(kwargs["settings"]["provider"], "openai")
            if model == "chosen-a":
                saved.update(provider="custom", base_url="https://changed.invalid/v1", key="different-mock-key")
                return None, 1, 1
            return [CLIP], 1, 1

        patch.object(ai, "call", side_effect=call).start()
        self.assertEqual(brain.ask_ai("Pick clips")[1], "chosen-b")

    def test_prompt_and_output_are_bounded(self):
        ai.httpx.post.side_effect = None
        ai.httpx.post.return_value = chat_reply([CLIP])
        with self.assertRaises(ai.AIError):
            ai.call("model-a", "x" * 200000, brain.SCHEMA)
        ai.httpx.post.assert_not_called()
        with self.assertRaises(ai.AIError):
            ai._parse('["' + "x" * 200000 + '"]')

    def test_generic_bad_request_does_not_retry_capability_variants(self):
        ai.httpx.post.side_effect = None
        ai.httpx.post.return_value = Reply(400, {"error": {"message": "bad model id"}})
        with self.assertRaises(ai.AIError):
            ai.call("model-a", "Pick clips", brain.SCHEMA)
        self.assertEqual(ai.httpx.post.call_count, 1)

    def test_only_the_explicitly_refused_json_capability_is_removed(self):
        ai.httpx.post.side_effect = [Reply(422, {"error": {"message": "response_format json_object is not supported"}}),
                                   chat_reply({"items": [CLIP]})]
        self.assertEqual(ai.call("model-a", "Pick clips", brain.SCHEMA)[0], [CLIP])
        sent = [c.kwargs["json"] for c in ai.httpx.post.call_args_list]
        self.assertEqual(len(sent), 2)
        self.assertNotIn("response_format", sent[1])
        self.assertEqual(sent[1]["temperature"], 0.4)
        self.assertLessEqual(sent[1]["max_tokens"], 8192)

    def test_malformed_answers_follow_configured_order_not_same_model_retries(self):
        patch.object(config, "ai_ready", return_value=True).start()
        patch.object(ai, "models", return_value=["chosen-a", "chosen-b"]).start()
        call = patch.object(ai, "call", side_effect=[(None, 2, 1), ([CLIP], 4, 2)]).start()
        out, model, _ = brain.ask_ai("Pick clips")
        self.assertEqual((out, model), ([CLIP], "chosen-b"))
        self.assertEqual([c.args[0] for c in call.call_args_list], ["chosen-a", "chosen-b"])

    def test_retry_budget_is_bounded_and_reaches_ordered_fallbacks(self):
        patch.object(config, "ai_ready", return_value=True).start()
        patch.object(ai, "models", return_value=["a", "b", "c", "d", "e"]).start()
        call = patch.object(ai, "call", side_effect=ai.AIError("HTTP 503 temporarily overloaded")).start()
        sleep = patch.object(brain.time, "sleep").start()
        with self.assertRaises(RuntimeError):
            brain.ask_ai("Pick clips")
        self.assertLessEqual(call.call_count, 6)
        self.assertEqual(list(dict.fromkeys(c.args[0] for c in call.call_args_list)), ["a", "b", "c", "d", "e"])
        self.assertLessEqual(sum(c.args[0] for c in sleep.call_args_list), 12)

    def test_nonrecoverable_errors_are_not_retried(self):
        patch.object(config, "ai_ready", return_value=True).start()
        patch.object(ai, "models", return_value=["a", "b"]).start()
        call = patch.object(ai, "call", side_effect=ai.AIError("HTTP 401 refused key")).start()
        sleep = patch.object(brain.time, "sleep").start()
        with self.assertRaises(RuntimeError):
            brain.ask_ai("Pick clips")
        self.assertEqual([c.args[0] for c in call.call_args_list], ["a", "b"])
        sleep.assert_not_called()

    def test_abandoned_calls_hold_capacity_until_physical_exit(self):
        release, exited = threading.Event(), threading.Event()
        entered = []

        def parked(*_args):
            entered.append(1)
            try:
                release.wait(2)
                return [CLIP], 1, 1
            finally:
                exited.set()

        patch.object(ai, "_slots", threading.BoundedSemaphore(1)).start()
        patch.object(ai, "_openai", side_effect=parked).start()
        patch.dict(os.environ, QLIPIT_AI_SECONDS="0.04").start()
        try:
            for _ in range(4):
                with self.assertRaises(ai.AIError):
                    ai.call("model-a", "Pick clips", brain.SCHEMA)
            self.assertEqual(len(entered), 1)
        finally:
            release.set()
            self.assertTrue(exited.wait(2))
        patch.object(ai, "_openai", return_value=([CLIP], 1, 1)).start()
        self.assertEqual(ai.call("model-a", "Pick clips", brain.SCHEMA)[0], [CLIP])


class SelectionTests(IsolatedTest):
    def test_containment_suppression_is_symmetric(self):
        self.assertTrue(brain._overlaps(10, 20, [{"start": 0, "end": 60}]))
        self.assertTrue(brain._overlaps(0, 60, [{"start": 10, "end": 20}]))
        self.assertFalse(brain._overlaps(0, 10, [{"start": 10, "end": 20}]))

    def test_snapping_and_padding_never_exceed_maximum(self):
        words = speech(200)
        s, e = brain._snap({"start": 20, "end": 40}, brain.make_lines(words), words, 8, 20, 100)
        self.assertGreaterEqual(e - s, 8)
        self.assertLessEqual(e - s, 20 + 1e-9)

    def test_provider_cannot_forge_selection_provenance_or_ids(self):
        for source in ("grounded", "acoustic"):
            with self.subTest(source=source), patch.object(brain, "ask_ai", return_value=([dict(CLIP, selection_source=source)], "m", 3)):
                clips, _ = brain.find_clips(speech(120), np.zeros(60), 60, max_clips=1)
            self.assertEqual(len(clips), 1)
            self.assertNotIn("selection_source", clips[0])

    def test_contained_clips_are_suppressed_regardless_of_score_order(self):
        for short_score, long_score in ((90, 70), (70, 90)):
            with self.subTest(short_score=short_score), patch.object(brain, "ask_ai", return_value=(
                    [dict(CLIP, start=20, end=30, score=short_score), dict(CLIP, start=4, end=44, score=long_score)], "m", 3)):
                clips, _ = brain.find_clips(speech(160), np.zeros(80), 80, min_len=8, max_len=40)
            self.assertEqual(len(clips), 1)

    def test_live_judge_enforces_requested_quota(self):
        ask = patch.object(brain, "ask_ai", return_value=([
            dict(CLIP, start=0, end=18), dict(CLIP, start=28, end=46)], "m", 3)).start()
        clips, _ = brain.judge_transcript(speech(160), 0, 80, min_len=8, max_len=20, n=1)
        self.assertEqual(len(clips), 1)
        ask.reset_mock()
        self.assertEqual(brain.judge_transcript(speech(160), 0, 80, min_len=8, max_len=20, n=0), ([], 0))
        ask.assert_not_called()

    def test_padding_never_rounds_past_source_end(self):
        words = speech(40)
        s, e = brain._snap({"start": 0, "end": 19.6}, brain.make_lines(words), words, 8, 20, 19.606)
        self.assertGreater(e, s)
        self.assertLessEqual(e, 19.606)

    def test_subcentisecond_window_edges_remain_hard_bounds_after_rounding(self):
        a, b = 10.0000000001, 29.9999999999
        words = brain._selection_words(speech(80, a), a, b)
        s, e = brain._snap({"start": a, "end": b - 0.1}, brain.make_lines(words), words, 8, 20, b, a)
        self.assertGreater(e, s)
        self.assertGreaterEqual(s, a)
        self.assertLessEqual(e, b)

    def test_live_transcript_cannot_select_outside_analyzed_window(self):
        patch.object(brain, "ask_ai", return_value=([dict(CLIP, start=0, end=20)], "m", 2)).start()
        clips, _ = brain.judge_transcript(speech(120, 100), 100, 160, min_len=8, max_len=20)
        self.assertEqual(clips, [])

    def test_live_recent_transcript_cannot_snap_before_its_first_word(self):
        patch.object(brain, "ask_ai", return_value=([dict(CLIP, start=100, end=119)], "m", 2)).start()
        clips, _ = brain.find_live_clips(speech(120, 100), np.zeros(160), 160, min_len=8, max_len=20)
        self.assertEqual(len(clips), 1)
        self.assertGreaterEqual(clips[0]["start"], 100)
        self.assertLessEqual(clips[0]["end"] - clips[0]["start"], 20 + 1e-9)

    def test_null_or_malformed_answer_triggers_labeled_acoustic_fallback(self):
        for value in (None, {"items": None}, [dict(CLIP, end=1000)], [CLIP, None]):
            with self.subTest(value=value):
                with patch.object(brain, "ask_ai", return_value=(value, "bad", 3)):
                    clips, info = brain.find_clips(speech(120), np.zeros(60), 60, max_clips=1, min_len=8, max_len=20)
                self.assertEqual(len(clips), 1)
                self.assertIsNone(info["model"])
                self.assertIn("acoustic", info["note"].lower())
                self.assertIn("loudness", clips[0]["reason"].lower())

    def test_valid_abstention_is_not_overridden_by_acoustic_fallback(self):
        patch.object(brain, "ask_ai", return_value=([], "chosen", 3)).start()
        fallback = patch.object(brain, "fallback_by_loudness").start()
        clips, info = brain.find_clips(speech(120), np.zeros(60), 60)
        self.assertEqual(clips, [])
        self.assertEqual(info["model"], "chosen")
        fallback.assert_not_called()

    def test_quiet_coherent_speech_is_not_rejected_by_energy(self):
        patch.object(brain, "ask_ai", return_value=([CLIP], "chosen", 3)).start()
        clips, _ = brain.find_clips(speech(120), np.full(60, -75.0), 60, max_clips=1)
        self.assertEqual(len(clips), 1)

    def test_zero_quota_does_not_call_provider_or_select_one_clip(self):
        ask = patch.object(brain, "ask_ai", return_value=([CLIP], "chosen", 3)).start()
        clips, _ = brain.find_clips(speech(120), np.zeros(60), 60, max_clips=0)
        self.assertEqual(clips, [])
        ask.assert_not_called()

    def test_selection_rejects_nonfinite_or_reversed_configuration(self):
        for kwargs in ({"duration": float("nan")}, {"duration": -1}, {"duration": 60, "min_len": 0},
                       {"duration": 60, "min_len": 30, "max_len": 20}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                brain.find_clips(speech(120), np.zeros(60), **kwargs)

    def test_signal_judge_rejects_invented_or_nonfinite_times(self):
        for item in ({"start": 0, "end": 110, "score": 90, "reason": "r"},
                     {"start": 105, "end": float("nan"), "score": 90, "reason": "r"}):
            with self.subTest(item=item), patch.object(brain, "ask_ai", return_value=([item], "m", 3)):
                with self.assertRaises(ai.AIError):
                    brain.judge_signals(np.zeros(20), None, 100, 120)

    def test_grounded_candidate_ids_cover_bounded_context_and_resolve_locally(self):
        words = speech(120)
        segments, candidates = brain._candidate_context(brain.make_lines(words), words, 0, 60, 8, 20)
        self.assertTrue(candidates)
        self.assertEqual(len({c["id"] for c in candidates}), len(candidates))
        known = {s["id"] for s in segments}
        for candidate in candidates:
            self.assertTrue(set(candidate["segment_ids"]) <= known)
            self.assertTrue(0 <= candidate["start"] < candidate["end"] <= 60)
            self.assertTrue(8 <= candidate["end"] - candidate["start"] <= 20 + 1e-9)
        candidate = candidates[0]
        answer = dict(CLIP, candidate_id=candidate["id"], start=9999, end=99999,
                      setup_segment_id=candidate["segment_ids"][0], payoff_segment_id=candidate["segment_ids"][-1],
                      evidence_segment_ids=candidate["segment_ids"])
        patch.object(brain, "ask_ai", return_value=([answer], "chosen", 3)).start()
        with patch.dict(os.environ, QLIPIT_SELECTION_PROMPT="grounded"):
            clips, _ = brain.find_clips(words, np.full(60, -75.0), 60, max_clips=1, min_len=8, max_len=20)
        self.assertEqual((clips[0]["start"], clips[0]["end"]), (candidate["start"], candidate["end"]))

    def test_grounded_unknown_ids_follow_model_fallback(self):
        words = speech(120)
        _, candidates = brain._candidate_context(brain.make_lines(words), words, 0, 60, 8, 20)
        c = candidates[0]
        good = dict(CLIP, candidate_id=c["id"], setup_segment_id=c["segment_ids"][0],
                    payoff_segment_id=c["segment_ids"][-1], evidence_segment_ids=c["segment_ids"])
        patch.object(config, "ai_ready", return_value=True).start()
        patch.object(ai, "models", return_value=["chosen-a", "chosen-b"]).start()
        call = patch.object(ai, "call", side_effect=[([dict(good, candidate_id="invented")], 1, 1), ([good], 1, 1)]).start()
        with patch.dict(os.environ, QLIPIT_SELECTION_PROMPT="grounded"):
            clips, info = brain.find_clips(words, np.zeros(60), 60, max_clips=1, min_len=8, max_len=20)
        self.assertEqual(info["model"], "chosen-b")
        self.assertEqual(len(clips), 1)
        self.assertEqual([x.args[0] for x in call.call_args_list], ["chosen-a", "chosen-b"])

    def test_legacy_prompt_is_default_and_selectable_rollback(self):
        ask = patch.object(brain, "ask_ai", return_value=([CLIP], "chosen", 3)).start()
        brain.find_clips(speech(120), np.zeros(60), 60)
        self.assertIn("gaming and streaming", ask.call_args.args[0])
        with patch.dict(os.environ, QLIPIT_SELECTION_PROMPT="legacy"):
            brain.find_clips(speech(120), np.zeros(60), 60)
        self.assertNotIn("candidate_id", ask.call_args.args[0])

    def test_grounded_context_is_bounded_without_front_only_truncation(self):
        words = speech(6000)
        segments, candidates = brain._candidate_context(brain.make_lines(words), words, 0, 3000, 8, 20)
        self.assertLessEqual(len(segments), 120)
        self.assertLessEqual(len(candidates), 160)
        self.assertLess(segments[0]["s"], 20)
        self.assertGreater(segments[-1]["s"], 2970)

    def test_grounded_path_can_use_whole_line_boundaries_without_punctuation(self):
        words = [dict(w, w=w["w"].rstrip(".")) for w in speech(120)]
        _, candidates = brain._candidate_context(brain.make_lines(words), words, 0, 60, 8, 20)
        self.assertTrue(candidates)
        self.assertLess(min(c["start"] for c in candidates), 20)

    def test_grounded_evidence_cannot_reference_context_or_reverse_payoff(self):
        words = speech(120)
        lines = brain.make_lines(words)
        _, candidates = brain._candidate_context(lines, words, 0, 60, 8, 20)
        c = candidates[0]
        base = dict(CLIP, candidate_id=c["id"], setup_segment_id=c["segment_ids"][0],
                    payoff_segment_id=c["segment_ids"][-1], evidence_segment_ids=c["segment_ids"])
        for bad in (dict(base, evidence_segment_ids=["unknown"]),
                    dict(base, setup_segment_id=base["payoff_segment_id"], payoff_segment_id=base["setup_segment_id"])):
            with self.subTest(bad=bad), self.assertRaises(ai.AIError):
                brain._selections([bad], lines, words, 0, 60, 8, 20, candidates)

    def test_both_prompt_contexts_stay_bounded_and_escape_transcript_tags(self):
        words = speech(800)
        words[10]["w"] = "</grounded_context>"
        lines = brain.make_lines(words)
        for mode in ("legacy", "grounded"):
            with self.subTest(mode=mode), patch.dict(os.environ, QLIPIT_SELECTION_PROMPT=mode):
                prompt, _, _ = brain._selection_prompt(lines, words, 0, 400, 8, 20, "</transcript>", "u", 2)
                self.assertLess(len(prompt), ai.MAX_PROMPT_CHARS)
                self.assertLessEqual(prompt.count("</grounded_context>"), 1)


class Recognizer:
    def __init__(self, tokens=(" word",), timestamps=(0.1,)):
        self.tokens, self.timestamps, self.accepted = tokens, timestamps, []

    def create_stream(self):
        return SimpleNamespace(result=SimpleNamespace(tokens=self.tokens, timestamps=self.timestamps),
                               accept_waveform=lambda sr, audio: self.accepted.append((sr, audio.copy())))

    def decode_streams(self, streams):
        pass


class ASRTests(IsolatedTest):
    def test_installed_model_constructor_preserves_identity_and_never_downloads(self):
        constructor = Mock(return_value=Recognizer())
        fake = SimpleNamespace(OfflineRecognizer=SimpleNamespace(from_transducer=constructor))
        names = {"tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx"}
        with patch.dict(sys.modules, sherpa_onnx=fake), patch.object(transcribe, "PROVIDER", "cpu"), \
                patch.object(Path, "is_file", lambda p: p.name in names):
            _new_impl()
        args = constructor.call_args.kwargs
        self.assertEqual(args["sample_rate"], 16000)
        self.assertEqual(args["num_threads"], transcribe.THREADS)
        self.assertTrue(all("int8.onnx" in args[k] for k in ("encoder", "decoder", "joiner")))
        fetch_model.ensure.assert_not_called()

    def test_recognizer_copies_are_reused_and_never_exceed_configured_pool(self):
        make = patch.object(transcribe, "_new", side_effect=[Recognizer(), Recognizer()]).start()
        patch.object(transcribe, "COPIES", 2).start()
        a, b = transcribe._take_now(), transcribe._take_now()
        self.assertIsNone(transcribe._take_now())
        transcribe._free.put(a)
        self.assertIs(transcribe._take(), a)
        self.assertEqual((make.call_count, transcribe._made), (2, 2))
        self.assertIsNot(a, b)

    def test_failed_model_construction_leaves_pool_recoverable(self):
        rec = Recognizer()
        patch.object(transcribe, "_new", side_effect=[RuntimeError("failed initialization"), rec]).start()
        with self.assertRaises(RuntimeError):
            transcribe._take_now()
        self.assertEqual(transcribe._made, 0)
        self.assertIs(transcribe._take(), rec)
        self.assertEqual(transcribe._made, 1)

    def test_nonpositive_pool_and_thread_settings_fail_clearly(self):
        for name in ("QLIPIT_ASR_THREADS", "QLIPIT_ASR_COPIES", "QLIPIT_ASR_BATCH", "QLIPIT_ASR_REMOTE_PARALLEL"):
            for value in ("0", "-1", "broken"):
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                    with self.assertRaises(ValueError):
                        transcribe._positive(name, 1)

    def test_zero_pool_cannot_block_on_an_empty_queue(self):
        patch.object(transcribe, "COPIES", 0).start()
        patch.object(transcribe._free, "get", side_effect=AssertionError("must not wait forever")).start()
        with self.assertRaises(ValueError):
            transcribe._take()

    def test_matching_int8_suite_is_preserved_and_mixed_precision_is_not_ready(self):
        d = Path("mock-installed-parakeet")
        complete = {"tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx",
                    "encoder.onnx", "decoder.onnx", "joiner.onnx"}
        with patch.object(transcribe, "PROVIDER", "cpu"), patch.object(Path, "is_file", lambda p: p.name in complete):
            self.assertTrue(all("int8.onnx" in v for v in transcribe._model_files(d).values()))
        mixed = {"tokens.txt", "encoder.int8.onnx", "decoder.onnx", "joiner.fp16.onnx"}
        with patch.object(config, "ASR_MODEL_DIR", d), patch.object(fetch_model, "ready", return_value=True), \
                patch.object(Path, "is_file", lambda p: p.name in mixed):
            self.assertFalse(transcribe.model_ready())

    def test_input_pcm_shape_scale_and_sample_rate_are_validated(self):
        for audio in (np.zeros((16000, 2), dtype=np.int16), np.zeros(16000, dtype=np.float32),
                      np.zeros(16000, dtype=np.int32)):
            with self.subTest(shape=audio.shape, dtype=audio.dtype), self.assertRaises(ValueError):
                transcribe.transcribe(audio)
        with self.assertRaises(ValueError):
            transcribe.transcribe(np.zeros(16000, dtype=np.int16), sample_rate=8000)

    def test_recognizer_input_requires_finite_normalized_mono_float(self):
        rec = Recognizer()
        for seg in (np.zeros((16000, 2), dtype=np.float32), np.full(16000, np.nan, dtype=np.float32),
                    np.full(16000, 1.5, dtype=np.float32), np.zeros(16000, dtype=np.int16)):
            with self.subTest(shape=seg.shape, dtype=seg.dtype), self.assertRaises(ValueError):
                transcribe.decode_pieces([seg], rec)
        self.assertEqual(rec.accepted, [])

    def test_result_lengths_and_timestamp_domain_are_validated(self):
        cases = [([" word", " next"], [0.1]), ([" word"], [float("nan")]), ([" word"], [-0.1]),
                 ([" word"], [1.01]), ([" first", " next"], [0.8, 0.2]), ([1], [0.1]), ([" word"], [True])]
        for tokens, timestamps in cases:
            with self.subTest(tokens=tokens, timestamps=timestamps), self.assertRaises(ValueError):
                transcribe._words(tokens, timestamps, 0, 16000)

    def test_every_word_end_is_piece_bound_even_before_final_refinement(self):
        words = transcribe._words([" first", " second"], [0.91, 0.99], 0, 16000)
        self.assertTrue(all(0 <= w["s"] < w["e"] <= 1 for w in words))

    def test_subwords_spaces_punctuation_and_fractional_source_ends_stay_coherent(self):
        words = transcribe._words([" hello", "world", " ", " again", ".", " last"],
                                  [0.05, 0.08, 0.1, 0.2, 0.25, 0.999], 16001, 32003)
        self.assertEqual([w["w"] for w in words], ["helloworld", "again.", "last"])
        self.assertTrue(all(16001 / 16000 <= w["s"] < w["e"] <= 32003 / 16000 for w in words))
        self.assertTrue(all(a["e"] <= b["s"] for a, b in zip(words, words[1:])))

    def test_invalid_result_releases_borrowed_recognizer(self):
        rec = Recognizer([" word"], [float("nan")])
        with self.assertRaises(ValueError):
            transcribe.decode_pieces([np.zeros(16000, dtype=np.float32)], rec)
        self.assertIs(transcribe._free.get_nowait(), rec)

    def test_chunk_seams_and_final_word_remain_source_bound_after_refinement(self):
        audio = np.full(32000, 1000, dtype=np.int16)
        patch.object(transcribe, "_chunks", return_value=iter([(0, 16000), (16000, 32000)])).start()
        patch.object(transcribe, "_take_now", side_effect=[Recognizer([" first"], [0.98]),
                                                          Recognizer([" last"], [0.98])]).start()
        words = transcribe.transcribe(audio)
        self.assertEqual([w["w"] for w in words], ["first", "last"])
        self.assertLessEqual(words[0]["e"], 1)
        self.assertLessEqual(words[-1]["e"], len(audio) / 16000)
        self.assertTrue(all(w["s"] < w["e"] for w in words))

    def test_quiet_nonsilent_pcm_is_not_discarded_only_for_low_rms(self):
        audio = (np.sin(np.arange(32000) * 0.2) * 16).astype(np.int16)
        rec = Recognizer([" quiet", " explanation."], [0.1, 0.7])
        patch.object(transcribe, "_take_now", return_value=rec).start()
        words = transcribe.transcribe(audio)
        self.assertEqual([w["w"] for w in words], ["quiet", "explanation."])
        np.testing.assert_array_equal(rec.accepted[0][1], audio.astype(np.float32) / 32768)

    def test_digital_silence_does_not_borrow_or_create_a_model(self):
        take = patch.object(transcribe, "_take_now").start()
        self.assertEqual(transcribe.transcribe(np.zeros(32000, dtype=np.int16)), [])
        take.assert_not_called()

    def test_chunk_scan_uses_only_a_bounded_audio_span(self):
        audio = np.zeros(16000 * 90 + 123, dtype=np.int16)
        sizes, real_sqrt = [], np.sqrt

        def sqrt(values):
            sizes.append(len(values))
            return real_sqrt(values)

        with patch.object(transcribe.np, "sqrt", side_effect=sqrt):
            chunks = list(transcribe._chunks(audio))
        self.assertEqual((chunks[0][0], chunks[-1][1]), (0, len(audio)))
        self.assertTrue(all(a[1] == b[0] for a, b in zip(chunks, chunks[1:])))
        self.assertLessEqual(max(sizes), 480)

    def test_gain_is_benchmark_only_off_by_default_and_does_not_mutate_audio(self):
        audio = (np.sin(np.arange(32000) * 0.2) * 100).astype(np.int16)
        original = audio.copy()
        default, experiment = Recognizer(), Recognizer()
        with patch.object(transcribe, "_take_now", return_value=default), \
                patch.dict(os.environ, QLIPIT_ASR_BENCHMARK_GAIN="1"):
            transcribe.transcribe(audio)
        with patch.object(transcribe, "_take_now", return_value=experiment):
            transcribe.transcribe(audio, benchmark_gain=True)
        np.testing.assert_array_equal(audio, original)
        np.testing.assert_array_equal(default.accepted[0][1], audio.astype(np.float32) / 32768)
        self.assertGreater(np.max(np.abs(experiment.accepted[0][1])), np.max(np.abs(default.accepted[0][1])))
        self.assertLessEqual(np.max(np.abs(experiment.accepted[0][1])), 1)

    def test_invalid_remote_timestamps_recover_locally_instead_of_publishing_nan(self):
        patch.object(transcribe, "REMOTE", "https://remote.invalid").start()
        patch.object(transcribe, "_remote_down_until", 0).start()
        ai.httpx.post.side_effect = None
        ai.httpx.post.return_value = Reply(200, {"pieces": [{"tokens": [" bad"], "timestamps": [float("nan")]}]})
        patch.object(transcribe, "_take_now", return_value=Recognizer([" local"], [0.1])).start()
        words = transcribe.transcribe(np.full(32000, 1000, dtype=np.int16))
        self.assertEqual([w["w"] for w in words], ["local"])
        self.assertGreater(transcribe._remote_down_until, time.time())


class Request:
    def __init__(self, headers, chunks):
        self.headers, self.chunks, self.read_count = headers, chunks, 0

    async def body(self):
        return b"".join(self.chunks)

    async def stream(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk


class WorkerTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        patch.object(asr_worker, "TOKEN", "mock-shared-secret").start()

    def request(self, **headers):
        return Request({"authorization": "Bearer mock-shared-secret", "x-lens": "16000", **headers},
                       [b"\0\0" * 16000])

    def test_bad_length_header_rate_and_channels_are_client_errors(self):
        for headers in ({"content-length": "broken"}, {"content-length": "-1"}, {"x-lens": "1,,2"},
                        {"x-sample-rate": "8000"}, {"x-channels": "2"}):
            with self.subTest(headers=headers), self.assertRaises(HTTPException) as error:
                asyncio.run(asr_worker.decode(self.request(**headers)))
            self.assertEqual(error.exception.status_code, 400)

    def test_non_ascii_unauthorized_header_is_rejected_not_a_server_error(self):
        with self.assertRaises(HTTPException) as error:
            asyncio.run(asr_worker.decode(self.request(authorization="Bearer \u00e9")))
        self.assertEqual(error.exception.status_code, 401)

    def test_oversized_or_unauthorized_requests_are_rejected_before_body_reads(self):
        for headers, status in (({"authorization": "wrong"}, 401), ({"x-lens": "640001"}, 400),
                                ({"content-length": str(asr_worker.MAX_BODY + 1)}, 413)):
            req = self.request(**headers)
            with self.subTest(headers=headers), self.assertRaises(HTTPException) as error:
                asyncio.run(asr_worker.decode(req))
            self.assertEqual(error.exception.status_code, status)
            self.assertEqual(req.read_count, 0)

    def test_chunked_body_is_rejected_before_unbounded_accumulation(self):
        req = Request({"authorization": "Bearer mock-shared-secret", "x-lens": "1"}, [b"x" * 100, b"more"])
        with self.assertRaises(HTTPException):
            asyncio.run(asr_worker.decode(req))
        self.assertEqual(req.read_count, 1)

    def test_worker_uses_shared_recognizer_and_preserves_pcm_scaling(self):
        rec = Recognizer([" word"], [0.1])
        patch.object(transcribe, "_take", return_value=rec).start()
        got = asyncio.run(asr_worker.decode(self.request()))
        self.assertEqual(got, {"pieces": [{"tokens": [" word"], "timestamps": [0.1]}]})
        self.assertEqual(rec.accepted[0][0], 16000)
        self.assertEqual(rec.accepted[0][1].dtype, np.float32)

    def test_invalid_recognizer_result_cannot_leave_worker(self):
        patch.object(transcribe, "decode_pieces", return_value=[([" word"], [float("nan")])]).start()
        with self.assertRaises(HTTPException) as error:
            asyncio.run(asr_worker.decode(self.request()))
        self.assertEqual(error.exception.status_code, 502)


if __name__ == "__main__":
    unittest.main(verbosity=2)
