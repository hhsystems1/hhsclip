#!/usr/bin/env python3
"""HHS Clips whisper.cpp backend tests — the Termux path.
Run:  python3 -m unittest discover -s tests -v

- parse_whisper_cpp_json: a hand-written -ojf JSON fixture (no binary
  needed) parses into words/segments in the same shape as the
  faster-whisper path; special tokens are dropped; segments split on
  a gap over 0.8s or after 10 words; timestamps strings are the
  fallback when offsets are missing.
- transcribe_whisper_cpp: a missing whisper-cli binary or a failed run
  degrades to ([], []) with a WARN, never a crash; a missing model
  file fails fast with a plain message naming where to get it.
- resolve_transcriber: auto prefers faster-whisper when importable,
  falls back to whisper-cli, and fails plainly when neither exists.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clipper


def _entry(word, start_ms, end_ms, with_offsets=True, tokens=None):
    """One -ojf transcription entry. With -ml 1 -sow each entry is a word."""
    def ts(ms):
        return f"{ms // 3600000:02d}:{(ms // 60000) % 60:02d}:{(ms // 1000) % 60:02d},{ms % 1000:03d}"
    e = {"timestamps": {"from": ts(start_ms), "to": ts(end_ms)},
         "text": f" {word}",
         "tokens": tokens if tokens is not None else [
             {"text": f" {word}", "id": 1, "p": 0.99, "t_dtw": start_ms}]}
    if with_offsets:
        e["offsets"] = {"from": start_ms, "to": end_ms}
    return e


def _fixture_json():
    """12 words: a short run, a >0.8s gap (segment split), a special
    token (dropped), and one entry with no offsets (timestamp fallback)."""
    entries = [
        _entry("Hello", 0, 400),
        _entry("world", 450, 800),
        _entry("this", 850, 1100),
        _entry("is", 1150, 1350),
        _entry("a", 1400, 1550),
        _entry("test", 1600, 2000),
        _entry("[_BEG_]", 2000, 2000),
        # gap of 1.5s -> new segment
        _entry("second", 3500, 3900),
        _entry("sentence", 3950, 4500),
        _entry("here", 4550, 4800),
        _entry("now", 4850, 5100),
        _entry("again", 5150, 5500),
        _entry("please", 5550, 5900),
        # no offsets -> timestamps fallback
        _entry("last", 6000, 6400, with_offsets=False),
        _entry("<|endoftext|>", 6400, 6400),
    ]
    return {"systeminfo": {}, "model": {"type": "tiny"},
            "params": {}, "result": {"language": "en"},
            "transcription": entries}


class ParseWhisperCppJsonTest(unittest.TestCase):
    def test_words_in_seconds_with_text(self):
        words, segs = clipper.parse_whisper_cpp_json(_fixture_json())
        self.assertEqual([w["word"] for w in words],
                         ["Hello", "world", "this", "is", "a", "test",
                          "second", "sentence", "here", "now", "again",
                          "please", "last"])
        self.assertEqual(words[0], {"start": 0.0, "end": 0.4, "word": "Hello"})
        self.assertEqual(words[-1], {"start": 6.0, "end": 6.4, "word": "last"})

    def test_segments_split_on_gap_and_rebuilt(self):
        words, segs = clipper.parse_whisper_cpp_json(_fixture_json())
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0]["text"], "Hello world this is a test")
        self.assertEqual(segs[0]["start"], 0.0)
        self.assertEqual(segs[0]["end"], 2.0)
        self.assertEqual(segs[1]["text"],
                         "second sentence here now again please last")

    def test_segments_cap_at_ten_words(self):
        entries = [_entry(f"w{i}", i * 300, i * 300 + 200) for i in range(25)]
        _, segs = clipper.parse_whisper_cpp_json({"transcription": entries})
        self.assertEqual([len(s["text"].split()) for s in segs], [10, 10, 5])

    def test_empty_transcription(self):
        words, segs = clipper.parse_whisper_cpp_json({"transcription": []})
        self.assertEqual((words, segs), ([], []))
        words, segs = clipper.parse_whisper_cpp_json({})
        self.assertEqual((words, segs), ([], []))

    def test_timestamp_string_fallback(self):
        data = {"transcription": [
            {"timestamps": {"from": "00:00:01,250", "to": "00:00:01,750"},
             "text": " hello"}]}
        words, segs = clipper.parse_whisper_cpp_json(data)
        self.assertEqual(words, [{"start": 1.25, "end": 1.75, "word": "hello"}])

    def test_bracketed_annotations_dropped_and_zero_span_floored(self):
        # Real whisper-cli output: [Music] arrives either as one entry
        # or split into three word entries ('[', 'Music', ']'). Neither
        # form may leak into the transcript. A zero-span word (the
        # first token of a word can have from == to) gets a 100ms floor.
        data = {"transcription": [
            _entry("[Music]", 0, 10000),
            _entry("[", 15000, 15400),
            _entry("Applause", 15400, 18000),
            _entry("]", 18000, 18500),
            _entry("Get", 42000, 42000),
            _entry("down", 42000, 42330)]}
        words, segs = clipper.parse_whisper_cpp_json(data)
        self.assertEqual([w["word"] for w in words], ["Get", "down"])
        self.assertEqual(words[0], {"start": 42.0, "end": 42.1, "word": "Get"})
        self.assertEqual(words[1], {"start": 42.0, "end": 42.33, "word": "down"})


class TranscribeWhisperCppTest(unittest.TestCase):
    def test_missing_binary_degrades(self):
        with mock.patch("shutil.which", return_value=None):
            words, segs = clipper.transcribe_whisper_cpp(
                "x.wav", "models/ggml-tiny.bin", "en")
        self.assertEqual((words, segs), ([], []))

    def test_missing_model_file_fails_plainly(self):
        with mock.patch("shutil.which", return_value="/usr/bin/whisper-cli"):
            with self.assertRaises(SystemExit) as ctx:
                clipper.transcribe_whisper_cpp(
                    "x.wav", "/nonexistent/ggml-tiny.bin", "en")
        msg = str(ctx.exception)
        self.assertIn("ggml-tiny.bin", msg)
        self.assertIn("huggingface.co", msg)

    def test_failed_run_degrades(self):
        with mock.patch("shutil.which", return_value="/usr/bin/whisper-cli"), \
             mock.patch.object(subprocess, "run",
                               return_value=mock.Mock(returncode=1,
                                                      stdout="", stderr="boom")):
            words, segs = clipper.transcribe_whisper_cpp(
                "x.wav", __file__, "en")
        self.assertEqual((words, segs), ([], []))

    def test_fake_binary_end_to_end(self):
        """A stub whisper-cli that writes canned -ojf JSON: proves the
        invocation, the parse, and that the JSON file is cleaned up."""
        tmp = Path(tempfile.mkdtemp())
        try:
            script = tmp / "whisper-cli"
            fixture = json.dumps(_fixture_json())
            script.write_text(
                "#!/bin/sh\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = \"-of\" ]; then prefix=\"$2\"; fi\n"
                "  shift\ndone\n"
                f"cat > \"$prefix.json\" <<'JSONEOF'\n{fixture}\nJSONEOF\n")
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            model = tmp / "ggml-tiny.bin"
            model.write_bytes(b"\x00")
            wav = tmp / "audio.wav"
            wav.write_bytes(b"\x00")
            with mock.patch.dict(os.environ, {"PATH": str(tmp) + ":" + os.environ["PATH"]}):
                words, segs = clipper.transcribe_whisper_cpp(
                    str(wav), str(model), "en", "tiny")
            self.assertEqual(len(words), 13)
            self.assertEqual(words[0]["word"], "Hello")
            self.assertEqual(len(segs), 2)
            self.assertFalse((tmp / "audio.json").exists())
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class ResolveTranscriberTest(unittest.TestCase):
    def test_auto_prefers_faster_whisper(self):
        with mock.patch.object(clipper, "_faster_whisper_available",
                               return_value=True), \
             mock.patch("shutil.which", return_value=None):
            self.assertEqual(clipper.resolve_transcriber("auto"),
                             "faster-whisper")

    def test_auto_falls_back_to_whisper_cpp(self):
        with mock.patch.object(clipper, "_faster_whisper_available",
                               return_value=False), \
             mock.patch("shutil.which", return_value="/usr/bin/whisper-cli"):
            self.assertEqual(clipper.resolve_transcriber("auto"),
                             "whisper-cpp")

    def test_auto_neither_available_fails_plainly(self):
        with mock.patch.object(clipper, "_faster_whisper_available",
                               return_value=False), \
             mock.patch("shutil.which", return_value=None):
            with self.assertRaises(SystemExit) as ctx:
                clipper.resolve_transcriber("auto")
        msg = str(ctx.exception)
        self.assertIn("faster-whisper", msg)
        self.assertIn("whisper-cli", msg)

    def test_explicit_whisper_cpp_missing_binary_fails(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(SystemExit) as ctx:
                clipper.resolve_transcriber("whisper-cpp")
        self.assertIn("whisper-cli", str(ctx.exception))

    def test_explicit_faster_whisper_missing_fails(self):
        with mock.patch.object(clipper, "_faster_whisper_available",
                               return_value=False):
            with self.assertRaises(SystemExit) as ctx:
                clipper.resolve_transcriber("faster-whisper")
        self.assertIn("faster-whisper", str(ctx.exception))


class MainModelCheckTest(unittest.TestCase):
    def test_missing_model_fails_before_any_download(self):
        # The model check runs before input handling / URL download:
        # download_url must never be called.
        argv = ["clipper.py", "--url", "https://example.com/v.mp4",
                "--i-own-it", "--transcriber", "whisper-cpp",
                "--model-path", "/nonexistent/ggml-tiny.bin"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch("shutil.which", return_value="/usr/bin/whisper-cli"), \
             mock.patch.object(clipper, "download_url",
                               side_effect=AssertionError("download attempted")):
            with self.assertRaises(SystemExit) as ctx:
                clipper.main()
        self.assertIn("huggingface.co", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
