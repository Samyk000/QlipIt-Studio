"""Offline held-out gate for user-authorized English references and recorded predictions.

This script never runs a provider, recognizer, downloader or the app. Supply a schema_version:1
manifest with fixtures (id, duration, reference, words, entities, tags, positive_spans,
silence_spans), and a JSON list of baseline/candidate runs. Each run contains metadata and
fixtures with timed words, selected clips, independently annotated human_hook_ok and
human_standalone_ok, seconds_to_first_useful_clip, elapsed_seconds, cpu_peak_percent and
memory_peak_mb. Resource/latency values must be measured, not estimated. Keep recording,
provider/model/recognizer, hardware, concurrency and cache state fixed between runs.

CLI: python -B tests/benchmark_english.py --manifest <references.json> --runs <runs.json>
Without authorized inputs the gate is explicitly not_ready, never a fabricated PASS.
"""
import argparse
import hashlib
import json
import math
import re
import statistics
from pathlib import Path

REQUIRED_TAGS = {"clean", "quiet", "noisy", "names_numbers", "negative"}
FIXED_METADATA = ("provider", "model", "recognizer", "hardware", "concurrency", "cache_state")


def _tokens(text):
    return re.findall(r"[^\W_]+(?:'[^\W_]+)*", text.casefold().replace("\u2019", "'"))


def _number(value, label, lower=0, upper=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < lower or (upper is not None and value > upper):
        raise ValueError(f"Invalid measured {label}.")
    return float(value)


def _spans(rows, duration):
    if not isinstance(rows, list):
        raise ValueError("Spans must be a list.")
    out = []
    for row in rows:
        a = _number(row.get("start"), "span start", upper=duration)
        b = _number(row.get("end"), "span end", upper=duration)
        if b <= a:
            raise ValueError("Spans must have positive length.")
        out.append((a, b))
    return out


def _words(rows, duration):
    if not isinstance(rows, list):
        raise ValueError("Words must be a list.")
    out = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("w"), str) or not row["w"].strip():
            raise ValueError("A timed word must contain text.")
        a = _number(row.get("s"), "word start", upper=duration)
        b = _number(row.get("e"), "word end", upper=duration)
        if b <= a:
            raise ValueError("Word ends must follow starts inside the recording.")
        out.append((row["w"], a, b))
    return out


def _distance(reference, hypothesis):
    row = list(range(len(hypothesis) + 1))
    for i, word in enumerate(reference, 1):
        next_row = [i]
        for j, other in enumerate(hypothesis, 1):
            next_row.append(min(next_row[-1] + 1, row[j] + 1, row[j - 1] + (word != other)))
        row = next_row
    return row[-1]


def _unique(rows, label):
    if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r["id"] for r in rows):
        raise ValueError(f"{label} must have explicit fixture IDs.")
    out = {r["id"]: r for r in rows}
    if len(out) != len(rows):
        raise ValueError(f"Duplicate fixture ID in {label}.")
    return out


def evaluate(manifest, runs):
    """Score immutable supplied records; missing quality evidence cannot pass promotion."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("Use manifest schema_version 1.")
    fixtures = _unique(manifest.get("fixtures"), "Manifest")
    if not fixtures:
        raise ValueError("No authorized reference fixtures were supplied.")
    if not isinstance(runs, list) or len(runs) != 2 or {r.get("name") for r in runs} != {"baseline", "candidate"}:
        raise ValueError("Supply exactly baseline and candidate runs.")
    by_name = {r["name"]: r for r in runs}
    baseline_meta = by_name["baseline"].get("metadata") or {}
    candidate_meta = by_name["candidate"].get("metadata") or {}
    for key in FIXED_METADATA:
        if key not in baseline_meta or key not in candidate_meta or baseline_meta[key] != candidate_meta[key]:
            raise ValueError(f"Hold {key} fixed across both runs.")
    if any(not isinstance(baseline_meta[k], str) or not baseline_meta[k].strip() for k in FIXED_METADATA if k != "concurrency"):
        raise ValueError("Pinned runtime/model metadata must be explicit.")
    _number(baseline_meta["concurrency"], "concurrency", lower=1)
    reasons, missing = [], []
    tags = set()
    prepared = {}
    for fid, fixture in fixtures.items():
        duration = _number(fixture.get("duration"), "duration", lower=0.001)
        if not isinstance(fixture.get("reference"), str):
            raise ValueError("Each recording needs a human reference, including empty negative references.")
        ref_words = _words(fixture.get("words", []), duration)
        positive = _spans(fixture.get("positive_spans", []), duration)
        silence = _spans(fixture.get("silence_spans", []), duration)
        entities = fixture.get("entities", [])
        if not isinstance(entities, list) or any(not isinstance(e, str) or not e for e in entities):
            raise ValueError("Reference entities must be explicit text names/numbers.")
        fixture_tags = fixture.get("tags", [])
        if not isinstance(fixture_tags, list) or any(not isinstance(t, str) for t in fixture_tags):
            raise ValueError("Fixture tags must be a list of coverage labels.")
        tags.update(fixture_tags)
        if fixture["reference"] and not ref_words:
            missing.append(f"{fid}: human word timing annotations are missing")
        prepared[fid] = (duration, ref_words, positive, silence, entities)
    if not REQUIRED_TAGS <= tags:
        missing.append("coverage missing: " + ", ".join(sorted(REQUIRED_TAGS - tags)))
    metrics = {}
    for name in ("baseline", "candidate"):
        predicted = _unique(by_name[name].get("fixtures"), f"{name} predictions")
        if set(predicted) != set(fixtures):
            raise ValueError("Both runs must contain exactly the same held-out recordings.")
        totals = {k: 0 for k in ("reference_words", "word_errors", "entity_errors", "silence_words", "timing_errors",
                                "clips", "useful_clips", "negative_clips", "duplicate_clips", "incomplete_clips", "hook_failures")}
        timing, latency, elapsed, cpu, memory = [], [], [], [], []
        for fid, fixture in fixtures.items():
            row = predicted[fid]
            duration, ref_words, positive, silence, entities = prepared[fid]
            words = _words(row.get("words"), duration)
            reference = _tokens(fixture["reference"])
            hypothesis = [token for word, _, _ in words for token in _tokens(word)]
            totals["reference_words"] += len(reference)
            totals["word_errors"] += _distance(reference, hypothesis)
            joined = " " + " ".join(hypothesis) + " "
            totals["entity_errors"] += sum(" " + " ".join(_tokens(entity)) + " " not in joined for entity in entities)
            totals["silence_words"] += sum(any(max(a, s) < min(b, e) - 0.02 for s, e in silence) for _, a, b in words)
            totals["timing_errors"] += sum(words[i][1] < words[i - 1][2] - 0.011 for i in range(1, len(words)))
            used = set()
            for word, a, b in ref_words:
                match = next((i for i, (other, _, _) in enumerate(words) if i not in used and _tokens(other) == _tokens(word)), None)
                if match is not None:
                    used.add(match)
                    timing.append((abs(words[match][1] - a) + abs(words[match][2] - b)) / 2)
            clips = row.get("clips")
            spans = _spans(clips, duration)
            totals["clips"] += len(clips)
            totals["negative_clips"] += len(clips) if not positive else 0
            for i, ((a, b), clip) in enumerate(zip(spans, clips)):
                overlap = [max(0, min(b, e) - max(a, s)) / max(0.001, max(b, e) - min(a, s)) for s, e in positive]
                matched = max(range(len(positive)), key=lambda n: overlap[n]) if positive else None
                complete = matched is not None and overlap[matched] >= 0.5 and a <= positive[matched][0] + 0.02 and b >= positive[matched][1] - 0.02
                hook, standalone = clip.get("human_hook_ok"), clip.get("human_standalone_ok")
                if not isinstance(hook, bool) or not isinstance(standalone, bool):
                    missing.append(f"{name}/{fid}: independent hook/standalone annotations are missing")
                totals["hook_failures"] += hook is False
                totals["incomplete_clips"] += not complete or standalone is False
                totals["useful_clips"] += complete and hook is True and standalone is True
                totals["duplicate_clips"] += any(max(0, min(b, e) - max(a, s)) > 0.3 * min(b - a, e - s) for s, e in spans[:i])
            if positive:
                if row.get("seconds_to_first_useful_clip") is None:
                    missing.append(f"{name}/{fid}: measured time to first useful clip is missing")
                else:
                    latency.append(_number(row["seconds_to_first_useful_clip"], "time to first useful clip"))
            for key, target in (("elapsed_seconds", elapsed), ("cpu_peak_percent", cpu), ("memory_peak_mb", memory)):
                if row.get(key) is None:
                    missing.append(f"{name}/{fid}: measured {key} is missing")
                else:
                    target.append(_number(row[key], key))
        metrics[name] = {**totals,
                         "wer": totals["word_errors"] / max(1, totals["reference_words"]),
                         "word_boundary_error_seconds": statistics.mean(timing) if timing else None,
                         "selection_precision": totals["useful_clips"] / totals["clips"] if totals["clips"] else None,
                         "seconds_to_first_useful_clip": statistics.mean(latency) if latency else None,
                         "elapsed_seconds": sum(elapsed) if len(elapsed) == len(fixtures) else None,
                         "cpu_peak_percent": max(cpu) if cpu else None,
                         "memory_peak_mb": max(memory) if memory else None}
    old, new = metrics["baseline"], metrics["candidate"]
    for key in ("wer", "entity_errors", "silence_words", "timing_errors", "negative_clips", "duplicate_clips",
                "incomplete_clips", "hook_failures", "word_boundary_error_seconds", "seconds_to_first_useful_clip",
                "elapsed_seconds", "cpu_peak_percent", "memory_peak_mb"):
        if old[key] is not None and new[key] is not None and new[key] > old[key] + 1e-9:
            reasons.append(key + " regressed")
    if old["selection_precision"] is not None and (new["selection_precision"] is None or new["selection_precision"] < old["selection_precision"]):
        reasons.append("selection_precision regressed")
    if new["useful_clips"] < old["useful_clips"]:
        reasons.append("fewer complete, independently approved clips")
    reasons = sorted(set(missing + reasons))
    return {"gate": "not_ready" if missing else "fail" if reasons else "pass", "reasons": reasons,
            "manifest_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
            "pinned_metadata": baseline_meta, "coverage": sorted(tags), "runs": metrics,
            "note": "Scores use supplied authorized references, recorded measurements and independent human annotations. Equal results are not evidence of improvement."}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, help="User-authorized held-out reference manifest JSON")
    parser.add_argument("--runs", type=Path, help="Recorded baseline/candidate JSON; no provider/model calls are made")
    args = parser.parse_args()
    if args.manifest is None or args.runs is None:
        print(json.dumps({"gate": "not_ready", "reasons": ["Authorized English reference manifest and measured baseline/candidate runs are required."],
                          "provider_calls": 0, "model_downloads": 0}))
        return 2
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        runs = json.loads(args.runs.read_text(encoding="utf-8"))
        result = evaluate(manifest, runs.get("runs") if isinstance(runs, dict) else runs)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"gate": "not_ready", "reasons": [str(exc)]}))
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["gate"] == "pass" else 1 if result["gate"] == "fail" else 2


if __name__ == "__main__":
    raise SystemExit(main())
