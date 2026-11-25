"""Offline renderer/media regressions; all artifacts live in an isolated temporary folder."""
import atexit
import ast
import copy
import contextlib
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_PARENT = Path(tempfile.gettempdir()) / "kilo"
TEMP = tempfile.TemporaryDirectory(prefix="pk-rv-", dir=TEMP_PARENT if TEMP_PARENT.is_dir() else None)
atexit.register(TEMP.cleanup)
DATA = Path(TEMP.name)
for key in list(os.environ):
    if key.startswith(("PEAKCUT_", "QLIPIT_")):
        del os.environ[key]
os.environ.update({"QLIPIT_LOAD_DOTENV": "0", "PEAKCUT_DATA": str(DATA), "DATABASE_URL": "",
                   "PEAKCUT_FETCH_MODEL": "0", "PEAKCUT_UPDATE_CHECK": "0", "PEAKCUT_AI_KEY": ""})

import numpy as np
import soundfile as sf
from PIL import Image

from studio import config, media, render, voice

FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
WORDS = [{"w": w, "s": 10 + i * 0.4, "e": 10.3 + i * 0.4}
         for i, w in enumerate("one surprising payoff ends here.".split())]
SOURCE = {"width": 640, "height": 360, "coded_width": 640, "coded_height": 360,
          "sar": "1:1", "rotation": 0, "duration": 3.0, "audio": False, "codec": "h264", "pix_fmt": "yuv420p"}
VOICE_CFG = {"enabled": True, "script": "One surprising payoff.", "voice": "en-US-AndrewNeural", "rate": "+0%"}

# Recorded before the renderer changes. Full samples and encoder hashes are only written under DATA.
LEGACY_HASH = "7bae4d55048e2eb7acb99b095050a4492fa5c81b80e5be4e2063365a199dc7b1"


def legacy_samples():
    samples = {}
    for layout in render.LAYOUTS:
        actual, fg_h, hook_y, cap_y = render.geometry(1920, 1080, layout)
        for style in render.STYLES:
            for hook in ("", "Big payoff after a surprising start"):
                key = f"{layout}-{style}-{'hook' if hook else 'no-hook'}"
                ass = render.build_ass(WORDS, 10, 13, hook, style, ["payoff"], hook_y, cap_y)
                samples[key] = {"geometry": [actual, fg_h, hook_y, cap_y],
                                "filter": render._filter(actual, fg_h), "ass": ass}
    return samples


def legacy_hash(samples):
    return hashlib.sha256(json.dumps(samples, sort_keys=True).encode()).hexdigest()


def visual(ratio="1:1", fit="crop", **headline):
    value = {"schema_version": 1, "framing": {"mode": "manual", "ratio": ratio, "fit": fit}}
    if headline:
        value["headline"] = headline
    return value


def make_video(path, size="640x360", duration=0.4, audio=False, extra=None):
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=30:d={duration}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=f=440:d={duration}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac"]
    subprocess.run(cmd + (extra or []) + [str(path)], check=True, capture_output=True)


def record_legacy():
    samples = legacy_samples()
    for key, sample in samples.items():
        (DATA / f"{key}.ass").write_text(sample["ass"], encoding="utf-8")
    manifest = {"recipe_sha256": legacy_hash(samples), "samples": len(samples)}
    if FFMPEG:
        src = DATA / "baseline.mp4"
        make_video(src, audio=True)
        render.install_fonts()
        vo = DATA / "fixture.mp3"
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=550:d=0.4", str(vo)])

        def fake_voice(text, vid, path, rate="+0%"):
            shutil.copyfile(vo, path)
            return [{"w": "one", "s": 0, "e": 0.15}, {"w": "payoff", "s": 0.15, "e": 0.35}]

        with patch.object(render, "_qsv_ok", False), patch.object(voice, "generate", side_effect=fake_voice):
            encoded = {}
            for layout in render.LAYOUTS:
                for style in render.STYLES:
                    for hook in ("", "Big payoff"):
                        name = f"b_{layout}_{style}_{int(bool(hook))}"
                        clip = {"start": 0, "end": 0.4, "layout": layout, "style": style, "hook": hook}
                        render.render_clip(src, DATA / "baseline", name, clip, [], 640, 360)
                        encoded[name] = hashlib.sha256((DATA / "baseline" / f"{name}.mp4").read_bytes()).hexdigest()
            for mode in ("mute", "duck"):
                name = f"b_voice_{mode}"
                clip = {"start": 0, "end": 0.4, "layout": "blur", "hook": "Voice payoff",
                        "voiceover": {**VOICE_CFG, "mode": mode}}
                render.render_clip(src, DATA / "baseline", name, clip, [], 640, 360)
                encoded[name] = hashlib.sha256((DATA / "baseline" / f"{name}.mp4").read_bytes()).hexdigest()
        manifest["encodes"] = encoded
    (DATA / "legacy-hashes.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


class VisualValidationTests(unittest.TestCase):
    def test_owned_sources_keep_python_310_syntax(self):
        for path in (ROOT / "studio" / "render.py", ROOT / "studio" / "media.py", Path(__file__)):
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))

    def test_absent_visual_stays_absent(self):
        self.assertIsNone(render.validate_visual(None))
        self.assertEqual(render.validate_visual({}), {"schema_version": 1})

    def test_inheritance_is_deep_and_explicit_null_is_transparent(self):
        base = render.validate_visual({"headline": {"font": "anton", "position": {"x": 0, "y": 0.3}},
                                       "framing": {"mode": "manual", "ratio": "16:9"}})
        original = copy.deepcopy(base)
        got = render.validate_visual({"headline": {"color": "#123abc", "background": None}}, base)
        self.assertEqual(got["headline"]["font"], "anton")
        self.assertEqual(got["headline"]["position"], {"x": 0, "y": 0.3})
        self.assertEqual(got["headline"]["color"], "#123ABC")
        self.assertIsNone(got["headline"]["background"])
        self.assertEqual(got["framing"]["ratio"], "16:9")
        self.assertEqual(base, original)
        self.assertEqual(render.validate_visual(None, base), base)

    def test_defaults_and_palette_are_rgb(self):
        got = render.validate_visual({"schema_version": 1, "headline": {}})
        self.assertEqual(got["headline"]["font"], "poppins")
        self.assertEqual(got["headline"]["size"], 96)
        self.assertEqual(got["headline"]["color"], "#0E1014")
        self.assertEqual(got["headline"]["background"], {"color": "#FFFFFF", "opacity": 1})
        self.assertAlmostEqual(got["headline"]["width"], 5 / 6)
        catalog = render.visual_catalog()
        self.assertEqual({f["id"] for f in catalog["fonts"]}, {"poppins", "anton", "archivo"})
        self.assertEqual(catalog["styles"]["bounce"]["active"], "#FFF000")
        self.assertEqual(catalog["styles"]["hype"]["active"], "#FF3C3C")
        json.dumps(catalog, allow_nan=False)

    def test_invalid_values_fail_clearly(self):
        invalid = [[], True, {"schema_version": 2}, {"schema_version": True}, {"unknown": 1},
                   {"headline": {"font": "C:/bad.ttf"}}, {"headline": {"size": float("nan")}},
                   {"headline": {"size": 0}}, {"headline": {"color": "&HFFFFFF"}},
                   {"headline": {"background": {"opacity": 1.1}}}, {"headline": {"width": 0}},
                   {"headline": {"position": {"x": -0.1, "y": 0}}},
                   {"headline": {"position": {"x": True, "y": 0}}},
                   {"framing": {"ratio": "4:3"}}, {"framing": {"ratio": "1:1", "mode": "legacy"}},
                   {"framing": {"fit": "stretch"}}, {"framing": {"roi": {"width": 0}}},
                   {"framing": {"roi": {"x": 0.9, "width": 0.5}}}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                render.validate_visual(value)


class GeometryTests(unittest.TestCase):
    def test_legacy_geometry_ignores_sar_as_before_but_manual_is_display_oriented(self):
        for rotation in (0, 90, 270):
            meta = {**SOURCE, "width": 768 if rotation == 0 else 576, "height": 576 if rotation == 0 else 768,
                    "coded_width": 720, "coded_height": 576, "sar": "16:15", "rotation": rotation}
            lw, lh = (720, 576) if rotation == 0 else (576, 720)
            for layout in render.LAYOUTS:
                with self.subTest(layout=layout, rotation=rotation):
                    expected, fg_h, hook_y, caption_y = render.geometry(lw, lh, layout)
                    got = render.resolve_visual({"layout": layout}, meta)
                    self.assertEqual((got["layout"], got["foreground"]["height"], got["hook_y"], got["caption_y"]),
                                     (expected, fg_h, hook_y, caption_y))
            self.assertEqual(render.resolve_visual({"visual": visual("source")}, meta)["source"]["width"], meta["width"])

    def test_manual_long_and_nonroman_captions_keep_safe_centers(self):
        for text in ("extraordinarily" * 8, "\u65e5\u672c\u8a9e"):
            ass = render.build_ass([{"w": text, "s": 0.1, "e": 0.5}], 0, 1, "", "bounce", [], 50, 290,
                                   width=360, height=360, omit_hook=True)
            self.assertIn("\\pos(180,", ass)
            self.assertIn(",Cap,,", ass)
            self.assertNotIn(",Hook,,", ass)

    def test_source_ratio_stays_locked_when_roi_has_a_different_aspect(self):
        for fit in ("crop", "contain"):
            with self.subTest(fit=fit):
                value = visual("source", fit)
                value["framing"]["roi"] = {"x": 0.25, "y": 0, "width": 0.25, "height": 0.75}
                got = render.resolve_visual({"visual": value}, SOURCE)
                self.assertAlmostEqual(got["width"] / got["height"], 640 / 360, delta=0.02)
                if fit == "contain":
                    self.assertEqual(got["crop_pixels"]["width"], 160)
                    self.assertEqual(got["crop_pixels"]["height"], 270)
                else:
                    self.assertAlmostEqual(got["crop_pixels"]["width"] / got["crop_pixels"]["height"], 640 / 360, delta=0.02)

    def test_manual_render_keeps_legacy_layout_choice_outside_geometry(self):
        clip = {"start": 0, "end": 1, "layout": "zoom", "visual": visual()}
        with patch.object(render, "_render_in", return_value={"file": "layout.mp4", "layout": "manual"}):
            result = render.render_clip(Path("s.mp4"), DATA / "out", "layout", clip, [], 640, 360, source_meta=SOURCE)
        self.assertEqual(result["layout"], "zoom")
        self.assertEqual(result["geometry"]["layout"], "manual")

    def test_sar_normalization_does_not_invent_foreground_detail(self):
        for rotation in (0, 90):
            meta = {**SOURCE, "width": 768 if rotation == 0 else 576,
                    "height": 576 if rotation == 0 else 768, "coded_width": 720, "coded_height": 576,
                    "sar": "16:15", "rotation": rotation}
            sample_w, sample_h = (720, 576) if rotation == 0 else (576, 720)
            for ratio in ("9:16", "1:1", "16:9", "source"):
                for fit in ("crop", "contain"):
                    with self.subTest(rotation=rotation, ratio=ratio, fit=fit):
                        got = render.resolve_visual({"visual": visual(ratio, fit)}, meta)
                        crop, fg = got["crop_pixels"], got["foreground"]
                        self.assertLessEqual(fg["width"], crop["width"] * sample_w / meta["width"] + 1)
                        self.assertLessEqual(fg["height"], crop["height"] * sample_h / meta["height"] + 1)

    def test_legacy_recipe_is_byte_identical(self):
        self.assertEqual(legacy_hash(legacy_samples()), LEGACY_HASH)
        self.assertEqual(render.geometry(640, 640, "blur"), ("crop", 1920, 300, 1380))
        got = render.resolve_visual({"layout": "zoom"}, SOURCE)
        self.assertEqual((got["width"], got["height"], got["layout"]), (1080, 1920, "zoom"))
        self.assertIsNone(got["headline"])

    def test_manual_ratios_preserve_aspect_without_upscale(self):
        for ratio, expected in (("9:16", 9 / 16), ("1:1", 1), ("16:9", 16 / 9), ("source", 640 / 360)):
            with self.subTest(ratio=ratio):
                got = render.resolve_visual({"visual": visual(ratio)}, SOURCE)
                crop = got["crop_pixels"]
                self.assertAlmostEqual(got["width"] / got["height"], expected, delta=0.01)
                self.assertAlmostEqual(crop["width"] / crop["height"], got["width"] / got["height"], delta=0.01)
                self.assertLessEqual(got["width"], crop["width"])
                self.assertLessEqual(got["height"], crop["height"])
                self.assertTrue(all(crop[k] % 2 == 0 for k in ("x", "y", "width", "height")))
                self.assertLessEqual(crop["x"] + crop["width"], SOURCE["width"])
                self.assertLessEqual(crop["y"] + crop["height"], SOURCE["height"])
                self.assertIn("reduced_resolution", got["warnings"])
                json.dumps(got, allow_nan=False)

    def test_one_effective_roi_is_reported_and_used(self):
        value = visual("1:1")
        value["framing"]["roi"] = {"x": 0.2, "y": 0.1, "width": 0.5, "height": 0.6}
        got = render.resolve_visual({"visual": value}, SOURCE)
        crop = got["crop_pixels"]
        effective = got["effective_roi"]
        self.assertAlmostEqual(effective["x"], crop["x"] / 640)
        self.assertAlmostEqual(effective["width"], crop["width"] / 640)
        self.assertGreaterEqual(crop["x"], 128)
        self.assertLessEqual(crop["x"] + crop["width"], 448)
        graph = render._visual_filter(got)
        self.assertEqual(graph.count("crop="), 1)
        self.assertIn(f"crop={crop['width']}:{crop['height']}:{crop['x']}:{crop['y']}", graph)
        self.assertNotIn("force_original_aspect_ratio=increase", graph)

    def test_contain_keeps_roi_and_foreground_detail(self):
        got = render.resolve_visual({"visual": visual("9:16", "contain")}, SOURCE)
        self.assertEqual(got["crop_pixels"], {"x": 0, "y": 0, "width": 640, "height": 360})
        self.assertLessEqual(got["foreground"]["width"], 640)
        self.assertLessEqual(got["foreground"]["height"], 360)
        self.assertEqual(render._visual_filter(got).count("crop="), 1)
        self.assertIn("boxblur=", render._visual_filter(got))

    def test_odd_and_tiny_roi_are_handled(self):
        got = render.resolve_visual({"visual": visual("source")}, {**SOURCE, "width": 641, "height": 359})
        self.assertEqual((got["width"] % 2, got["height"] % 2), (0, 0))
        too_small = visual("9:16")
        too_small["framing"]["roi"] = {"x": 0, "y": 0, "width": 0.001, "height": 0.001}
        with self.assertRaisesRegex(ValueError, "small|detail"):
            render.resolve_visual({"visual": too_small}, SOURCE)

    def test_captions_are_canvas_aware_without_changing_animation(self):
        for width, height in ((360, 360), (640, 360), (198, 352)):
            with self.subTest(canvas=(width, height)):
                ass = render.build_ass(WORDS, 10, 13, "", "bounce", [], height * 0.15, height * 0.82,
                                       width=width, height=height, omit_hook=True)
                self.assertIn(f"PlayResX: {width}\nPlayResY: {height}", ass)
                self.assertIn("\\t(0,90,", ass)
                self.assertIn("\\t(90,200,", ass)
                import re
                positions = re.findall(r"\\pos\(([-\d.]+),([-\d.]+)\)", ass)
                self.assertTrue(positions)
                self.assertTrue(all(0 <= float(x) <= width and 0 <= float(y) <= height for x, y in positions))


class HeadlineTests(unittest.TestCase):
    def test_preview_reports_resolution_warnings_without_duplicate_geometry_warnings(self):
        value = visual("1:1", font="poppins")
        _, meta = render.headline_preview("Payoff", value, SOURCE)
        self.assertIn("reduced_resolution", meta["warnings"])
        resolved = render.resolve_visual({"visual": value, "hook": "Payoff"}, SOURCE)
        self.assertEqual(resolved["headline"], meta)
        self.assertEqual(len(resolved["warnings"]), len(set(resolved["warnings"])))

    def test_preview_is_shared_rgba_bitmap_with_fixed_fade(self):
        value = visual("1:1", color="#123ABC", background={"color": "#FF0000", "opacity": 0.5})
        data, meta = render.headline_preview("A real payoff", value, SOURCE)
        image = Image.open(io.BytesIO(data))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.size, (meta["bitmap_width"], meta["bitmap_height"]))
        self.assertEqual((meta["canvas_width"], meta["canvas_height"]), (360, 360))
        self.assertEqual(meta["fade_ms"], 160)
        self.assertEqual(meta["color"], "#123ABC")
        self.assertTrue(any(pixel[:3] == (255, 0, 0) and pixel[3] == 128 for pixel in image.getdata()))
        self.assertTrue(any(pixel[:3] == (18, 58, 188) and pixel[3] == 255 for pixel in image.getdata()))
        resolved = render.resolve_visual({"visual": value, "hook": "A real payoff"}, SOURCE)
        self.assertEqual(resolved["headline"], meta)

    def test_zero_position_clamps_entire_bitmap_and_does_not_change_pixels(self):
        base = visual("1:1", font="archivo", position={"x": 0, "y": 0})
        data, meta = render.headline_preview("Corner headline", base, SOURCE)
        base["headline"]["position"] = {"x": 1, "y": 1}
        second, moved = render.headline_preview("Corner headline", base, SOURCE)
        self.assertEqual(data, second)
        self.assertEqual((meta["x"], meta["y"]), (0, 0))
        self.assertGreater(meta["center"]["x"], 0)
        self.assertLessEqual(moved["x"] + moved["bitmap_width"], moved["canvas_width"])
        self.assertLessEqual(moved["y"] + moved["bitmap_height"], moved["canvas_height"])

    def test_transparent_background_and_long_text_remain_in_frame(self):
        for font in ("poppins", "anton", "archivo"):
            with self.subTest(font=font):
                data, meta = render.headline_preview("A surprising payoff " * 20,
                                                     visual("16:9", font=font, background=None, size=240), SOURCE)
                image = Image.open(io.BytesIO(data))
                self.assertEqual(image.getpixel((0, 0))[3], 0)
                self.assertLessEqual(meta["bitmap_width"], meta["canvas_width"])
                self.assertLessEqual(meta["bitmap_height"], meta["canvas_height"])
                self.assertTrue(meta["warnings"])


class MediaTests(unittest.TestCase):
    def test_json3_duplicate_or_missing_offsets_share_one_segment_interval(self):
        data = {"events": [{"tStartMs": 0, "dDurationMs": 1200,
                            "segs": [{"utf8": "one two", "tOffsetMs": 0}, {"utf8": "three four"}]}]}
        words = media._caption_words(data)
        self.assertEqual([w["w"] for w in words], ["one", "two", "three", "four"])
        self.assertTrue(all(words[i]["s"] < words[i + 1]["s"] for i in range(len(words) - 1)))
        self.assertTrue(all(w["timing"] == "approximate" for w in words))

    def test_malformed_json3_items_cannot_break_valid_events(self):
        data = {"events": [None, {"tStartMs": 0, "segs": [{"utf8": "one two"}, None]},
                            {"tStartMs": "bad", "segs": [{"utf8": "ignored"}]},
                            {"tStartMs": 1000, "dDurationMs": 500, "segs": [{"utf8": "payoff"}]}]}
        words = media._caption_words(data)
        self.assertEqual([w["w"] for w in words], ["one", "two", "payoff"])
        self.assertTrue(all(w["e"] <= 1.5 for w in words))

    def test_json3_timed_multiword_segments_expand_and_stop_at_source_end(self):
        data = {"events": [{"tStartMs": 1000, "dDurationMs": 2000,
                            "segs": [{"utf8": "one two three", "tOffsetMs": 0},
                                     {"utf8": "four five", "tOffsetMs": 900}]}]}
        words = media._caption_words(data, source_end=2.6)
        self.assertEqual([w["w"] for w in words], ["one", "two", "three", "four", "five"])
        self.assertTrue(all(words[i]["s"] < words[i + 1]["s"] for i in range(len(words) - 1)))
        self.assertTrue(all(w["s"] <= w["e"] <= 2.6 for w in words))
        self.assertTrue(all(w["timing"] == "approximate" for w in words))

    def test_json3_end_never_crosses_event_next_word_or_tiny_source_bound(self):
        data = {"events": [{"tStartMs": 0, "dDurationMs": 500,
                            "segs": [{"utf8": "one", "tOffsetMs": 0}, {"utf8": "two", "tOffsetMs": 450}]}]}
        words = media._caption_words(data, source_end=0.47)
        self.assertEqual(words[0]["timing"], "json3")
        self.assertLessEqual(words[0]["e"], words[1]["s"])
        self.assertLessEqual(words[-1]["e"], 0.47)

    def test_probe_uses_sar_rotation_and_optional_audio(self):
        raw = {"streams": [{"codec_type": "video", "width": 720, "height": 576,
                            "sample_aspect_ratio": "16:15", "codec_name": "h264", "pix_fmt": "yuv420p",
                            "side_data_list": [{"side_data_type": "unrelated"}, {"rotation": -90}]}],
               "format": {"duration": "2.5"}}
        with patch.object(media, "run", return_value=json.dumps(raw)):
            got = media.probe(Path("synthetic.mp4"))
            self.assertEqual((got["width"], got["height"]), (576, 768))
            self.assertEqual((got["coded_width"], got["coded_height"]), (720, 576))
            self.assertEqual(got["sar"], "16:15")
            self.assertEqual(got["rotation"], 270)
            self.assertFalse(got["audio"])
            self.assertEqual((got["codec"], got["pix_fmt"]), ("h264", "yuv420p"))
            with self.assertRaisesRegex(RuntimeError, "audio"):
                media.probe(Path("synthetic.mp4"), require_audio=True)

    def test_probe_accepts_rotation_tag_and_rejects_bad_dimensions(self):
        raw = {"streams": [{"codec_type": "video", "width": 640, "height": 360, "tags": {"rotate": "90"}}],
               "format": {"duration": "1"}}
        with patch.object(media, "run", return_value=json.dumps(raw)):
            self.assertEqual(media.probe(Path("s.mp4"))["width"], 360)
        raw["streams"][0]["width"] = 0
        with patch.object(media, "run", return_value=json.dumps(raw)), self.assertRaises(RuntimeError):
            media.probe(Path("s.mp4"))

    def test_extract_span_preserves_old_call_and_reports_known_duration(self):
        with patch.object(media, "run") as run:
            media.extract_audio(Path("s.mp4"), DATA / "a.wav", start=12.5, end=14.5,
                                jid="j", cancel=lambda: None, on_progress=lambda **fields: None)
            cmd = run.call_args.args[0]
            self.assertEqual(float(cmd[cmd.index("-ss") + 1]), 12.5)
            self.assertEqual(float(cmd[cmd.index("-t") + 1]), 2)
            self.assertEqual(run.call_args.kwargs["duration"], 2)
            self.assertEqual(run.call_args.kwargs["jid"], "j")
            media.extract_audio(Path("s.mp4"), DATA / "a.wav")
        for start, end in ((-1, 2), (1, 1), (0, float("nan"))):
            with self.assertRaises(ValueError):
                media.extract_audio(Path("s.mp4"), DATA / "a.wav", start=start, end=end)

    def test_load_audio_reads_bounded_mono_16k_span(self):
        path = DATA / "span.wav"
        sf.write(path, np.arange(48000, dtype=np.int16), 16000, subtype="PCM_16")
        got = media.load_audio(path, start=1, end=1.25)
        self.assertEqual(got.shape, (4000,))
        np.testing.assert_array_equal(got, np.arange(48000, dtype=np.int16)[16000:20000])
        for channels, rate in ((2, 16000), (1, 8000)):
            sf.write(path, np.zeros((16000, channels), dtype=np.int16) if channels == 2 else np.zeros(16000, dtype=np.int16), rate)
            with self.assertRaisesRegex(ValueError, "mono|16000"):
                media.load_audio(path)


class DownloadTests(unittest.TestCase):
    def test_download_keeps_legacy_audio_callback_and_supports_cancel_and_byte_progress(self):
        events = []

        class Downloader:
            def __init__(self, opts):
                self.opts = opts

            def download(self, urls):
                for callback in self.opts.get("progress_hooks", []):
                    callback({"status": "downloading", "downloaded_bytes": 100, "total_bytes": 200})

        with patch.object(media, "_ydl", side_effect=lambda opts: contextlib.nullcontext(Downloader(opts))), \
             patch.object(media, "_biggest", return_value=DATA / "download.mp4"):
            fractions = []
            media.download_audio("https://example.invalid", DATA, fractions.append, jid="test", cancel=lambda: None)
            self.assertEqual(fractions, [0.5])
            media.download("https://example.invalid", DATA, jid="test", cancel=lambda: None,
                           on_progress=lambda **f: events.append(f))
        measured = [f for f in events if f["completed"] == 100]
        self.assertTrue(measured)
        self.assertEqual((measured[0]["total"], measured[0]["unit"]), (200, "bytes"))

    def test_download_cancel_during_hook_and_section_retry_is_irreversible(self):
        stopped = [False]

        def cancel():
            if stopped[0]:
                raise RuntimeError("download cancelled")

        class Downloader:
            def __init__(self, opts):
                self.opts = opts

            def download(self, urls):
                stopped[0] = True
                for callback in self.opts.get("progress_hooks", []):
                    callback({"status": "downloading", "downloaded_bytes": 100})

        with patch.object(media, "_ydl", side_effect=lambda opts: contextlib.nullcontext(Downloader(opts))), \
             self.assertRaisesRegex(RuntimeError, "download cancelled"):
            media.download_audio("https://example.invalid", DATA, jid="test", cancel=cancel)

        stopped[0] = False

        def section(*args, **kwargs):
            stopped[0] = True
            raise RuntimeError("cancelled in provider")

        with patch.object(media, "_section", side_effect=section) as attempt, \
             self.assertRaisesRegex(RuntimeError, "download cancelled"):
            media.download_section("https://example.invalid", DATA / "pieces", "part", 1, 2,
                                   jid="test", cancel=cancel)
        self.assertEqual(attempt.call_count, 1)


class ProcessTests(unittest.TestCase):
    def test_external_job_stop_kills_and_reaps_registered_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(15)"], creationflags=media.NO_WINDOW)
        try:
            media.register_proc("external-stop", child)
            media.kill_job_procs("external-stop")
            self.assertIsNotNone(child.returncode)
            self.assertNotIn("external-stop", media._JOB_PROCS)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_process_registry_releases_finished_attempt_keys(self):
        p = object()
        media.register_proc("registry-attempt", p)
        media.unregister_proc("registry-attempt", p)
        self.assertNotIn("registry-attempt", media._JOB_PROCS)

    def test_ffmpeg_microsecond_and_clock_progress_parsing(self):
        self.assertEqual(media._ffmpeg_seconds({"out_time_us": "1500000"}), 1.5)
        self.assertEqual(media._ffmpeg_seconds({"out_time_ms": "1500000"}), 1.5)
        self.assertEqual(media._ffmpeg_seconds({"out_time": "01:02:03.500000"}), 3723.5)
        self.assertEqual(media._ffmpeg_seconds({"out_time_us": "1000000", "out_time_ms": "9000000"}), 1)
        self.assertIsNone(media._ffmpeg_seconds({"out_time_us": "N/A"}))
        self.assertIsNone(media._ffmpeg_seconds({"out_time_us": "nan"}))

    def test_progress_is_measured_monotonic_and_callbacks_are_safe(self):
        script = ("import sys,time; "
                  "sys.stdout.write('out_time_us=1000000\\nprogress=continue\\n');sys.stdout.flush();time.sleep(.12);"
                  "sys.stdout.write('out_time_ms=500000\\nprogress=continue\\n');sys.stdout.flush();time.sleep(.12);"
                  "sys.stdout.write('out_time=00:00:01.750000\\nprogress=end\\n');sys.stdout.flush()")
        seen = []
        media.run([sys.executable, "-u", "-c", script], duration=2, phase="encode", on_progress=lambda **f: seen.append(f))
        self.assertEqual(seen[0]["completed"], 0)
        measured = [f["completed"] for f in seen if f["completed"] is not None]
        self.assertEqual(measured, sorted(measured))
        self.assertIn(1.75, measured)
        self.assertTrue(all(f["total"] == 2 and f["unit"] == "seconds" and f["phase_attempt"] == 1 for f in seen))
        media.run([sys.executable, "-c", "print('out_time_us=1\\nprogress=end')"], duration=2,
                  on_progress=lambda **f: (_ for _ in ()).throw(ValueError("callback only")))

    def test_stderr_is_bounded_and_drained_without_deadlock(self):
        script = "import sys;sys.stderr.write('x'*500000+'TAIL_ERROR');sys.stderr.flush();sys.exit(3)"
        with self.assertRaises(RuntimeError) as error:
            media.run([sys.executable, "-c", script], timeout=5)
        self.assertIn("TAIL_ERROR", str(error.exception))
        self.assertLess(len(str(error.exception)), 2000)

    def test_timeout_and_cancel_kill_and_reap(self):
        real_popen = subprocess.Popen
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                spawned = []
                began = time.monotonic()

                def spawn(*args, **kwargs):
                    p = real_popen(*args, **kwargs)
                    spawned.append(p)
                    return p

                def cancel():
                    if cancelled and time.monotonic() - began > 0.15:
                        raise RuntimeError("Cancelled test")

                with patch.object(subprocess, "Popen", side_effect=spawn), self.assertRaises(RuntimeError):
                    media.run([sys.executable, "-c", "import time;time.sleep(15)"], timeout=0.25,
                              jid="process-test", cancel=cancel)
                self.assertLess(time.monotonic() - began, 3)
                self.assertIsNotNone(spawned[0].returncode)
                self.assertFalse(media._JOB_PROCS.get("process-test"))


class VoiceReuseTests(unittest.TestCase):
    def test_ass_recovery_preserves_multiword_waiting_said_and_all_styles(self):
        cfg = {**VOICE_CFG, "script": "one two three four five"}
        words = [{"w": word, "s": 0.1 + i * 0.3, "e": 0.25 + i * 0.3}
                 for i, word in enumerate(cfg["script"].split())]
        for style in render.STYLES:
            with self.subTest(style=style):
                stem = "many-" + style
                path = DATA / f"{stem}_vo.mp3"
                path.write_bytes(b"audio fixture")
                original = render.build_ass(words, 0, 2, "", style, [], 300, 1380)
                (DATA / f"{stem}.ass").write_text(original, encoding="utf-8")
                asset = {"path": path, "fingerprint": render.voice_fingerprint(cfg)}
                clip = {"start": 0, "end": 2, "layout": "crop", "style": style, "voiceover": cfg}
                with patch.object(voice, "generate", side_effect=AssertionError("must not synthesize")), \
                     patch.object(render, "_render_in", return_value={"file": stem + ".mp4"}) as encode:
                    render.render_clip(Path("s.mp4"), DATA / "out", stem, clip, [], 640, 360,
                                       source_meta=SOURCE, voice_asset=asset)
                self.assertEqual(encode.call_args.args[8], original)

    def test_recovered_voice_allows_new_caption_style_grouping(self):
        path = DATA / "restyle_vo.mp3"
        path.write_bytes(b"audio fixture")
        words = [{"w": word, "s": 0.1 + i * 0.3, "e": 0.25 + i * 0.3}
                 for i, word in enumerate("one two three four five".split())]
        original = render.build_ass(words, 0, 2, "", "bounce", [], 300, 1380)
        (DATA / "restyle.ass").write_text(original, encoding="utf-8")
        recovered = render._saved_voice_words({}, path, script="one two three four five")
        changed = render.build_ass(recovered, 0, 2, "", "hype", [], 300, 1380)
        active = next(line for line in changed.splitlines() if line.startswith("Dialogue: 2,") and line.endswith("FIVE"))
        self.assertIn("\\pos(540,", active)

    def test_ass_recovery_preserves_caption_lead_group_boundaries_and_tail_exactly(self):
        path = DATA / "timing_vo.mp3"
        path.write_bytes(b"audio fixture")
        cfg = {**VOICE_CFG, "script": "one. two"}
        words = [{"w": "one.", "s": 0.1, "e": 0.3}, {"w": "two", "s": 0.4, "e": 0.7}]
        original = render.build_ass(words, 0, 1, "", "bounce", [], 300, 1380)
        (DATA / "timing.ass").write_text(original, encoding="utf-8")
        asset = {"path": path, "fingerprint": render.voice_fingerprint(cfg)}
        clip = {"start": 0, "end": 1, "layout": "crop", "voiceover": cfg}
        with patch.object(voice, "generate", side_effect=AssertionError("must not synthesize")), \
             patch.object(render, "_render_in", return_value={"file": "timing.mp4"}) as encode:
            result = render.render_clip(Path("s.mp4"), DATA / "out", "timing", clip, [], 640, 360,
                                        source_meta=SOURCE, voice_asset=asset)
        self.assertEqual(encode.call_args.args[8], original)
        self.assertAlmostEqual(result["voice_asset"]["words"][0]["s"], 0.1)
        self.assertAlmostEqual(result["voice_asset"]["words"][1]["s"], 0.4)

    def test_partial_ass_cannot_silently_drop_unrendered_narration_words(self):
        path = DATA / "partial-words_vo.mp3"
        path.write_bytes(b"audio fixture")
        cfg = {**VOICE_CFG, "script": "one two three"}
        ass = render.build_ass([{ "w": "one", "s": 0.1, "e": 0.3}, {"w": "two", "s": 0.4, "e": 0.7}],
                               0, 1, "", "bounce", [], 300, 1380)
        (DATA / "partial-words.ass").write_text(ass, encoding="utf-8")
        asset = {"path": path, "fingerprint": render.voice_fingerprint(cfg)}
        clip = {"start": 0, "end": 1, "layout": "crop", "voiceover": cfg}
        with patch.object(voice, "generate", side_effect=AssertionError("must not synthesize")), \
             patch.object(render, "_render_in", return_value={"file": "partial-words.mp4"}), \
             self.assertRaisesRegex(RuntimeError, "recover|complete"):
            render.render_clip(Path("s.mp4"), DATA / "out", "partial-words", clip, [], 640, 360,
                               source_meta=SOURCE, voice_asset=asset)

    def test_changed_narration_synthesizes_once_and_returns_reusable_metadata(self):
        path = DATA / "changed_vo.mp3"
        path.write_bytes(b"previous audio")
        old = {"path": path, "words": [{"w": "old", "s": 0, "e": 0.4}],
               "fingerprint": render.voice_fingerprint(VOICE_CFG)}
        new = {**VOICE_CFG, "script": "A changed script."}
        generated = [{"w": "changed", "s": 0, "e": 0.4}]

        def generate(text, vid, target, rate="+0%"):
            target.write_bytes(b"fresh audio")
            return generated

        with patch.object(voice, "generate", side_effect=generate) as synth, \
             patch.object(render, "_render_in", return_value={"file": "changed.mp4"}):
            result = render.render_clip(Path("s.mp4"), DATA / "out", "changed", {"start": 0, "end": 1, "voiceover": new},
                                        [], 640, 360, source_meta=SOURCE, voice_asset=old)
        self.assertEqual(synth.call_count, 1)
        self.assertEqual(result["voice_asset"], {"file": "changed_vo.mp3", "words": generated,
                                                "fingerprint": render.voice_fingerprint(new)})

    def test_fingerprint_tracks_engine_text_voice_rate_not_visual_or_mix(self):
        original = render.voice_fingerprint(VOICE_CFG)
        self.assertEqual(original, render.voice_fingerprint({**VOICE_CFG, "mode": "duck", "visual": visual()}))
        self.assertNotEqual(original, render.voice_fingerprint({**VOICE_CFG, "script": "New payoff"}))
        self.assertNotEqual(original, render.voice_fingerprint({**VOICE_CFG, "rate": "+10%"}))
        self.assertEqual(len(original), 64)

    def test_visual_only_edit_reuses_saved_audio_and_words(self):
        path = DATA / "saved_vo.mp3"
        path.write_bytes(b"audio fixture")
        words = [{"w": "one", "s": 0, "e": 0.4}]
        asset = {"path": path, "words": words, "fingerprint": render.voice_fingerprint(VOICE_CFG)}
        clip = {"start": 0, "end": 1, "hook": "Changed headline", "visual": visual(), "voiceover": VOICE_CFG}
        with patch.object(voice, "generate", side_effect=AssertionError("must not synthesize")), \
             patch.object(render, "_render_in", return_value={"file": "revision.mp4"}) as encode:
            got = render.render_clip(Path("s.mp4"), DATA / "out", "revision", clip, [], 640, 360,
                                     source_meta=SOURCE, voice_asset=asset)
        self.assertIn("one".upper(), encode.call_args.args[8])
        self.assertEqual(got["voice_asset"]["words"], words)
        self.assertEqual(got["voice_asset"]["file"], "revision_vo.mp3")
        self.assertNotIn("path", got["voice_asset"])

    def test_legacy_saved_ass_recovers_active_word_events(self):
        path = DATA / "legacy_vo.mp3"
        path.write_bytes(b"audio fixture")
        ass = render.build_ass([{ "w": "one", "s": 0, "e": 0.3}, {"w": "two", "s": 0.4, "e": 0.7}],
                               0, 1, "Legacy title", "bounce", [], 300, 1380)
        (DATA / "legacy.ass").write_text(ass, encoding="utf-8")
        cfg = {**VOICE_CFG, "script": "one two"}
        asset = {"path": path, "fingerprint": render.voice_fingerprint(cfg)}
        clip = {"start": 0, "end": 1, "visual": visual(), "voiceover": cfg}
        with patch.object(voice, "generate", side_effect=AssertionError("must not synthesize")), \
             patch.object(render, "_render_in", return_value={"file": "revision.mp4"}):
            got = render.render_clip(Path("s.mp4"), DATA / "out", "revision", clip, [], 640, 360,
                                     source_meta=SOURCE, voice_asset=asset)
        self.assertEqual([w["w"].lower() for w in got["voice_asset"]["words"]], ["one", "two"])

    def test_missing_legacy_timings_fails_without_resynthesis(self):
        path = DATA / "missing_vo.mp3"
        path.write_bytes(b"audio fixture")
        asset = {"path": path, "fingerprint": render.voice_fingerprint(VOICE_CFG)}
        clip = {"start": 0, "end": 1, "visual": visual(), "voiceover": VOICE_CFG}
        with patch.object(voice, "generate", side_effect=AssertionError("must not synthesize")), \
             self.assertRaisesRegex(RuntimeError, "recover|timing"):
            render.render_clip(Path("s.mp4"), DATA / "out", "revision", clip, [], 640, 360,
                               source_meta=SOURCE, voice_asset=asset)


@unittest.skipUnless(FFMPEG, "FFmpeg is not installed")
class EncodeTests(unittest.TestCase):
    def test_legacy_sar_encode_matches_historical_coded_geometry(self):
        src = DATA / "legacy-sar.mp4"
        make_video(src, size="320x240", extra=["-vf", "setsar=4/3"])
        source_meta = media.probe(src)
        layout, fg_h, hook_y, caption_y = render.geometry(320, 240, "blur")
        words = [{"w": "payoff", "s": 0.05, "e": 0.3}]
        ass = render.build_ass(words, 0, 0.4, "Legacy payoff", "bounce", [], hook_y, caption_y)
        expected_dir, work = DATA / "legacy-sar-reference", DATA / "legacy-sar-work"
        expected_dir.mkdir()
        work.mkdir()
        with patch.object(render, "_qsv_ok", False):
            render._render_in(work, src, expected_dir, "expected", 0, 0.4, layout, fg_h, ass)
            result = render.render_clip(src, DATA / "encoded", "legacy-sar", {"start": 0, "end": 0.4, "hook": "Legacy payoff"},
                                        words, source_meta["width"], source_meta["height"], source_meta=source_meta)
            positional = render.render_clip(src, DATA / "encoded", "legacy-sar-positional",
                                            {"start": 0, "end": 0.4, "hook": "Legacy payoff"}, words, 320, 240)
        self.assertEqual(hashlib.sha256((expected_dir / "expected.mp4").read_bytes()).digest(),
                         hashlib.sha256((DATA / "encoded" / result["file"]).read_bytes()).digest())
        self.assertEqual(hashlib.sha256((expected_dir / "expected.mp4").read_bytes()).digest(),
                         hashlib.sha256((DATA / "encoded" / positional["file"]).read_bytes()).digest())

    def test_real_audio_span_is_mono_16k_and_sample_bounded(self):
        src, dst = DATA / "pcm-source.wav", DATA / "pcm-span.wav"
        samples = (np.arange(48000) % 10000 - 5000).astype(np.int16)
        sf.write(src, samples, 16000, subtype="PCM_16")
        measured = []
        media.extract_audio(src, dst, start=1.25, end=1.5, on_progress=lambda **f: measured.append(f))
        np.testing.assert_array_equal(media.load_audio(dst), samples[20000:24000])
        self.assertTrue(all(f["total"] == 0.25 for f in measured))

    @classmethod
    def setUpClass(cls):
        cls.src = DATA / "source.mp4"
        make_video(cls.src)
        render.install_fonts()

    def test_all_manual_ratios_encode_without_source_audio(self):
        for ratio in ("9:16", "1:1", "16:9", "source"):
            with self.subTest(ratio=ratio), patch.object(render, "_qsv_ok", False):
                name = "manual_" + ratio.replace(":", "_")
                clip = {"start": 0, "end": 0.4, "hook": "Real payoff", "visual": visual(ratio, font="anton")}
                seen = []
                got = render.render_clip(self.src, DATA / "encoded", name, clip,
                                         [{"w": "payoff", "s": 0.05, "e": 0.3}], 640, 360,
                                         source_meta=SOURCE, on_progress=lambda **f: seen.append(f))
                meta = media.probe(DATA / "encoded" / got["file"])
                expected = render.resolve_visual(clip, SOURCE)
                self.assertEqual((meta["width"], meta["height"]), (expected["width"], expected["height"]))
                self.assertFalse(meta["audio"])
                self.assertTrue((DATA / "encoded" / got["cover"]).is_file())
                ass = (DATA / "encoded" / f"{name}.ass").read_text(encoding="utf-8")
                self.assertNotIn(",Hook,,", ass)
                self.assertIn(",Cap,,", ass)
                self.assertIn("encode", [f["phase"] for f in seen])
                self.assertIn("cover", [f["phase"] for f in seen])

    def test_off_center_roi_renders_the_reported_region(self):
        src = DATA / "roi.mp4"
        pattern = ("color=c=red:s=640x360:r=30:d=0.4,"
                   "drawbox=x=320:y=0:w=320:h=360:color=lime:t=fill,"
                   "drawbox=x=400:y=150:w=20:h=20:color=blue:t=fill")
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", pattern,
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)])
        value = visual("9:16")
        value["framing"]["roi"] = {"x": 0.5, "y": 0, "width": 0.5, "height": 1}
        clip = {"start": 0, "end": 0.4, "visual": value}
        with patch.object(render, "_qsv_ok", False):
            result = render.render_clip(src, DATA / "encoded", "roi", clip, [], 640, 360, source_meta=SOURCE)
        geometry = result["geometry"]
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(DATA / "encoded" / result["file"]),
                              "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                             check=True, capture_output=True).stdout
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(geometry["height"], geometry["width"], 3)
        crop = geometry["crop_pixels"]
        marker = frame[160 - crop["y"], 410 - crop["x"]]
        self.assertGreater(marker[2], 230)
        self.assertLess(marker[0], 15)
        self.assertGreater(frame[10, 10, 1], 230)
        self.assertLess(frame[10, 10, 0], 15)

    def test_existing_revision_is_never_overwritten(self):
        folder = DATA / "existing"
        folder.mkdir(exist_ok=True)
        old = folder / "same.mp4"
        old.write_bytes(b"last successful revision")
        with patch.object(render, "_qsv_ok", False), self.assertRaisesRegex(RuntimeError, "revision|exists"):
            render.render_clip(self.src, folder, "same", {"start": 0, "end": 0.4, "visual": visual()},
                               [], 640, 360, source_meta=SOURCE)
        self.assertEqual(old.read_bytes(), b"last successful revision")

    def test_cancel_during_file_moves_removes_only_new_partial_artifacts(self):
        folder = DATA / "mid-move"
        folder.mkdir(exist_ok=True)
        previous = folder / "previous.mp4"
        previous.write_bytes(b"untouched previous revision")
        stopped = [False]
        move = shutil.move

        def move_then_cancel(*args, **kwargs):
            result = move(*args, **kwargs)
            stopped[0] = True
            return result

        def cancel():
            if stopped[0]:
                raise RuntimeError("cancelled during publication")

        with patch.object(render, "_qsv_ok", False), patch.object(shutil, "move", side_effect=move_then_cancel), \
             self.assertRaisesRegex(RuntimeError, "cancelled during publication"):
            render.render_clip(self.src, folder, "partial", {"start": 0, "end": 0.4, "visual": visual()},
                               [], 640, 360, source_meta=SOURCE, cancel=cancel)
        self.assertEqual(previous.read_bytes(), b"untouched previous revision")
        self.assertFalse(list(folder.glob("partial.*")))

    def test_bitmap_fade_and_overlay_once_are_visible_in_encoded_frames(self):
        src = DATA / "blue.mp4"
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=640x360:r=30:d=0.4",
                   "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)])
        clip = {"start": 0, "end": 0.4, "hook": "PAYOFF", "visual": visual("1:1", position={"x": 0.5, "y": 0.5})}
        with patch.object(render, "_qsv_ok", False):
            result = render.render_clip(src, DATA / "encoded", "fade", clip, [], 640, 360, source_meta=SOURCE)
        meta = result["geometry"]["headline"]
        pixels = subprocess.run(["ffmpeg", "-v", "error", "-i", str(DATA / "encoded" / result["file"]),
                                 "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], check=True, capture_output=True).stdout
        frames = np.frombuffer(pixels, dtype=np.uint8).reshape(-1, 360, 360, 3)
        x, y = meta["x"] + meta["bitmap_width"] // 2, meta["y"] + 2
        samples = frames[[0, 3, 6], y, x].astype(int)
        self.assertLess(samples[0][0], 10)
        self.assertGreater(samples[1][0], 100)
        self.assertLess(samples[1][0], 220)
        self.assertTrue(np.all(samples[2] > 240))
        legacy_y = 300 * 360 / 1920
        self.assertLess(frames[6, round(legacy_y), 180, 0], 10)

    def test_cancelled_or_timed_out_encode_does_not_fall_back(self):
        for message in ("cancelled during encode", "encode took too long and was stopped"):
            with self.subTest(message=message), patch.object(render, "_qsv_ok", None), \
                 patch.object(render, "run", side_effect=RuntimeError(message)) as command, \
                 self.assertRaisesRegex(RuntimeError, message):
                render.render_clip(self.src, DATA / "interrupted", "interrupted", {"start": 0, "end": 0.4},
                                   [], 640, 360, source_meta=SOURCE)
            self.assertEqual(command.call_count, 1)
            self.assertFalse((DATA / "interrupted" / "interrupted.mp4").exists())

    def test_contain_and_sar_rotation_are_not_double_cropped_or_rotated(self):
        sar_src = DATA / "sar.mp4"
        make_video(sar_src, size="320x240", extra=["-vf", "setsar=4/3"])
        rotated = DATA / "rotated.mp4"
        media.run(["ffmpeg", "-y", "-v", "error", "-display_rotation", "90", "-i", str(sar_src),
                   "-c", "copy", str(rotated)])
        source_meta = media.probe(rotated)
        self.assertEqual(source_meta["width"], 240)
        self.assertGreater(source_meta["height"], 320)
        with patch.object(render, "_qsv_ok", False):
            clip = {"start": 0, "end": 0.4, "visual": visual("16:9", "contain")}
            got = render.render_clip(rotated, DATA / "encoded", "rotated", clip, [],
                                     source_meta["width"], source_meta["height"], source_meta=source_meta)
        result = media.probe(DATA / "encoded" / got["file"])
        self.assertAlmostEqual(result["width"] / result["height"], 16 / 9, delta=0.01)
        self.assertEqual(result["rotation"], 0)
        self.assertEqual(result["sar"], "1:1")

    def test_fallback_starts_fresh_and_cancel_before_publication_leaves_no_output(self):
        real_run = render.run
        seen = []

        def fail_hardware(cmd, **kwargs):
            if "h264_qsv" in cmd:
                media.report_progress(kwargs.get("on_progress"), "encode", 0, 0.4, "seconds", 1)
                media.report_progress(kwargs.get("on_progress"), "encode", 0.3, 0.4, "seconds", 1)
                raise media.ProcessFailed("hardware unavailable")
            return real_run(cmd, **kwargs)

        clip = {"start": 0, "end": 0.4, "visual": visual()}
        with patch.object(render, "_qsv_ok", None), patch.object(render, "run", side_effect=fail_hardware):
            render.render_clip(self.src, DATA / "encoded", "fallback", clip, [], 640, 360,
                               source_meta=SOURCE, on_progress=lambda **f: seen.append(f))
        encode = [f for f in seen if f["phase"] == "encode"]
        self.assertEqual(next(f for f in encode if f["phase_attempt"] == 2)["completed"], 0)

        def progress(**fields):
            if fields["phase"] == "publication":
                stopped[0] = True

        stopped = [False]

        def cancel():
            if stopped[0]:
                raise RuntimeError("cancelled before publication")

        with patch.object(render, "_qsv_ok", False), self.assertRaisesRegex(RuntimeError, "cancelled"):
            render.render_clip(self.src, DATA / "cancelled", "cancelled", clip, [], 640, 360,
                               source_meta=SOURCE, cancel=cancel, on_progress=progress)
        self.assertFalse((DATA / "cancelled" / "cancelled.mp4").exists())


class Results(unittest.TextTestResult):
    def addSuccess(self, test):
        super().addSuccess(test)
        self.stream.writeln("PASS " + test.id().rsplit(".", 1)[-1])


def offline(*args, **kwargs):
    raise AssertionError("Network access is forbidden in renderer/media tests")


if __name__ == "__main__":
    with patch.object(socket.socket, "connect", side_effect=offline), \
         patch.object(socket, "create_connection", side_effect=offline), \
         patch.object(voice, "generate", side_effect=offline):
        if "--record-legacy" in sys.argv:
            record_legacy()
        else:
            suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(case)
                                       for case in list(globals().values())
                                       if isinstance(case, type) and issubclass(case, unittest.TestCase))
            if not suite.countTestCases():
                raise RuntimeError("No renderer/media regressions were discovered.")
            result = unittest.TextTestRunner(stream=sys.stdout, verbosity=0, resultclass=Results).run(suite)
            sys.exit(0 if result.wasSuccessful() else 1)
