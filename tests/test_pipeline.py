"""Run with: python -m unittest discover tests"""

import base64
import json
import shutil
import unittest
from unittest import mock

from pipeline import cli, config, s3_voiceover
from pipeline.common import extract_json, parse_sections, sentences_with_paragraphs, split_sentences
from pipeline.lint import lint_script, lint_title, validate_chapters
from pipeline.s4_shots import group
from pipeline.s6_assemble import caption_chunks


class TextTests(unittest.TestCase):
    def test_sentences_keep_numbers_and_abbreviations(self):
        para = "Costco Inc. earned $1.5 billion in the U.S. last year. Was it luck? No."
        self.assertEqual(split_sentences(para), [
            "Costco Inc. earned $1.5 billion in the U.S. last year.", "Was it luck?", "No."])

    def test_paragraph_numbers(self):
        rows = sentences_with_paragraphs("One. Two.\n\nThree.")
        self.assertEqual([r["paragraph"] for r in rows], [0, 0, 1])

    def test_sections_and_json(self):
        parts = parse_sections("===SCRIPT===\nHi.\n===FACTS===\nF1 | x | ESTIMATE\n===SOURCES===\n", ["SCRIPT", "FACTS", "SOURCES"])
        self.assertEqual(parts["SCRIPT"], "Hi.")
        self.assertEqual(extract_json('noise ===JSON===\n```json\n{"a": 1}\n```\n===END==='), {"a": 1})


class LintTests(unittest.TestCase):
    def test_flags_policy_format_and_unchecked_numbers(self):
        script = "Welcome back! Costs run $2-5 million and margins are 12%. Subscribe for more."
        errors = " ".join(lint_script(script, "F1 | margins are 12% | S1")["errors"])
        for needle in ("welcome back", "Subscribe", "$2-5 million", "Unchecked figure: 2"):
            self.assertIn(needle.lower(), errors.lower())
        self.assertNotIn("Unchecked figure: 12", errors)

    def test_years_are_not_figures(self):
        result = lint_script("In 2019 it cost $40,000.", "F1 | cost $40,000 | S1")
        self.assertEqual(result["errors"], [])

    def test_fact_line_needs_tag(self):
        errors = lint_script("It cost $40,000.", "F1 | cost $40,000 | trust me")["errors"]
        self.assertTrue(any("no source" in e for e in errors))

    def test_title_pair(self):
        self.assertEqual(lint_title("How Costco Makes Money", "THE $60 MEMBERSHIP TRICK"), [])
        problems = lint_title("How Costco Makes Money From Memberships", "COSTCO MEMBERSHIPS MONEY")
        self.assertTrue(any("repeats the title" in p for p in problems))

    def test_chapters(self):
        ok = [{"start": 0, "title": "a"}, {"start": 40, "title": "b"}, {"start": 90, "title": "c"}]
        self.assertEqual(validate_chapters(ok, 200), [])
        self.assertTrue(validate_chapters(ok[:2], 200))


class TimingTests(unittest.TestCase):
    def test_shots_cover_whole_video_and_respect_minimum(self):
        sents = [{"i": i + 1, "paragraph": i // 4, "text": "x", "start": i * 3.0, "end": i * 3.0 + 2.8} for i in range(40)]
        shots = group(sents, 121.0, min_s=10)
        self.assertEqual(shots[0]["start"], 0.0)
        self.assertEqual(shots[-1]["end"], 121.0)
        self.assertEqual(sum(len(s["sentences"]) for s in shots), 40)
        for a, b in zip(shots, shots[1:]):
            self.assertEqual(a["end"], b["start"])
        self.assertTrue(all(s["end"] - s["start"] >= 6 for s in shots))

    def test_caption_chunks(self):
        chunks = caption_chunks("A vending machine in a busy hospital hallway can quietly earn more than you think.")
        self.assertTrue(all(len(c) <= 45 for c in chunks))
        self.assertGreater(len(chunks[-1].split()), 1)


class GeminiVoiceTests(unittest.TestCase):
    def _resp(self, status, payload):
        r = mock.Mock(status_code=status, text=json.dumps(payload))
        r.json.return_value = payload
        return r

    def test_returns_pcm_and_sends_voice_and_style(self):
        pcm = b"\x01\x00" * 100
        ok = {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/L16;rate=24000",
                                                                  "data": base64.b64encode(pcm).decode()}}]}}]}
        with mock.patch.object(config, "GOOGLE_API_KEY", "k"), \
                mock.patch("pipeline.s3_voiceover.requests.post", return_value=self._resp(200, ok)) as post:
            self.assertEqual(s3_voiceover._gemini("Hello there."), pcm)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["generationConfig"]["responseModalities"], ["AUDIO"])
        self.assertEqual(body["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"], config.GEMINI_TTS_VOICE)
        self.assertTrue(body["contents"][0]["parts"][0]["text"].endswith("Hello there."))
        self.assertEqual(post.call_args.kwargs["headers"], {"x-goog-api-key": "k"})

    def test_retries_when_no_audio_then_succeeds(self):
        empty = {"candidates": [{"content": {"parts": [{"text": "no"}]}}]}
        pcm = b"\x02\x00" * 10
        ok = {"candidates": [{"content": {"parts": [{"inlineData": {"data": base64.b64encode(pcm).decode()}}]}}]}
        with mock.patch("pipeline.s3_voiceover.requests.post", side_effect=[self._resp(200, empty), self._resp(200, ok)]), \
                mock.patch("pipeline.s3_voiceover.time.sleep"):
            self.assertEqual(s3_voiceover._gemini("Hi."), pcm)

    def test_hard_error_stops(self):
        with mock.patch("pipeline.s3_voiceover.requests.post", return_value=self._resp(403, {"error": "denied"})):
            with self.assertRaises(cli.StageError):
                s3_voiceover._gemini("Hi.")

    def test_cost(self):
        self.assertAlmostEqual(config.voice_cost("gemini", 840, 14000), 0.126, places=3)


class StubEndToEnd(unittest.TestCase):
    """The whole pipeline on placeholders: no keys, no Claude, about a minute."""

    slug = "zz-test-stub-run"

    def setUp(self):
        shutil.rmtree(config.OUT / self.slug, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(config.OUT / self.slug, ignore_errors=True)

    def test_run(self):
        code = cli.main(["--stub", "new", "How vending machines make money", "--slug", self.slug, "--run"])
        self.assertEqual(code, 0)
        d = config.OUT / self.slug
        for name in ("video.mp4", "thumbnail.png", "captions.srt", "description.txt", "review/REVIEW.md"):
            self.assertTrue((d / name).exists(), name)
        self.assertIn("Not financial", (d / "description.txt").read_text())
        # Stub runs can never be approved.
        self.assertEqual(cli.main(["approve", self.slug]), 1)
        self.assertFalse((d / "approval.json").exists())
        self.assertTrue(json.loads((d / "factcheck.json").read_text())["passed"])


if __name__ == "__main__":
    unittest.main()
