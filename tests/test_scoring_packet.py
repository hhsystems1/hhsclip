#!/usr/bin/env python3
"""HHS Clips scoring-packet tests (Slice 1: browser-model clip scoring).
Run:  python3 -m unittest discover -s tests -v

- Shortlist: top 25 windows by rule score, best-first, non-overlapping
  by the clipper's existing pick_top rule (starts >= clip_seconds / 2
  apart); a job with fewer windows than the shortlist still produces
  a valid packet.
- Packet fields: present for gameplay and talking modes (talking
  carries null energy/excitement/density, as clips.json does);
  prompt_version travels with the packet; the title is included only
  when the job has one, never invented.
- Context lines: the sentence or two just before / just after the
  window, from the transcript segments — never the window's own text.
- Packet matches clips.json: a full main() run (ffmpeg/transcription
  mocked) writes scoring-packet.json next to clips.json, and the top
  packet candidate matches the top clip on start/end/score/reason.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clipper


def _words_over(duration, step=2.0, hot_spots=()):
    """Words spread evenly over a duration. hot_spots is a set of
    second-marks where excited phrases are placed instead."""
    out, t, i = [], 0.0, 0
    filler = ["the", "game", "continues", "here", "we", "go", "on"]
    hot = iter(["let's", "go", "wow", "insane", "let's", "go"])
    while t < duration:
        if any(abs(t - h) < 1.0 for h in hot_spots):
            try:
                word = next(hot)
            except StopIteration:
                word = filler[i % len(filler)]
        else:
            word = filler[i % len(filler)]
        out.append({"start": round(t, 2), "end": round(t + 0.3, 2),
                    "word": word})
        t += step
        i += 1
    return out


def _segments_every(duration, step=30.0):
    segs, t, i = [], 0.0, 0
    while t < duration:
        segs.append({"start": t, "end": min(duration, t + step),
                     "text": f"Segment {i} talks about the plan."})
        t += step
        i += 1
    return segs


def _gameplay_windows(duration=600.0, clip_seconds=30):
    # Energy alternates 0.8 / 0.25 in 25s blocks, so windows score
    # differently across the timeline.
    energy = [0.8 if (i // 25) % 2 == 0 else 0.25
              for i in range(int(duration))]
    words = _words_over(duration, step=2.5)
    segs = _segments_every(duration)
    return clipper.window_scores(duration, energy, words, segs,
                                 clip_seconds, "gameplay"), words, segs


class ShortlistTest(unittest.TestCase):
    def test_shortlist_size_order_and_non_overlap(self):
        windows, words, segs = _gameplay_windows()
        packet = clipper.build_scoring_packet(
            job="job-test", created_utc="20261126-000000",
            source="input/x.webm", source_duration=600.0, clip_seconds=30,
            mode_requested="auto", mode="gameplay",
            scoring="audio energy + excited speech + speech density",
            windows=windows, words=words, segments=segs)
        cands = packet["candidates"]
        self.assertEqual(len(cands), 25)
        self.assertEqual(packet["counts"]["total_windows"], len(windows))
        self.assertEqual(packet["counts"]["shortlist_size"], 25)
        scores = [c["rule_score"] for c in cands]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual([c["rank"] for c in cands], list(range(1, 26)))
        self.assertEqual(packet["counts"]["rule_cutoff"], min(scores))
        starts = sorted(c["start"] for c in cands)
        for a, b in zip(starts, starts[1:]):
            self.assertGreaterEqual(b - a, 15.0)  # clip_seconds / 2

    def test_small_job_still_valid_packet(self):
        windows, words, segs = _gameplay_windows(duration=40.0)
        packet = clipper.build_scoring_packet(
            job="job-small", created_utc="20261126-000000",
            source="input/x.webm", source_duration=40.0, clip_seconds=30,
            mode_requested="auto", mode="gameplay", scoring="audio energy",
            windows=windows, words=words, segments=segs)
        # 40s gives 4 windows (starts 0/10/20/30); pick_top's 15s
        # min gap keeps at most two of them, well under the 25 cap.
        self.assertEqual(packet["counts"]["total_windows"], 4)
        self.assertLessEqual(packet["counts"]["shortlist_size"], 2)
        self.assertGreaterEqual(packet["counts"]["shortlist_size"], 1)
        self.assertEqual(len(packet["candidates"]),
                         packet["counts"]["shortlist_size"])
        # Cutoff is the lowest rule score in the shortlist (candidates
        # are ranked best-first, so it is the last one).
        self.assertEqual(packet["counts"]["rule_cutoff"],
                         packet["candidates"][-1]["rule_score"])

    def test_empty_windows_valid_packet(self):
        packet = clipper.build_scoring_packet(
            job="job-empty", created_utc="20261126-000000",
            source="input/x.webm", source_duration=20.0, clip_seconds=30,
            mode_requested="auto", mode="gameplay", scoring="audio energy",
            windows=[], words=[], segments=[])
        self.assertEqual(packet["candidates"], [])
        self.assertEqual(packet["counts"]["total_windows"], 0)
        self.assertIsNone(packet["counts"]["rule_cutoff"])


class PacketFieldsTest(unittest.TestCase):
    def test_gameplay_packet_fields(self):
        windows, words, segs = _gameplay_windows(duration=150.0)
        packet = clipper.build_scoring_packet(
            job="job-20261119-050858", created_utc="20261119-050858",
            source="/tmp/gta5-prologue-test.webm", source_duration=150.0,
            clip_seconds=30, mode_requested="auto", mode="gameplay",
            scoring="audio energy + excited speech + speech density",
            windows=windows, words=words, segments=segs,
            whisper_model="tiny", whisper_used=True)
        self.assertEqual(packet["packet_version"], 1)
        self.assertEqual(packet["prompt_version"], "v1")
        self.assertEqual(packet["job"], "job-20261119-050858")
        self.assertEqual(packet["mode"], "gameplay")
        self.assertEqual(packet["mode_requested"], "auto")
        self.assertTrue(packet["whisper_used"])
        self.assertNotIn("title", packet)  # never invented
        c = packet["candidates"][0]
        for key in ("rank", "start", "end", "duration", "transcript",
                    "context_before", "context_after", "excerpt",
                    "scores", "rule_score", "rule_reason", "mode"):
            self.assertIn(key, c)
        self.assertIsInstance(c["scores"]["energy"], float)
        # The packet's rule score is the window's rule score, unchanged.
        same = [w for w in windows if w["start"] == c["start"]][0]
        self.assertEqual(c["rule_score"], same["score"])
        self.assertEqual(c["rule_reason"], same["reason"])
        # Transcript is the full window text; the excerpt is the same
        # text truncated to 220 chars (identical here — the window is
        # sparse — but the transcript is never shorter than the excerpt).
        expected_text = " ".join(
            w["word"] for w in words if c["start"] <= w["start"] < c["end"])
        self.assertEqual(c["transcript"], expected_text)
        self.assertEqual(c["excerpt"], expected_text[:220])

    def test_talking_packet_has_null_signals(self):
        text = ("Here's the secret nobody tells you about pricing. "
                "Why do most people get this wrong? It turns out the "
                "answer is simple and it changed everything. ")
        words, segs = [], []
        t = 0.0
        for rep in range(4):
            for w in text.split():
                words.append({"start": round(t, 2), "end": round(t + 0.3, 2),
                              "word": w})
                t += 0.35
            segs.append({"start": round(t - len(text.split()) * 0.35, 2),
                         "end": round(t, 2), "text": text.strip()})
        windows = clipper.window_scores(150.0, [0.3] * 150, words, segs,
                                        30, "talking")
        packet = clipper.build_scoring_packet(
            job="job-talk", created_utc="20261126-000000",
            source="input/talk.mp4", source_duration=150.0, clip_seconds=30,
            mode_requested="talking", mode="talking",
            scoring="talking-head", windows=windows, words=words,
            segments=segs)
        self.assertEqual(packet["mode"], "talking")
        for c in packet["candidates"]:
            self.assertIsNone(c["scores"]["energy"])
            self.assertIsNone(c["scores"]["excitement"])
            self.assertIsNone(c["scores"]["density"])

    def test_title_included_only_when_given(self):
        windows, words, segs = _gameplay_windows(duration=150.0)
        base = dict(job="job-t", created_utc="20261126-000000",
                    source="input/x.webm", source_duration=150.0,
                    clip_seconds=30, mode_requested="auto",
                    mode="gameplay", scoring="audio energy",
                    windows=windows, words=words, segments=segs)
        self.assertNotIn("title", clipper.build_scoring_packet(**base))
        with_title = clipper.build_scoring_packet(title="GTA V Prologue",
                                                  **base)
        self.assertEqual(with_title["title"], "GTA V Prologue")


class ContextLinesTest(unittest.TestCase):
    def test_context_comes_from_either_side(self):
        segs = [
            {"start": 0.0, "end": 10.0, "text": "Before the moment."},
            {"start": 12.0, "end": 28.0, "text": "Setup line here."},
            {"start": 32.0, "end": 58.0, "text": "Inside the window words."},
            {"start": 62.0, "end": 80.0, "text": "After the moment."},
            {"start": 82.0, "end": 95.0, "text": "Later still."},
        ]
        words = []
        for s in segs:
            t = s["start"]
            for w in s["text"].split():
                words.append({"start": round(t, 2), "end": round(t + 0.3, 2),
                              "word": w})
                t += 0.4
        windows = [{"start": 30.0, "end": 60.0, "score": 0.9,
                    "energy": 0.8, "excitement": 0.5, "density": 0.6,
                    "reason": "loud moment", "excerpt": "Inside the window"}]
        packet = clipper.build_scoring_packet(
            job="job-ctx", created_utc="20261126-000000",
            source="input/x.webm", source_duration=120.0, clip_seconds=30,
            mode_requested="auto", mode="gameplay", scoring="audio energy",
            windows=windows, words=words, segments=segs)
        c = packet["candidates"][0]
        self.assertIn("Setup line here.", c["context_before"])
        self.assertIn("Before the moment.", c["context_before"])
        self.assertIn("After the moment.", c["context_after"])
        self.assertIn("Later still.", c["context_after"])
        # The window's own segment text is not context.
        self.assertNotIn("Inside the window", c["context_before"])
        self.assertNotIn("Inside the window", c["context_after"])


class PacketMatchesManifestTest(unittest.TestCase):
    def test_main_writes_packet_matching_clips_json(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            src = tmp / "dummy.mp4"
            src.write_bytes(b"\x00" * 64)
            out = tmp / "job-20261126-test"
            words = _words_over(150.0, step=2.0, hot_spots=(54, 56, 58, 60))
            segs = _segments_every(150.0)
            energy = [0.3] * 150
            for i in range(50, 80):
                energy[i] = 0.95
            argv = ["clipper.py", str(src), "--i-own-it", "--out", str(out),
                    "--mode", "gameplay", "--num-clips", "5",
                    "--title", "Test Stream"]
            with mock.patch.object(clipper, "probe", return_value={
                    "duration": 150.0, "width": 1920, "height": 1080,
                    "has_audio": True, "has_video": True}), \
                 mock.patch.object(clipper, "resolve_transcriber",
                                   return_value="faster-whisper"), \
                 mock.patch.object(clipper, "extract_wav"), \
                 mock.patch.object(clipper, "rms_per_second",
                                   return_value=energy), \
                 mock.patch.object(clipper, "transcribe",
                                   return_value=(words, segs)), \
                 mock.patch.object(clipper, "cut_clip"), \
                 mock.patch.object(sys, "argv", argv):
                clipper.main()
            manifest = json.loads((out / "clips.json").read_text())
            packet = json.loads((out / "scoring-packet.json").read_text())
            self.assertEqual(packet["job"], out.name)
            self.assertEqual(packet["title"], "Test Stream")
            self.assertEqual(packet["counts"]["total_windows"], 15)
            # Shortlist is pick_top's non-overlap top set (cap 25), not
            # the 5 clips: 15 windows 10s apart with a 15s min gap.
            expected = clipper.pick_top(
                clipper.window_scores(150.0, energy, words, segs, 30,
                                      "gameplay"), 25, 30)
            self.assertEqual(packet["counts"]["shortlist_size"],
                             len(expected))
            # Top candidate matches the top clip exactly.
            top_cand, top_clip = packet["candidates"][0], manifest["clips"][0]
            self.assertEqual(top_cand["start"], top_clip["start"])
            self.assertEqual(top_cand["end"], top_clip["end"])
            self.assertEqual(top_cand["rule_score"], top_clip["score"])
            self.assertEqual(top_cand["rule_reason"], top_clip["reason"])
            # Every clip in the manifest is in the packet with the same
            # rule score, and the packet's top 5 are the manifest's 5.
            by_start = {c["start"]: c for c in packet["candidates"]}
            for clip in manifest["clips"]:
                self.assertIn(clip["start"], by_start)
                self.assertEqual(by_start[clip["start"]]["rule_score"],
                                 clip["score"])
            self.assertEqual(
                [(c["start"], c["rule_score"])
                 for c in packet["candidates"][:5]],
                [(c["start"], c["score"]) for c in manifest["clips"]])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
