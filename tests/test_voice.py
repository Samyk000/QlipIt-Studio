"""Offline voice tests: real audio/API/render plumbing, deterministic TTS and ASR fixtures."""
import atexit
import asyncio
import copy
import json
import math
import os
import sys
import tempfile
import traceback
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import soundfile as sf
import numpy as np

from tests.run import _block_external_network, _isolated_env

# Also safe when run directly, without tests/run.py or its throwaway server.
_data = tempfile.TemporaryDirectory(prefix="pc-voice-")
atexit.register(_data.cleanup)
_env = _isolated_env(Path(_data.name), 8765)
os.environ.clear()
os.environ.update(_env)
tempfile.tempdir = _data.name
_block_external_network()

from studio import brain, config, ctx, jobs, media, render, voice


@contextmanager
def _speech_fixture(words=None):
    """Serve a valid local MP3 at the edge-tts boundary, never a real provider or model."""
    with tempfile.TemporaryDirectory(prefix="tts-") as folder:
        wav, mp3 = Path(folder) / "fixture.wav", Path(folder) / "fixture.mp3"
        pcm = (np.sin(np.arange(38400) * (2 * np.pi * 660 / 16000)) * 4096).astype(np.int16)
        sf.write(str(wav), pcm, 16000, subtype="PCM_16")
        media.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav), "-c:a", "libmp3lame",
                   "-b:a", "64k", str(mp3)], timeout=30)
        audio = mp3.read_bytes()
        assert len(audio) > 1000
        communicate = Mock()
        communicate.return_value.save = AsyncMock(side_effect=lambda path: Path(path).write_bytes(audio))
        with patch("edge_tts.Communicate", communicate), \
                patch.object(voice.transcribe, "model_ready", return_value=words is not None), \
                patch.object(voice.transcribe, "transcribe", return_value=copy.deepcopy(words)) as recognize:
            yield audio, communicate, recognize


def test_list_voices():
    voices = voice.list_voices()
    assert len(voices) >= 5
    ids = [v["id"] for v in voices]
    assert "en-US-AndrewNeural" in ids
    assert "en-US-BrianNeural" in ids
    assert "en-US-AvaNeural" in ids
    assert "en-US-EmmaNeural" in ids
    assert "en-GB-RyanNeural" in ids

    for v in voices:
        assert v["name"]
        assert v["tag"]
        assert v["desc"]


def test_voice_info_fallback():
    v = voice.voice_info("en-US-AndrewNeural")
    assert v["name"] == "Andrew"

    fb = voice.voice_info("unknown-voice-id")
    assert fb["id"] == voice.DEFAULT_VOICE


def test_script_cleaning():
    cleaned = voice._clean_script("  Save $50 on this deal with 20% discount!  ")
    assert "$50" not in cleaned
    assert "50 dollars" in cleaned
    assert "20 percent" in cleaned


def test_suggest_voiceover_fallback():
    words = [{"w": "Hey", "s": 0.0, "e": 0.4}, {"w": "guys", "s": 0.4, "e": 0.8},
             {"w": "look", "s": 0.8, "e": 1.2}, {"w": "at", "s": 1.2, "e": 1.4},
             {"w": "this", "s": 1.4, "e": 1.8}, {"w": "play", "s": 1.8, "e": 2.2}]
    with patch.object(brain, "ask_ai", side_effect=RuntimeError("Offline fixture: AI unavailable")) as ask:
        res = brain.suggest_voiceover(words, duration=10.0, hook="INSANE PLAY", title="Epic clutch")
    assert res.startswith("INSANE PLAY")
    assert 18 <= len(res.split()) <= 27
    assert ask.call_args.kwargs["purpose"] == "Voiceover script suggestion"
    ask.assert_called_once()


def test_polish_voiceover_fallback():
    draft = "So basically um like I was trying to do this trick and then it happened"
    with patch.object(brain, "ask_ai", side_effect=RuntimeError("Offline fixture: AI unavailable")) as ask:
        res = brain.polish_voiceover(draft, duration=15.0)
    assert res.startswith(draft)
    assert 27 <= len(res.split()) <= 40
    assert ask.call_args.kwargs["purpose"] == "Voiceover script polish"
    ask.assert_called_once()


def test_voiceover_script_provider_responses():
    script = "  Watch this clean finish. https://example.invalid/advert  "
    with patch.object(brain, "ask_ai", return_value=({"script": script}, "fixture-model", 5)) as ask:
        assert brain.suggest_voiceover([], 10.0, hook="THE FINISH") == "Watch this clean finish."
        assert ask.call_args.args[1] is brain.VOICEOVER_SCHEMA
        assert "10.0-second" in ask.call_args.args[0]
        assert brain.polish_voiceover("Watch this", 10.0) == "Watch this clean finish."
        assert ask.call_args.args[1] is brain.VOICEOVER_SCHEMA
        assert ask.call_count == 2
        assert brain.polish_voiceover("  ", 10.0) == ""
        assert ask.call_count == 2


def test_request_render_with_voiceover():
    jid = "test-job-vo"
    cid = "clip01"
    clip = {
        "id": cid, "start": 10.0, "end": 25.0, "hook": "BIG MOMENT",
        "title": "Test Title", "description": "Test Desc",
        "style": "bounce", "layout": "blur", "status": "done",
        "file": "published.mp4", "cover": "published.jpg", "version": 4, "artifact_revision": 4,
        "voiceover": {"enabled": True, "mode": "duck", "voice": "en-US-AvaNeural",
                      "script": "Last successful narration.", "rate": "-5%"}
    }
    published = copy.deepcopy(clip)
    changes = {
        "voiceover": {
            "enabled": True,
            "mode": "mute",
            "voice": "en-US-BrianNeural",
            "script": "This was an incredible moment in the stream.",
            "rate": "+0%"
        }
    }
    with tempfile.TemporaryDirectory(prefix="job-") as folder:
        job = {
            "id": jid, "user": "u_voice01", "kind": "video", "creator": None,
            "dir": folder, "created": 1000.0, "source": {"type": "file"},
            "options": {}, "status": "done", "clips": [clip], "duration": 60.0
        }
        source = {"path": Path(folder) / "source.mp4", "meta": {"width": 640, "height": 360,
                  "duration": 60.0, "audio": True}, "offset": 0.0, "start": 0.0, "end": 60.0}
        with ctx.as_user("u_voice01"), patch.object(jobs, "_jobs", {jid: job}), \
                patch.object(jobs, "_by_user", {"u_voice01": {jid: job}}), \
                patch.object(jobs, "_controls", {}), patch.object(jobs, "_q") as queued, \
                patch.object(jobs, "save"), patch.object(jobs, "resolve_clip_source", return_value=source):
            result = jobs.request_render(jid, cid, changes)
            attempt = job["attempts"][result["attempt"]["id"]]
            assert result["attempt"]["status"] == "queued" and result["attempt"]["kind"] == "render"
            assert result["attempt"]["revision"] == 5
            assert attempt["settings"]["voiceover"] == changes["voiceover"]
            assert all(clip[k] == v for k, v in published.items())
            assert "settings" not in result["attempt"]
            queued.put.assert_called_once_with(("attempt", jid, attempt["id"]))
            changes["voiceover"]["script"] = "Changed by the caller after submission."
            assert attempt["settings"]["voiceover"]["script"] == "This was an incredible moment in the stream."
            assert clip["voiceover"] == published["voiceover"]
            # A visual-only retry inherits the last successful narration, not the cancelled draft.
            attempt.update(status="cancelled", outcome="cancelled")
            retry = jobs.request_render(jid, cid, {"hook": "EDITED HEADLINE"})
            inherited = job["attempts"][retry["attempt"]["id"]]["settings"]["voiceover"]
            assert inherited == published["voiceover"]
            clip["voiceover"]["script"] = "Changed after the retry was submitted."
            assert inherited["script"] == published["voiceover"]["script"]


def test_voice_sample_generation():
    with tempfile.TemporaryDirectory(prefix="samples-") as folder, \
            patch.object(config, "DATA_DIR", Path(folder)), _speech_fixture() as (audio, communicate, recognize):
        sample_file = voice.sample_audio("en-US-AndrewNeural")
        assert sample_file.read_bytes() == audio
        assert sample_file.parent == Path(folder) / "voice_samples"
        communicate.assert_called_once_with("Hi, I'm Andrew. I can voice your next video clip.",
                                            voice.DEFAULT_VOICE, rate="+0%")
        assert voice.sample_audio("unknown-voice-id") == sample_file
        communicate.assert_called_once()
        sample_file.write_bytes(b"incomplete")
        assert voice.sample_audio(voice.DEFAULT_VOICE).read_bytes() == audio
        assert communicate.call_count == 2
        recognize.assert_not_called()


def test_voice_synthesis_and_word_timestamps():
    script = "QlipIt Studio brings your best gaming moments to life."
    expected = [{"w": w, "s": round(0.08 + i * 0.2, 2), "e": round(0.18 + i * 0.2, 2)}
                for i, w in enumerate(script.split())]
    with tempfile.TemporaryDirectory(prefix="synthesis-") as folder, \
            _speech_fixture(expected) as (audio, communicate, recognize):
        out_path = Path(folder) / "voice.mp3"
        wav_files = set(Path(tempfile.gettempdir()).glob("*.wav"))
        words = voice.generate(script, "Andrew Multilingual", out_path, rate="+15%")
        assert out_path.read_bytes() == audio
        assert words == expected
        communicate.assert_called_once_with(script, voice.DEFAULT_VOICE, rate="+15%")
        recognize.assert_called_once()
        pcm = recognize.call_args.args[0]
        assert pcm.dtype == np.int16 and pcm.ndim == 1
        assert len(pcm) == 38400
        assert set(Path(tempfile.gettempdir()).glob("*.wav")) == wav_files


def test_voice_timing_without_asr():
    script = "Save $50 with 20% discount"
    clean = "Save 50 dollars with 20 percent discount"
    for model_words in (None, []):
        with tempfile.TemporaryDirectory(prefix="timing-") as folder, \
                _speech_fixture(model_words) as (audio, communicate, recognize):
            out_path = Path(folder) / "voice.mp3"
            words = voice.generate(script, "unknown-voice-id", out_path)
            assert out_path.read_bytes() == audio
            assert [w["w"] for w in words] == clean.split()
            assert words[0]["s"] == 0.0 and abs(words[-1]["e"] - 2.4) <= 0.01
            for i, word in enumerate(words):
                assert math.isfinite(word["s"]) and math.isfinite(word["e"])
                assert 0 <= word["s"] < word["e"] <= 2.41
                if i:
                    assert word["s"] == words[i - 1]["e"]
            communicate.assert_called_once_with(clean, voice.DEFAULT_VOICE, rate="+0%")
            assert recognize.call_count == (0 if model_words is None else 1)


def test_voice_timing_when_asr_fails():
    with tempfile.TemporaryDirectory(prefix="timing-error-") as folder, \
            _speech_fixture([]) as (audio, communicate, recognize):
        recognize.side_effect = RuntimeError("Offline fixture: recognizer failed")
        words = voice.generate("Keep these words", voice.DEFAULT_VOICE, Path(folder) / "voice.mp3")
        assert words == [{"w": "Keep", "s": 0.0, "e": 0.8}, {"w": "these", "s": 0.8, "e": 1.6},
                         {"w": "words", "s": 1.6, "e": 2.4}]
        recognize.assert_called_once()
        communicate.assert_called_once()


def test_voice_rejects_empty_script():
    with tempfile.TemporaryDirectory(prefix="empty-") as folder, patch("edge_tts.Communicate") as communicate:
        out_path = Path(folder) / "empty.mp3"
        try:
            voice.generate("  ", voice.DEFAULT_VOICE, out_path)
        except ValueError as error:
            assert "empty" in str(error).lower()
        else:
            raise AssertionError("An empty script must be rejected")
        assert not out_path.exists()
        communicate.assert_not_called()


def test_voice_say_retries_and_rejects_short_audio():
    with tempfile.TemporaryDirectory(prefix="retry-") as folder, \
            _speech_fixture() as (audio, communicate, recognize), \
            patch.object(voice.asyncio, "sleep", new_callable=AsyncMock) as delay:
        out_path = Path(folder) / "retry.mp3"
        attempts = []
        async def save(path):
            attempts.append(path)
            if len(attempts) == 1:
                raise RuntimeError("Offline fixture: transient synthesis failure")
            Path(path).write_bytes(audio)
        communicate.return_value.save.side_effect = save
        assert asyncio.run(voice._say("retry", voice.DEFAULT_VOICE, out_path, rate="-10%", tries=2)) is True
        assert out_path.read_bytes() == audio
        assert communicate.call_count == 2
        delay.assert_awaited_once_with(0.8)
        communicate.reset_mock()
        delay.reset_mock()
        communicate.return_value.save.side_effect = lambda path: Path(path).write_bytes(b"too short")
        assert asyncio.run(voice._say("short", voice.DEFAULT_VOICE, out_path, tries=2)) is False
        assert communicate.call_count == 2 and delay.await_count == 2
        recognize.assert_not_called()


def test_render_clip_with_voiceover():
    expected = [{"w": "Voiceover", "s": 0.12, "e": 0.4}, {"w": "stays", "s": 0.6, "e": 0.83},
                {"w": "in", "s": 1.03, "e": 1.23}, {"w": "sync.", "s": 1.54, "e": 2.26}]
    clip = {
        "start": 1.0,
        "end": 4.0,
        "hook": "VOICEOVER READY",
        "layout": "crop",
        "style": "clean",
        "voiceover": {
            "enabled": True,
            "mode": "mute",
            "voice": "en-US-AndrewNeural",
            "script": "Voiceover stays in sync.",
            "rate": "+0%"
        }
    }
    with tempfile.TemporaryDirectory(prefix="render-") as folder, \
            _speech_fixture(expected) as (audio, communicate, recognize), patch.object(render, "_qsv_ok", False):
        work_dir = Path(folder)
        dummy_src, out_dir = work_dir / "dummy.mp4", work_dir / "out"
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=640x360:d=4",
                   "-f", "lavfi", "-i", "sine=f=440:d=4", "-c:v", "libx264", "-c:a", "aac", str(dummy_src)], timeout=30)
        render.install_fonts()
        original_words = [{"w": "Original", "s": 1.0, "e": 1.4}]
        for mode in ("mute", "duck"):
            name = f"vo_clip_{mode}"
            clip["voiceover"]["mode"] = mode
            res = render.render_clip(dummy_src, out_dir, name, clip, original_words, src_w=640, src_h=360)
            assert res["file"] == name + ".mp4" and res["cover"] == name + ".jpg"
            assert (out_dir / (name + ".mp4")).stat().st_size > 10000
            assert (out_dir / (name + ".jpg")).stat().st_size > 0
            assert (out_dir / (name + "_vo.mp3")).read_bytes() == audio
            ass = (out_dir / (name + ".ass")).read_text(encoding="utf-8")
            assert "VOICEOVER READY" in ass and "Original" not in ass
            assert any(",0:00:00.12," in line and line.endswith("Voiceover") for line in ass.splitlines())
            metadata = json.loads(media.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                                             "-of", "json", str(out_dir / (name + ".mp4"))], timeout=30))
            assert abs(float(metadata["format"]["duration"]) - 3.0) < 0.2
            assert any(s["codec_type"] == "audio" and s["codec_name"] == "aac" for s in metadata["streams"])
            assert any(s["codec_type"] == "video" and (s["width"], s["height"]) == (1080, 1920)
                       for s in metadata["streams"])
        assert communicate.call_count == 2 and recognize.call_count == 2


def test_voice_api_endpoints():
    from fastapi.testclient import TestClient
    from studio import auth, db
    from studio.server import app

    db.init()
    user, _ = auth._create("voice-test@example.com", "Voice test", password_hash="x")
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.cookies.set(auth.COOKIE, auth.start_session(user.id))
    origin = {"Origin": "http://127.0.0.1:8765"}

    try:
        with _speech_fixture() as (audio, communicate, recognize):
            r = client.get("/api/voice/voices")
            assert r.status_code == 200
            voices = r.json()
            assert len(voices) >= 5 and any(v["name"] == "Andrew" for v in voices)
            communicate.assert_not_called()
            r = client.get("/api/voice/sample/en-US-AndrewNeural")
            assert r.status_code == 200 and r.headers["content-type"].startswith("audio/mpeg")
            assert r.content == audio
            assert client.get("/api/voice/sample/en-US-AndrewNeural").content == audio
            communicate.assert_called_once()
            r = client.post("/api/voice/preview", json={
                "text": "Testing preview", "voice": "en-US-BrianMultilingualNeural", "rate": "-10%"
            }, headers=origin)
            assert r.status_code == 200 and r.headers["content-type"].startswith("audio/mpeg")
            assert r.content == audio
            communicate.assert_called_with("Testing preview", "en-US-BrianNeural", rate="-10%")
            assert communicate.call_count == 2
            assert not Path(communicate.return_value.save.call_args.args[0]).exists()
            for text in ("", "x" * 1201):
                assert client.post("/api/voice/preview", json={"text": text}, headers=origin).status_code == 400
            assert client.post("/api/voice/preview", json={"text": "Foreign origin"},
                               headers={"Origin": "https://evil.invalid"}).status_code == 403
            assert communicate.call_count == 2
            recognize.assert_not_called()
    finally:
        client.close()
        db.engine.dispose()


if __name__ == "__main__":
    failed = []
    for name, test in list(globals().items()):
        if not name.startswith("test_") or not callable(test):
            continue
        try:
            test()
            print("PASS " + name, flush=True)
        except Exception:
            failed.append(name)
            print("FAIL " + name, flush=True)
            traceback.print_exc()
    print("FAILED: " + ", ".join(failed) if failed else "ALL VOICE TESTS PASSED!")
    sys.exit(1 if failed else 0)
