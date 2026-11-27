#!/usr/bin/env python3
"""
HHS Clips — local clipper. No posting in this file.

Takes a long video you own — a local file or a video URL — transcribes
it locally with faster-whisper, scores moments, cuts the top N into
9:16 vertical clips with burned-in captions, and writes a clips.json
manifest for the approval queue.

Modes (--mode gameplay|talking|auto, default auto):
- gameplay: audio-energy spikes + excited speech (game streams).
- talking: transcript-based scoring for talking-head video — hooks,
  quotable lines, questions, story peaks, emotion (podcasts, talks,
  webinars, testimonials).
- auto: talking when speech is dense (>= 1 word/sec on average),
  gameplay when it isn't or there is no transcript. Explicit
  --mode talking with no transcript falls back to gameplay scoring
  and says so in the manifest.

Transcription backends (--transcriber auto|faster-whisper|whisper-cpp,
default auto):
- faster-whisper: the Python library, the default on a desktop
  (pip install faster-whisper). Needs numpy + PyAV.
- whisper-cpp: the whisper.cpp CLI (`whisper-cli`), the Termux path —
  faster-whisper has no Android wheels, so on a phone the clipper
  shells out to whisper-cli and reads its JSON output. The model is
  a ggml file, `models/ggml-<model>.bin` by default (see --model-path).
  auto picks faster-whisper when it imports, else whisper-cli when
  it is on PATH.

Own-content gate: every job needs --i-own-it. Without it the job is
refused before anything is downloaded or cut. This app is for your
own videos only.

No cloud API keys. No uploads. Nothing in this file posts anywhere.
"""
import argparse
import json
import math
import os
import re
import struct
import subprocess
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

EXCITEMENT = {
    "let's go": 3, "lets go": 3, "oh my god": 3, "omg": 3, "no way": 3,
    "insane": 3, "crazy": 2, "wow": 2, "holy": 2, "jesus": 2,
    "got him": 3, "got 'em": 3, "got em": 3, "let's gooo": 3,
    "yes": 1, "yeah": 1, "nooo": 2, "oh no": 2, "watch out": 2,
    "kill": 2, "killed": 2, "explosion": 2, "exploded": 2, "crash": 2,
    "crashed": 2, "chase": 2, "police": 1, "cops": 1, "heist": 2,
    "haha": 2, "lol": 1, "what": 1, "bro": 1, "dude": 1,
}
HASHTAGS = ["#Gaming", "#Shorts", "#Clips", "#Gameplay"]
TALKING_HASHTAGS = ["#Shorts", "#Clips", "#Podcast", "#Reels"]

# Talking-head scoring (transcript-based). A window scores on what was
# SAID, not how loud it was: a strong opening line, a short quotable
# line, a question, a story turn, an emotional word, and whether the
# window starts/ends on a sentence boundary (a clean cut).
HOOK_STARTS = ("here's", "here is", "the secret", "nobody", "stop ",
               "how to", "how do", "why", "what if", "the truth",
               "let me", "imagine", "look", "the best", "the worst",
               "number one", "did you know", "first ", "most people")
QUESTION_STARTS = ("what", "why", "how", "when", "where", "who", "which",
                   "can ", "should", "do you", "are you", "is there",
                   "have you")
STRONG_WORDS = {"amazing", "incredible", "secret", "mistake", "never",
                "always", "best", "worst", "love", "hate", "free", "new",
                "biggest", "first", "only", "truth", "actually", "really",
                "huge", "important", "exactly", "wrong", "right"}
STORY_MARKERS = ("then", "so ", "but ", "turns out", "suddenly", "finally",
                 "the moment", "and then", "in the end", "after that",
                 "next")


def resolve_mode(requested, words, duration):
    """Pick the effective scoring mode.

    Explicit gameplay/talking pass through. auto -> talking when speech
    is dense (>= 1 word/sec on average), gameplay when it isn't or
    there is no transcript at all.
    """
    if requested in ("gameplay", "talking"):
        return requested
    if words and duration and len(words) / duration >= 1.0:
        return "talking"
    return "gameplay"


def run(cmd, check=True):
    print("  $", " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        print(r.stdout[-2000:], file=sys.stderr)
        print(r.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"command failed ({r.returncode}): {cmd[0]}")
    return r


def probe(path):
    r = run(["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)], check=False)
    if r.returncode != 0:
        raise SystemExit(f"ffprobe failed on {path}")
    j = json.loads(r.stdout)
    fmt = j.get("format", {})
    dur = float(fmt.get("duration", 0))
    video = next((s for s in j["streams"] if s["codec_type"] == "video"), {})
    audio = next((s for s in j["streams"] if s["codec_type"] == "audio"), {})
    return {"duration": dur,
            "width": int(video.get("width", 0)),
            "height": int(video.get("height", 0)),
            "has_audio": bool(audio), "has_video": bool(video)}


def extract_wav(src, wav_path):
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path)])


def rms_per_second(wav_path):
    """1-second RMS energy, stdlib only. Returns list[float] 0..1 (log-normalised)."""
    with wave.open(str(wav_path), "rb") as w:
        n = w.getnframes()
        rate = w.getframerate()
        frames = w.readframes(n)
    samples = struct.unpack(f"<{len(frames)//2}h", frames)
    per_sec = []
    for i in range(0, len(samples), rate):
        chunk = samples[i:i + rate]
        if not chunk:
            continue
        rms = math.sqrt(sum(s * s for s in chunk) / len(chunk)) / 32768.0
        per_sec.append(rms)
    if not per_sec:
        return []
    # log scale: silence ~0, loud ~1
    out = []
    for r in per_sec:
        v = (math.log10(r + 1e-4) + 4) / 4.0  # 1e-4 -> 0, 1.0 -> 1
        out.append(max(0.0, min(1.0, v)))
    return out


def transcribe(wav_path, model, language):
    """Local faster-whisper on a 16k mono WAV. Returns (words, segments).

    The WAV is passed as a float32 numpy array, NOT as a file path:
    faster-whisper 1.2.1 decodes file paths through PyAV, and PyAV 19
    removed the `metadata_errors` kwarg faster-whisper passes, which
    crashes transcription. Passing decoded PCM bypasses PyAV entirely.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("WARN: faster-whisper not installed — transcript disabled.")
        return [], []
    print(f"Transcribing locally with faster-whisper model={model} ...")
    try:
        import numpy as np
        with wave.open(str(wav_path), "rb") as w:
            raw = w.readframes(w.getnframes())
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        m = WhisperModel(model, device="cpu", compute_type="int8")
        segments, _info = m.transcribe(audio, language=language or None,
                                       word_timestamps=True, vad_filter=True)
    except Exception as e:
        # Honest degraded path: never crash the job for transcription.
        # Scoring falls back to audio energy only; no captions are faked.
        print(f"WARN: transcription failed ({type(e).__name__}: {e}) — "
              "falling back to audio-only scoring, no captions.")
        return [], []
    words, segs = [], []
    for s in segments:
        segs.append({"start": float(s.start), "end": float(s.end), "text": s.text.strip()})
        for wd in (s.words or []):
            words.append({"start": float(wd.start), "end": float(wd.end),
                          "word": wd.word.strip()})
    print(f"  transcript: {len(words)} words, {len(segs)} segments")
    return words, segs


# --- whisper.cpp backend (the Termux path) ---

# Where to get a ggml model file. faster-whisper downloads its models
# itself; whisper-cli needs the file on disk, and this is the plain
# answer when it is missing.
WHISPER_CPP_MODEL_URL = ("https://huggingface.co/ggerganov/whisper.cpp"
                         "/resolve/main/ggml-{model}.bin")

# Tokens whisper.cpp emits that are not speech — never let them into
# the transcript as words.
_SPECIAL_TOKENS = ("[_BEG_]", "<|endoftext|>", "<|startoftranscript|>",
                   "<|notimestamps|>")
# Sound annotations, not speech: [Music], [Applause], [Laughter]...
# (whisper.cpp emits these as word-length entries spanning seconds).
_BRACKETED_RE = re.compile(r"^[\[(].*[\])]$")


def _faster_whisper_available():
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def resolve_transcriber(requested):
    """Pick the transcription backend.

    auto: faster-whisper when it imports, else whisper-cli when it is
    on PATH. An explicit backend that is not available fails fast with
    a plain message naming what is missing — no stack trace.
    """
    import shutil
    if requested == "faster-whisper":
        if not _faster_whisper_available():
            raise SystemExit(
                "faster-whisper is not installed — install it "
                "(pip install faster-whisper) or use "
                "--transcriber whisper-cpp.")
        return "faster-whisper"
    if requested == "whisper-cpp":
        if shutil.which("whisper-cli") is None:
            raise SystemExit(
                "whisper-cli not found on PATH — install whisper.cpp "
                "(see TERMUX.md) or use --transcriber faster-whisper.")
        return "whisper-cpp"
    # auto
    if _faster_whisper_available():
        return "faster-whisper"
    if shutil.which("whisper-cli") is not None:
        return "whisper-cpp"
    raise SystemExit(
        "No transcription backend available — install faster-whisper "
        "(pip install faster-whisper) or whisper-cli (see TERMUX.md).")


def default_model_path(model):
    """ggml model file for the whisper-cpp backend: models/ggml-<model>.bin."""
    return str(Path(__file__).resolve().parent / "models" / f"ggml-{model}.bin")


def require_whisper_cpp_model(model_path, model):
    """Fail plainly when the ggml model file is missing.

    The file is a ~75 MB download, not something the clipper fetches
    silently — the message says exactly where to get it.
    """
    if not Path(model_path).is_file():
        raise SystemExit(
            f"whisper.cpp model not found: {model_path}\n"
            f"Download it first: {WHISPER_CPP_MODEL_URL.format(model=model)}\n"
            f"Save it as {model_path} (or pass --model-path <path>).")


def parse_whisper_cpp_json(data):
    """Parse whisper-cli -ojf JSON into (words, segments).

    With `-ml 1 -sow` each transcription entry is one word, with
    offsets.from/to in milliseconds. Returns the same shape as
    transcribe(): words are {start, end, word} in seconds, segments
    are {start, end, text}. Segments are rebuilt by grouping
    consecutive words, splitting on a gap over 0.8s or after 10 words.
    Pure function — no binary needed, so tests use it directly.
    """
    words = []
    for entry in (data.get("transcription") or []):
        text = (entry.get("text") or "").strip()
        if (not text or text in _SPECIAL_TOKENS or text.startswith("[_")
                or _BRACKETED_RE.match(text)):
            continue
        offsets = entry.get("offsets") or {}
        start_ms = offsets.get("from")
        end_ms = offsets.get("to")
        if start_ms is None or end_ms is None:
            # Fall back to the "HH:MM:SS,mmm" timestamp strings.
            start_ms = _parse_whisper_ts((entry.get("timestamps") or {}).get("from"))
            end_ms = _parse_whisper_ts((entry.get("timestamps") or {}).get("to"))
        if start_ms is None or end_ms is None:
            continue
        # whisper.cpp sometimes gives the first token of a word a
        # zero-length span (from == to). The word was still spoken at
        # that instant — give it a 100ms floor so downstream timestamps
        # stay sane.
        if end_ms <= start_ms:
            end_ms = start_ms + 100
        words.append({"start": round(start_ms / 1000.0, 3),
                      "end": round(end_ms / 1000.0, 3),
                      "word": text})
    words.sort(key=lambda w: w["start"])
    words = _drop_bracketed_annotations(words)
    segments = []
    cur = []
    for w in words:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            if gap > 0.8 or len(cur) >= 10:
                segments.append(_words_to_segment(cur))
                cur = []
        cur.append(w)
    if cur:
        segments.append(_words_to_segment(cur))
    return words, segments


def _drop_bracketed_annotations(words):
    """Drop [ ... ] sound annotations from the word list.

    whisper.cpp splits '[Music]' into three word entries ('[',
    'Music', ']') — a single-entry check cannot catch them, so scan
    for bracket runs and drop the whole run. A '[' ... ']' run is
    never speech. A lone bracket token with no close nearby is
    dropped too; the run is capped at 6 words so a stray '[' can
    never swallow real speech.
    """
    out, i = [], 0
    pairs = {"[": "]", "(": ")"}
    while i < len(words):
        w = words[i]["word"]
        if w in pairs:
            close = pairs[w]
            j = i + 1
            while j < len(words) and words[j]["word"] != close and j - i <= 6:
                j += 1
            if j < len(words) and words[j]["word"] == close:
                i = j + 1  # drop the whole run, brackets included
            else:
                i += 1  # lone bracket, drop just it
            continue
        if w in ("]", ")"):
            i += 1  # closing bracket with no opener
            continue
        out.append(words[i])
        i += 1
    return out


def _words_to_segment(words):
    return {"start": words[0]["start"], "end": words[-1]["end"],
            "text": " ".join(w["word"] for w in words)}


def _parse_whisper_ts(value):
    """Parse a whisper.cpp "HH:MM:SS,mmm" timestamp into milliseconds."""
    if not value:
        return None
    try:
        hms, ms = value.split(",")
        h, m, s = (int(p) for p in hms.split(":"))
        return ((h * 60 + m) * 60 + s) * 1000 + int(ms)
    except (ValueError, AttributeError):
        return None


def transcribe_whisper_cpp(wav_path, model_path, language, model="tiny"):
    """Transcribe with the whisper.cpp CLI (the Termux path).

    Runs `whisper-cli -m <model> -f <wav> -l <lang> -ml 1 -sow -oj -of
    <prefix>` and parses the JSON it writes. Returns (words, segments)
    in the same shape as transcribe(). A missing binary or a failed
    run degrades to ([], []) with a WARN, exactly like the
    faster-whisper failure path — never fake a transcript, never crash
    the job. A missing model file is different: that fails fast with a
    plain message (see require_whisper_cpp_model).
    """
    import shutil
    cli = shutil.which("whisper-cli")
    if cli is None:
        print("WARN: whisper-cli not found on PATH — transcript disabled.")
        return [], []
    require_whisper_cpp_model(model_path, model)
    print(f"Transcribing locally with whisper-cli model={model_path} ...")
    prefix = str(Path(wav_path).with_suffix(""))
    out_json = Path(prefix + ".json")
    lang = language or "auto"
    cmd = [cli, "-m", str(model_path), "-f", str(wav_path),
           "-l", lang, "-ml", "1", "-sow", "-oj", "-of", prefix]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        print(f"WARN: whisper-cli failed to start ({type(e).__name__}: {e}) — "
              "falling back to audio-only scoring, no captions.")
        return [], []
    if r.returncode != 0:
        print(f"WARN: whisper-cli failed (exit {r.returncode}) — "
              "falling back to audio-only scoring, no captions.")
        print((r.stderr or "")[-1000:], file=sys.stderr)
        return [], []
    try:
        data = json.loads(out_json.read_text(encoding="utf-8"))
        words, segs = parse_whisper_cpp_json(data)
    except Exception as e:
        print(f"WARN: could not read whisper-cli output "
              f"({type(e).__name__}: {e}) — falling back to audio-only "
              "scoring, no captions.")
        return [], []
    finally:
        try:
            out_json.unlink(missing_ok=True)
        except OSError:
            pass
    print(f"  transcript: {len(words)} words, {len(segs)} segments")
    return words, segs


def _talking_window(t, end, words, segments):
    """Score one window for talking-head video (transcript-based).

    Returns (score, reasons). Windows with no speech score 0.
    """
    w_in = [w for w in words if t <= w["start"] < end]
    if not w_in:
        return 0.0, ["no speech in window"]
    text = " ".join(w["word"] for w in w_in).lower()
    seg_in = [s for s in segments if s["start"] < end and s["end"] > t]
    seg_texts = [s["text"].strip().lower() for s in seg_in if s["text"].strip()]
    # Hook: the window opens on a strong line or a question.
    hook = 0.0
    if seg_texts:
        first = seg_texts[0]
        if any(first.startswith(h) for h in HOOK_STARTS):
            hook = 1.0
        elif any(first.startswith(q) for q in QUESTION_STARTS):
            hook = 0.8
        elif "?" in first:
            hook = 0.7
    # Questions anywhere in the window.
    n_q = sum(1 for s in seg_texts
              if any(s.startswith(q) for q in QUESTION_STARTS) or "?" in s)
    q_n = min(1.0, n_q / 2.0)
    # Quotable lines: short, punchy, with a strong word.
    n_ql = 0
    for s in seg_texts:
        n = len(s.split())
        if 5 <= n <= 18 and any(wd in s for wd in STRONG_WORDS):
            n_ql += 1
    ql_n = min(1.0, n_ql / 2.0)
    # Emotion / excitement (same phrase list as gameplay).
    exc = sum(text.count(p) * pts for p, pts in EXCITEMENT.items())
    exc_n = min(1.0, exc / 6.0)
    # Story peaks: narrative turns.
    story = sum(text.count(m) for m in STORY_MARKERS)
    story_n = min(1.0, story / 3.0)
    # Clean cut: window starts/ends on a sentence boundary.
    completeness = 0.0
    if seg_in:
        if abs(seg_in[0]["start"] - t) <= 1.5:
            completeness += 0.5
        if abs(seg_in[-1]["end"] - end) <= 1.5:
            completeness += 0.5
    score = (0.30 * hook + 0.20 * ql_n + 0.15 * q_n + 0.15 * exc_n
             + 0.10 * story_n + 0.10 * completeness)
    reasons = []
    if hook >= 0.8:
        reasons.append("strong opening line")
    if ql_n > 0:
        reasons.append(f"quotable line ({n_ql})")
    if q_n > 0:
        reasons.append(f"question ({n_q})")
    if exc_n > 0:
        reasons.append(f"emotional speech ({exc} pts)")
    if story_n > 0:
        reasons.append(f"story turn ({story})")
    if completeness >= 1.0:
        reasons.append("clean cut on sentence boundaries")
    if not reasons:
        reasons.append("steady speech")
    return round(score, 3), reasons


def window_scores(duration, energy, words, segments, clip_seconds, mode):
    step = 10.0
    windows = []
    t = 0.0
    while t < duration:
        end = min(duration, t + clip_seconds)
        if end - t < 8:  # too short a tail to be a clip
            break
        w_in = [w for w in words if t <= w["start"] < end]
        excerpt = " ".join(w["word"] for w in w_in)[:220]
        if mode == "talking":
            score, reasons = _talking_window(t, end, words, segments)
            windows.append({"start": t, "end": end, "score": score,
                            "energy": None, "excitement": None,
                            "density": None,
                            "reason": "; ".join(reasons),
                            "excerpt": excerpt})
        else:
            # Gameplay scoring, unchanged from slice 1:
            # audio energy + excited speech + speech density.
            e_vals = energy[int(t):int(end)] or [0]
            e = sum(e_vals) / len(e_vals)
            text = " ".join(w["word"] for w in w_in).lower()
            exc = 0
            for phrase, pts in EXCITEMENT.items():
                exc += text.count(phrase) * pts
            exc_n = min(1.0, exc / 6.0)
            density = min(1.0, (len(w_in) / max(1.0, end - t)) / 2.5)
            if words:
                score = 0.5 * e + 0.3 * exc_n + 0.2 * density
            else:
                score = e
            reasons = []
            if e > 0.55:
                reasons.append(f"loud moment (energy {e:.2f})")
            if exc_n > 0:
                reasons.append(f"excited speech ({exc} pts)")
            if density > 0.5:
                reasons.append(f"dense speech ({len(w_in)} words)")
            if not reasons:
                reasons.append("steady gameplay")
            windows.append({"start": t, "end": end, "score": round(score, 3),
                            "energy": round(e, 3), "excitement": round(exc_n, 3),
                            "density": round(density, 3),
                            "reason": "; ".join(reasons),
                            "excerpt": excerpt})
        t += step
    return windows


def pick_top(windows, n, clip_seconds):
    ranked = sorted(windows, key=lambda w: w["score"], reverse=True)
    picked, taken = [], []
    min_gap = clip_seconds / 2.0
    for w in ranked:
        if all(abs(w["start"] - s) >= min_gap for s in taken):
            picked.append(w)
            taken.append(w["start"])
        if len(picked) >= n:
            break
    return sorted(picked, key=lambda w: w["start"])


# --- Scoring handoff packet (Slice 1: browser-model clip scoring) ---
#
# The mini keeps doing the video work; the browser model does the
# judging. The packet is everything the model needs to judge a
# candidate, and nothing it doesn't: the full window transcript, the
# sentence or two around it, the clipper's existing signals, and the
# rule score + reason. No model, network, or API call lives here —
# this slice only packages data the clipper already produces.
# Slice 2 (browser scoring) reads scoring-packet.json.
SCORING_PACKET_VERSION = 1
# Prompt template version. Slice 2 defines the scoring prompt; the
# version travels with the packet so a later re-score uses the same
# wording the original ranking used.
SCORING_PROMPT_VERSION = "v1"
SCORING_PACKET_FILENAME = "scoring-packet.json"
SCORING_SHORTLIST_SIZE = 25
SCORING_CONTEXT_SEGMENTS = 2


def _window_transcript(words, start, end):
    """All words spoken inside a window, in order (the full transcript,
    not the 220-char truncated excerpt clips.json carries)."""
    return " ".join(w["word"] for w in words if start <= w["start"] < end)


def _context_lines(segments, start, end, count=SCORING_CONTEXT_SEGMENTS):
    """The sentence or two just before / just after a window, from the
    transcript segments, so the judge can tell whether the moment
    stands on its own. Only fully-before segments (end <= start) feed
    context_before and fully-after segments (start >= end) feed
    context_after; a segment straddling a boundary is left out — its
    in-window words are already in the transcript."""
    before = [s["text"].strip() for s in segments
              if s["end"] <= start and s["text"].strip()]
    after = [s["text"].strip() for s in segments
             if s["start"] >= end and s["text"].strip()]
    return " ".join(before[-count:]), " ".join(after[:count])


def build_scoring_packet(*, job, created_utc, source, source_duration,
                         clip_seconds, mode_requested, mode, scoring,
                         windows, words, segments, mode_fallback=None,
                         whisper_model=None, whisper_used=False,
                         title=None,
                         shortlist_size=SCORING_SHORTLIST_SIZE):
    """Build the scoring handoff packet for one job. Pure function.

    The shortlist is pick_top's non-overlap top set (picks cannot
    overlap by more than half a clip — the same rule that picks the
    clips), capped at shortlist_size and ranked best rule score first.
    Windows below the shortlist are NOT in the packet; they were never
    written to disk by the clipper (only picked clips land in
    clips.json), so nothing is deleted — the counts record what was
    left out. The title is included only when the job has one; it is
    never invented.
    """
    shortlist = pick_top(windows, shortlist_size, clip_seconds)
    ranked = sorted(shortlist, key=lambda w: w["score"], reverse=True)
    candidates = []
    for i, w in enumerate(ranked, 1):
        before, after = _context_lines(segments, w["start"], w["end"])
        candidates.append({
            "rank": i,
            "start": round(w["start"], 2),
            "end": round(w["end"], 2),
            "duration": round(w["end"] - w["start"], 2),
            "transcript": _window_transcript(words, w["start"], w["end"]),
            "context_before": before,
            "context_after": after,
            "excerpt": w["excerpt"],
            "scores": {"energy": w["energy"], "excitement": w["excitement"],
                       "density": w["density"]},
            "rule_score": w["score"],
            "rule_reason": w["reason"],
            "mode": mode,
        })
    packet = {
        "packet_version": SCORING_PACKET_VERSION,
        "prompt_version": SCORING_PROMPT_VERSION,
        "job": job,
        "created_utc": created_utc,
        "source": str(source),
        "source_duration": round(source_duration, 2),
        "clip_seconds": clip_seconds,
        "mode_requested": mode_requested,
        "mode": mode,
        "mode_fallback": mode_fallback,
        "whisper_model": whisper_model,
        "whisper_used": whisper_used,
        "scoring": scoring,
        "counts": {
            "total_windows": len(windows),
            "shortlist_size": len(candidates),
            "rule_cutoff": (min(c["rule_score"] for c in candidates)
                            if candidates else None),
        },
        "candidates": candidates,
    }
    if title:
        packet["title"] = title
    return packet


def write_scoring_packet(job_dir, packet):
    """Write scoring-packet.json next to clips.json. Returns the path."""
    path = Path(job_dir) / SCORING_PACKET_FILENAME
    path.write_text(json.dumps(packet, indent=2), encoding="utf-8")
    return path


def fmt_ts(sec):
    sec = max(0.0, sec)
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def fmt_mmss(sec):
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


def write_clip_srt(words, start, end, path):
    """Word-level captions for one clip, timestamps relative to clip start."""
    w_in = [w for w in words if start <= w["start"] < end]
    if not w_in:
        return None
    # group into caption lines: <= 8 words or <= 2.5s
    lines, cur = [], []
    for w in w_in:
        cur.append(w)
        if len(cur) >= 8 or (cur and w["end"] - cur[0]["start"] > 2.5):
            lines.append(cur)
            cur = []
    if cur:
        lines.append(cur)
    out = []
    for i, grp in enumerate(lines, 1):
        s = grp[0]["start"] - start
        e = grp[-1]["end"] - start
        txt = " ".join(g["word"] for g in grp).strip()
        out.append(f"{i}\n{fmt_ts(s)} --> {fmt_ts(e)}\n{txt}\n")
    Path(path).write_text("\n".join(out), encoding="utf-8")
    return str(path)


def draft_copy(w, idx, mode="gameplay"):
    excerpt = w["excerpt"].strip()
    if excerpt:
        frag = excerpt[:60].strip()
        title = f"{frag}…" if len(excerpt) > 60 else frag
    else:
        title = f"Clip — {fmt_mmss(w['start'])}"
    tags = TALKING_HASHTAGS if mode == "talking" else HASHTAGS
    desc = (f"Clip ({fmt_mmss(w['start'])}–{fmt_mmss(w['end'])}). "
            f"{w['reason'].capitalize()}. "
            + (" ".join(tags)))
    return title, desc, tags


def cut_clip(src, w, out_mp4, srt_path, width, height):
    vf_parts = []
    if width and height and width >= height * 9 / 16:
        vf_parts.append("crop=ih*9/16:ih")
    else:
        vf_parts.append("scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280")
    vf_parts.append("scale=720:1280")
    vf_parts.append("fps=30")
    if srt_path:
        esc = str(srt_path).replace("'", r"\'").replace(":", r"\:")
        style = ("FontSize=22,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,"
                 "BorderStyle=1,Outline=2,Alignment=2,MarginV=110")
        vf_parts.append(f"subtitles='{esc}':force_style='{style}'")
    vf = ",".join(vf_parts)
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(round(w["start"], 3)),
           "-t", str(round(w["end"] - w["start"], 3)), "-i", str(src),
           "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out_mp4)]
    run(cmd)


def download_url(url, dest_dir):
    """Download a video URL with yt-dlp into a fresh subdir, and return
    the file yt-dlp actually wrote.

    The subdir matters: input/ also holds other videos (uploads, earlier
    downloads), and picking "newest file in input/" can silently grab
    the wrong one — e.g. an upload that landed seconds earlier. A fresh
    dir per download makes the result unambiguous.
    """
    import shutil
    from datetime import datetime, timezone
    ytdlp = shutil.which("yt-dlp") or str(Path(__file__).parent / "venv" / "bin" / "yt-dlp")
    if not Path(ytdlp).exists():
        raise SystemExit("yt-dlp not found — install it (pip install yt-dlp) or pass a local file.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dl_dir = Path(dest_dir) / f"dl-{stamp}"
    dl_dir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(dl_dir / "%(title)s.%(ext)s")
    run([ytdlp, "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
         "--no-playlist", "-o", out_tmpl, url])
    video = [f for f in sorted(dl_dir.glob("*"))
             if f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
    if not video:
        raise SystemExit("yt-dlp finished but no video file found.")
    return video[-1]


def main():
    ap = argparse.ArgumentParser(description="HHS Clips — local clipper (no posting)")
    ap.add_argument("input", nargs="?", help="local video file")
    ap.add_argument("--url", help="video URL to download with yt-dlp instead of a local file")
    ap.add_argument("--out", default=None, help="job output dir (default output/<timestamp>)")
    ap.add_argument("--num-clips", type=int, default=5)
    ap.add_argument("--clip-seconds", type=int, default=30)
    ap.add_argument("--model", default="tiny",
                    help="model name: tiny | small | base — same name on both "
                         "backends; whisper-cpp reads models/ggml-<model>.bin")
    ap.add_argument("--transcriber", default="auto",
                    choices=["auto", "faster-whisper", "whisper-cpp"],
                    help="transcription backend: auto (faster-whisper when "
                         "installed, else whisper-cli), faster-whisper, or "
                         "whisper-cpp (the Termux path — see TERMUX.md)")
    ap.add_argument("--model-path", default=None,
                    help="ggml model file for --transcriber whisper-cpp "
                         "(default models/ggml-<model>.bin)")
    ap.add_argument("--language", default="en")
    ap.add_argument("--mode", default="auto", choices=["gameplay", "talking", "auto"],
                    help="scoring mode: gameplay (audio spikes + excited speech), "
                         "talking (transcript hooks/quotes/questions), "
                         "auto (talking when speech is dense, else gameplay)")
    ap.add_argument("--i-own-it", action="store_true",
                    help="REQUIRED: attest you own this video or have rights "
                         "to post clips from it. Without it the job is refused.")
    ap.add_argument("--no-whisper", action="store_true",
                    help="skip transcription; score on audio energy only")
    ap.add_argument("--title", default=None,
                    help="stream title/topic for the scoring packet "
                         "(optional; omitted from the packet when not given)")
    args = ap.parse_args()

    # Own-content gate: refuse BEFORE anything is downloaded or cut.
    if not args.i_own_it:
        print("Refused: this app is for your own videos only.", file=sys.stderr)
        print("Re-run with --i-own-it to confirm you own this video or have",
              file=sys.stderr)
        print("rights to post clips from it.", file=sys.stderr)
        raise SystemExit(2)

    root = Path(__file__).resolve().parent
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    attested_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    job_dir = Path(args.out) if args.out else root / "output" / f"job-{stamp}"
    job_dir.mkdir(parents=True, exist_ok=True)
    (root / "input").mkdir(exist_ok=True)

    # A missing local file fails here, before backend setup, so a typo
    # says "input not found" instead of a misleading backend error.
    # A URL is only downloaded after the backend check below, so a
    # missing model still fails before any download.
    if args.input:
        src = Path(args.input)
        if not src.exists():
            raise SystemExit(f"input not found: {src}")
    elif not args.url:
        ap.error("give a local video file or --url")

    # Pick the transcription backend before anything is cut, so a
    # missing backend / missing model file fails fast with a plain
    # message instead of halfway through a job. --no-whisper needs
    # no backend at all.
    transcriber = None
    model_path = None
    if not args.no_whisper:
        transcriber = resolve_transcriber(args.transcriber)
        if transcriber == "whisper-cpp":
            model_path = args.model_path or default_model_path(args.model)
            require_whisper_cpp_model(model_path, args.model)
        print(f"  transcriber: {transcriber}"
              + (f" ({model_path})" if model_path else ""))

    if args.url:
        print(f"Downloading {args.url} ...")
        src = download_url(args.url, root / "input")

    print(f"Source: {src}")
    info = probe(src)
    print(f"  duration={info['duration']:.1f}s {info['width']}x{info['height']} "
          f"audio={info['has_audio']} video={info['has_video']}")
    if info["duration"] < 15:
        raise SystemExit("source too short to clip (<15s)")
    clip_seconds = min(args.clip_seconds, int(info["duration"]))
    if info["duration"] < args.clip_seconds:
        print(f"  note: source shorter than {args.clip_seconds}s, using {clip_seconds}s clips")

    wav = job_dir / "_audio.wav"
    words, segs = [], []
    whisper_used = False
    if info["has_audio"]:
        extract_wav(src, wav)
        energy = rms_per_second(wav)
        if not args.no_whisper:
            if transcriber == "whisper-cpp":
                words, segs = transcribe_whisper_cpp(wav, model_path,
                                                      args.language, args.model)
            else:
                words, segs = transcribe(wav, args.model, args.language)
            whisper_used = bool(words)
            if not whisper_used:
                print("  WARN: transcription produced no words — falling back to audio-only scoring")
    else:
        print("  WARN: source has no audio track — scoring will be flat, no captions")
        energy = [0.0] * int(info["duration"])

    mode = resolve_mode(args.mode, words, info["duration"])
    mode_fallback = None
    if mode == "talking" and not words:
        print("  WARN: talking mode needs a transcript — falling back to gameplay scoring")
        mode = "gameplay"
        mode_fallback = "talking requested but no transcript; fell back to gameplay scoring"
    print(f"  mode: {mode} (requested: {args.mode})"
          + (f" — {mode_fallback}" if mode_fallback else ""))

    windows = window_scores(info["duration"], energy, words, segs,
                            clip_seconds, mode)
    picked = pick_top(windows, args.num_clips, clip_seconds)
    print(f"Picked {len(picked)} clips (of {len(windows)} windows)")

    clips = []
    for i, w in enumerate(picked, 1):
        clip_id = f"clip-{i:02d}"
        srt = write_clip_srt(words, w["start"], w["end"], job_dir / f"{clip_id}.srt") \
            if words else None
        mp4 = job_dir / f"{clip_id}.mp4"
        print(f"[{i}/{len(picked)}] {fmt_mmss(w['start'])}–{fmt_mmss(w['end'])} "
              f"score={w['score']} ({w['reason']})")
        cut_clip(src, w, mp4, srt, info["width"], info["height"])
        title, desc, tags = draft_copy(w, i, mode)
        clips.append({
            "id": clip_id, "job": job_dir.name, "rank": i,
            "start": round(w["start"], 2), "end": round(w["end"], 2),
            "duration": round(w["end"] - w["start"], 2),
            "score": w["score"],
            "scores": {"energy": w["energy"], "excitement": w["excitement"],
                       "density": w["density"]},
            "reason": w["reason"], "excerpt": w["excerpt"],
            "video": f"{clip_id}.mp4",
            "srt": f"{clip_id}.srt" if srt else None,
            "draft_title": title, "draft_description": desc,
            "draft_hashtags": tags, "status": "pending",
        })

    # Manifest lists clips best-first (rank = score rank); file names stay
    # timeline-based (clip-02 can outrank clip-01).
    clips.sort(key=lambda c: c["score"], reverse=True)
    for i, c in enumerate(clips, 1):
        c["rank"] = i

    if mode == "talking":
        scoring_desc = "talking-head: hooks + quotable lines + questions + emotion + story peaks"
    elif whisper_used:
        scoring_desc = "audio energy + excited speech + speech density"
    else:
        scoring_desc = "audio energy only (no transcript)"
    manifest = {
        "job": job_dir.name, "created_utc": stamp,
        "source": str(src), "source_duration": round(info["duration"], 2),
        "clip_seconds": clip_seconds, "num_clips": len(clips),
        "owned": True, "attested_at": attested_at,
        "mode_requested": args.mode, "mode": mode,
        "mode_fallback": mode_fallback,
        "whisper_model": None if args.no_whisper else args.model,
        "transcriber": transcriber,
        "whisper_used": whisper_used,
        "scoring": scoring_desc,
        "clips": clips,
    }
    (job_dir / "clips.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    packet = build_scoring_packet(
        job=job_dir.name, created_utc=stamp, source=src,
        source_duration=info["duration"], clip_seconds=clip_seconds,
        mode_requested=args.mode, mode=mode, scoring=scoring_desc,
        windows=windows, words=words, segments=segs,
        mode_fallback=mode_fallback,
        whisper_model=None if args.no_whisper else args.model,
        whisper_used=whisper_used, title=args.title)
    packet_path = write_scoring_packet(job_dir, packet)
    print(f"Scoring packet: {packet_path} "
          f"({len(packet['candidates'])} candidates of {len(windows)} windows)")
    if wav.exists():
        wav.unlink()
    print(f"\nDone. Job dir: {job_dir}")
    print(f"Manifest: {job_dir / 'clips.json'}")
    print("Next: python3 server.py  — then open http://localhost:8000 to approve clips.")
    print("Posting is approval-gated (posters/gate.py): only approved clips post.")


if __name__ == "__main__":
    main()
