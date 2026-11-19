"""Unit and integration tests for Voiceover & AI Script Studio."""
import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import soundfile as sf
import numpy as np

from studio import brain, jobs, render, voice


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
    # When no AI is set up, suggest_voiceover provides clean fallback
    words = [{"w": "Hey", "s": 0.0, "e": 0.4}, {"w": "guys", "s": 0.4, "e": 0.8},
             {"w": "look", "s": 0.8, "e": 1.2}, {"w": "at", "s": 1.2, "e": 1.4},
             {"w": "this", "s": 1.4, "e": 1.8}, {"w": "play", "s": 1.8, "e": 2.2}]
    res = brain.suggest_voiceover(words, duration=10.0, hook="INSANE PLAY", title="Epic clutch")
    assert isinstance(res, str)
    assert len(res) > 0


def test_polish_voiceover_fallback():
    draft = "So basically um like I was trying to do this trick and then it happened"
    res = brain.polish_voiceover(draft, duration=15.0)
    assert isinstance(res, str)
    assert len(res) > 0


def test_request_render_with_voiceover():
    # Setup dummy job and clip
    jid = "test-job-vo"
    cid = "clip01"
    clip = {
        "id": cid, "start": 10.0, "end": 25.0, "hook": "BIG MOMENT",
        "title": "Test Title", "description": "Test Desc",
        "style": "bounce", "layout": "blur", "status": "done"
    }
    job = {
        "id": jid, "user": "local", "kind": "video", "creator": None,
        "dir": tempfile.mkdtemp(), "created": 1000.0, "source": {"type": "file"},
        "options": {}, "status": "done", "clips": [clip], "duration": 60.0
    }
    jobs._jobs[jid] = job
    jobs._by_user["local"] = {jid: job}

    jobs.request_render(jid, cid, {
        "voiceover": {
            "enabled": True,
            "mode": "mute",
            "voice": "en-US-BrianNeural",
            "script": "This was an incredible moment in the stream.",
            "rate": "+0%"
        }
    })

    updated = next(c for c in job["clips"] if c["id"] == cid)
    assert updated["status"] == "queued"
    assert updated["voiceover"]["enabled"] is True
    assert updated["voiceover"]["mode"] == "mute"
    assert updated["voiceover"]["voice"] == "en-US-BrianNeural"
    assert updated["voiceover"]["script"] == "This was an incredible moment in the stream."


def test_voice_sample_generation():
    sample_file = voice.sample_audio("en-US-AndrewNeural")
    assert sample_file.is_file()
    assert sample_file.stat().st_size > 500


def test_voice_synthesis_and_word_timestamps():
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        out_path = Path(tmp.name)
    try:
        script = "QlipIt Studio brings your best gaming moments to life."
        words = voice.generate(script, "en-US-AndrewNeural", out_path)
        assert out_path.is_file()
        assert out_path.stat().st_size > 1000
        assert len(words) >= 5
        for w in words:
            assert "w" in w
            assert "s" in w and "e" in w
            assert w["e"] >= w["s"]
    finally:
        out_path.unlink(missing_ok=True)


def test_render_clip_with_voiceover():
    import subprocess
    work_dir = Path(tempfile.mkdtemp(prefix="pk_vo_test_"))
    dummy_src = work_dir / "dummy.mp4"
    out_dir = work_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Create 3-second test video with video and audio streams
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=640x360:d=3",
           "-f", "lavfi", "-i", "sine=f=440:d=3", "-c:v", "libx264", "-c:a", "aac", str(dummy_src)]
    subprocess.run(cmd, check=True)

    # Render with voiceover (mute mode)
    clip = {
        "start": 0.0,
        "end": 3.0,
        "hook": "VOICEOVER READY",
        "layout": "crop",
        "style": "clean",
        "voiceover": {
            "enabled": True,
            "mode": "mute",
            "voice": "en-US-AndrewNeural",
            "script": "Voiceover is now fully integrated into QlipIt Studio.",
            "rate": "+0%"
        }
    }
    render.install_fonts()
    res = render.render_clip(dummy_src, out_dir, "vo_clip_1", clip, words=[], src_w=640, src_h=360)

    assert (out_dir / "vo_clip_1.mp4").is_file()
    assert (out_dir / "vo_clip_1.mp4").stat().st_size > 10000
    assert (out_dir / "vo_clip_1.jpg").is_file()
    assert (out_dir / "vo_clip_1.ass").is_file()

    ass_text = (out_dir / "vo_clip_1.ass").read_text(encoding="utf-8")
    assert "VOICEOVER READY" in ass_text
    assert "QLIPIT" in ass_text.upper() or "VOICEOVER" in ass_text.upper()

    # Clean up
    import shutil
    shutil.rmtree(work_dir, ignore_errors=True)


def test_voice_api_endpoints():
    import time
    from fastapi.testclient import TestClient
    from studio import auth, db
    from studio.server import app

    db.init()
    user, _ = auth._create(f"voice-{time.time_ns()}@example.com", "Voice test", password_hash="x")
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.cookies.set(auth.COOKIE, auth.start_session(user.id))
    origin = {"Origin": "http://127.0.0.1:8765"}

    # 1. Test voice listing
    r = client.get("/api/voice/voices")
    assert r.status_code == 200
    voices = r.json()
    assert len(voices) >= 5
    assert any(v["name"] == "Andrew" for v in voices)

    # 2. Test sample endpoint
    r = client.get("/api/voice/sample/en-US-AndrewNeural")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/mpeg")
    assert len(r.content) > 500

    # 3. Test preview endpoint
    r = client.post("/api/voice/preview", json={
        "text": "Testing preview",
        "voice": "en-US-AndrewNeural"
    }, headers=origin)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/mpeg")
    assert len(r.content) > 500


if __name__ == "__main__":
    test_list_voices()
    test_voice_info_fallback()
    test_script_cleaning()
    test_suggest_voiceover_fallback()
    test_polish_voiceover_fallback()
    test_request_render_with_voiceover()
    test_voice_sample_generation()
    test_voice_synthesis_and_word_timestamps()
    test_render_clip_with_voiceover()
    test_voice_api_endpoints()
    print("ALL VOICE TESTS PASSED!")
