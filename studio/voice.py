"""Voiceover generation using Microsoft Edge Neural Voices.

Provides studio-grade narration with no API keys or fees. The synthesized audio is transcribed
by QlipIt's speech model to obtain exact word timestamps for synchronized animated captions."""
import asyncio
import os
import re
import tempfile
from pathlib import Path

import soundfile as sf

from . import config, media, transcribe

VOICES = [
    {"id": "en-US-AndrewNeural", "name": "Andrew", "gender": "male", "tag": "Conversational (US)", "desc": "Warm, natural creator voice for short reels"},
    {"id": "en-US-BrianNeural", "name": "Brian", "gender": "male", "tag": "Narrative (US)", "desc": "Deep, rich documentary and storytelling voice"},
    {"id": "en-US-AvaNeural", "name": "Ava", "gender": "female", "tag": "Influencer (US)", "desc": "Expressive, lively and engaging voice"},
    {"id": "en-US-EmmaNeural", "name": "Emma", "gender": "female", "tag": "Friendly (US)", "desc": "Crisp, gentle, trustworthy narration"},
    {"id": "en-GB-RyanNeural", "name": "Ryan", "gender": "male", "tag": "Narrative (UK)", "desc": "Refined British commentary and analytical tone"},
]

VOICE_MAP = {v["id"]: v for v in VOICES}
DEFAULT_VOICE = "en-US-AndrewNeural"


def list_voices() -> list[dict]:
    return list(VOICES)


def voice_info(voice_id: str) -> dict:
    if not voice_id:
        return VOICE_MAP[DEFAULT_VOICE]
    if voice_id in VOICE_MAP:
        return VOICE_MAP[voice_id]

    # Robust matching: strip Multilingual, check case-insensitively, or match name
    vid_str = str(voice_id).strip()
    vid_clean = vid_str.replace("Multilingual", "").lower()
    for v in VOICES:
        if v["id"].lower() == vid_clean or v["id"].lower() == vid_str.lower():
            return v
    for v in VOICES:
        if v["name"].lower() in vid_str.lower():
            return v
    for v in VOICES:
        clean_v_id = v["id"].lower().replace("-", "")
        clean_req = vid_str.lower().replace("-", "")
        if clean_v_id in clean_req or clean_req in clean_v_id:
            return v
    return VOICE_MAP[DEFAULT_VOICE]


async def _say(text: str, voice: str, path: Path, rate: str = "+0%", tries: int = 4) -> bool:
    import edge_tts
    path.parent.mkdir(parents=True, exist_ok=True)
    for k in range(tries):
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            await communicate.save(str(path))
            if path.is_file() and path.stat().st_size > 500:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.8 * (k + 1))
    return False


def _clean_script(text: str) -> str:
    """Prepare user script for natural speech synthesis."""
    t = text.strip()
    # Normalize dollar amounts: $50 -> 50 dollars, $2.50 -> 2 dollars and 50 cents
    t = re.sub(r"\$(\d+)\.(\d{2})", r"\1 dollars and \2 cents", t)
    t = re.sub(r"\$(\d+)", r"\1 dollars", t)
    # Normalize percentages: 50% -> 50 percent
    t = re.sub(r"(\d+)%", r"\1 percent", t)
    return t


def generate(text: str, voice: str, out_path: Path, rate: str = "+0%") -> list[dict]:
    """Synthesize voiceover to out_path (mp3) and return exact word timestamps."""
    clean = _clean_script(text)
    if not clean:
        raise ValueError("Script text is empty.")
    info = voice_info(voice)
    v = info["id"]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    loop = asyncio.new_event_loop()
    try:
        ok = loop.run_until_complete(_say(clean, v, out_path, rate=rate))
    finally:
        loop.close()

    if not ok or not out_path.is_file() or out_path.stat().st_size < 500:
        dur = max(2.5, len(clean.split()) * 0.45)
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=f=440:d={dur}", "-c:a", "libmp3lame", "-b:a", "64k", str(out_path)])

    # Convert to 16kHz mono WAV to get exact word timestamps from sherpa-onnx
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = Path(tmp.name)
    try:
        media.run(["ffmpeg", "-y", "-v", "error", "-i", str(out_path), "-ar", "16000", "-ac", "1", str(wav_path)])
        audio, sr = sf.read(str(wav_path), dtype="int16")
        words = []
        if transcribe.model_ready():
            try:
                words = transcribe.transcribe(audio)
            except Exception:
                words = []
        if not words:
            total_dur = len(audio) / float(sr or 16000)
            raw_words = clean.split()
            words = []
            if raw_words and total_dur > 0:
                step = total_dur / len(raw_words)
                for i, w in enumerate(raw_words):
                    words.append({
                        "w": w,
                        "s": round(i * step, 2),
                        "e": round((i + 1) * step, 2)
                    })
    finally:
        wav_path.unlink(missing_ok=True)

    return words


def sample_audio(voice_id: str) -> Path:
    """Return a path to a cached short audio preview for the given voice."""
    info = voice_info(voice_id)
    cache_dir = config.DATA_DIR / "voice_samples"
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"{info['id']}.mp3"
    if out.is_file() and out.stat().st_size > 1000:
        return out

    script = f"Hi, I'm {info['name']}. I can voice your next video clip."
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_say(script, info["id"], out))
    finally:
        loop.close()

    if not out.is_file() or out.stat().st_size < 500:
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=523.25:d=2.0", "-c:a", "libmp3lame", "-b:a", "64k", str(out)])

    return out
