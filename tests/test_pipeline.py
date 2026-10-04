"""Run with: python -m unittest discover tests"""

import base64
import json
import shutil
import unittest
from unittest import mock

from pipeline import cli, config, s3_voiceover, s6_assemble
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
        script = "Welcome back! Costs run $2-5 million and margins are 12%. Hit the bell for more."
        errors = " ".join(lint_script(script, "F1 | margins are 12% | S1")["errors"])
        for needle in ("welcome back", "hit the bell", "$2-5 million", "Unchecked figure: 2", "Intro:", "Outro:"):
            self.assertIn(needle.lower(), errors.lower())
        self.assertNotIn("Unchecked figure: 12", errors)

    def test_intro_and_outro_rules(self):
        hook = "A hallway machine can out-earn a gift shop."
        intro = "This is Who Pays Who, where we follow the money. Today, who pays for the snacks."
        outro = "If this was useful, like the video and subscribe to Who Pays Who."
        good = "\n\n".join([hook, intro, "The middle part.", outro])
        self.assertEqual(lint_script(good, "")["errors"], [])
        no_intro = " ".join(["word"] * 160) + "\n\n" + outro
        self.assertTrue(any(e.startswith("Intro:") for e in lint_script(no_intro, "")["errors"]))
        no_outro = "\n\n".join([hook, intro, "The middle part."])
        self.assertTrue(any(e.startswith("Outro:") for e in lint_script(no_outro, "")["errors"]))
        middle = "\n\n".join([hook, intro, "Subscribe to Who Pays Who now.", "The end.", outro])
        self.assertTrue(any("middle" in e for e in lint_script(middle, "")["errors"]))

    def test_years_are_not_figures(self):
        result = lint_script("In 2019 it cost $40,000.", "F1 | cost $40,000 | S1")
        self.assertEqual([e for e in result["errors"] if not e.startswith(("Intro:", "Outro:"))], [])

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

    def test_rate_limit_waits_as_long_as_google_asks_then_succeeds(self):
        limited = {"error": {"code": 429, "message": "You exceeded your current quota",
                             "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "23s"}]}}
        pcm = b"\x03\x00" * 10
        ok = {"candidates": [{"content": {"parts": [{"inlineData": {"data": base64.b64encode(pcm).decode()}}]}}]}
        with mock.patch("pipeline.s3_voiceover.requests.post",
                        side_effect=[self._resp(429, limited), self._resp(200, ok)]), \
                mock.patch("pipeline.s3_voiceover.time.sleep") as sleep:
            self.assertEqual(s3_voiceover._gemini("Hi."), pcm)
        self.assertGreaterEqual(sleep.call_args_list[-1].args[0], 24)

    def test_daily_quota_stops_at_once_with_google_details(self):
        daily = {"error": {"code": 429, "message": "Quota exceeded for metric ...requests_per_day_per_project, limit: 0"}}
        with mock.patch("pipeline.s3_voiceover.requests.post", return_value=self._resp(429, daily)) as post, \
                mock.patch("pipeline.s3_voiceover.time.sleep"):
            with self.assertRaises(cli.StageError) as cm:
                s3_voiceover._gemini("Hi.")
        self.assertEqual(post.call_count, 1)
        self.assertIn("limit: 0", str(cm.exception))
        self.assertIn("billing", str(cm.exception))

    def test_hard_error_stops(self):
        with mock.patch("pipeline.s3_voiceover.requests.post", return_value=self._resp(403, {"error": "denied"})):
            with self.assertRaises(cli.StageError):
                s3_voiceover._gemini("Hi.")

    def test_cost(self):
        self.assertAlmostEqual(config.voice_cost("gemini", 840, 14000), 0.126, places=3)


class VoiceChunkTests(unittest.TestCase):
    RATE = 24000

    def _speech(self, seconds):
        import array
        a = array.array("h", [0] * int(self.RATE * seconds))
        for i in range(len(a)):
            a[i] = 8000 if (i // 40) % 2 else -8000  # a loud square wave stands in for speech
        return a.tobytes()

    def _silence(self, seconds):
        return b"\x00\x00" * int(self.RATE * seconds)

    def test_boundaries_land_on_the_pauses_between_sentences_not_commas(self):
        # Three sentences of 40, 80 and 40 characters. The second has a short pause (a comma) in the middle.
        pcm = (self._speech(3.0) + self._silence(0.5) +
               self._speech(2.5) + self._silence(0.2) + self._speech(3.5) + self._silence(0.5) +
               self._speech(3.0))
        start, end, bounds = s3_voiceover._locate_sentences(pcm, [40, 80, 40])
        self.assertAlmostEqual(start, 0.0, delta=0.05)
        self.assertAlmostEqual(end, 13.2, delta=0.2)
        self.assertEqual(len(bounds), 2)
        self.assertAlmostEqual(bounds[0], 3.25, delta=0.1)
        self.assertAlmostEqual(bounds[1], 9.95, delta=0.1)

    def test_falls_back_to_proportional_timing_without_pauses(self):
        pcm = self._speech(10.0)
        start, end, bounds = s3_voiceover._locate_sentences(pcm, [50, 50])
        self.assertAlmostEqual(bounds[0], 5.0, delta=0.4)
        self.assertEqual(s3_voiceover._locate_sentences(self._silence(4.0), [10, 30])[2], [1.0])

    def test_chunks_hold_whole_paragraphs_and_split_a_long_one(self):
        rows = sentences_with_paragraphs("Alpha runs first. Beta runs next.\n\nGamma comes later. Delta ends it.\n\n" + " ".join(f"Long {i} here." for i in range(10)))
        chunks = s3_voiceover._chunk_rows(rows, 40)
        self.assertEqual(sum(len(c) for c in chunks), len(rows))
        self.assertEqual([r["paragraph"] for r in chunks[0]], [0, 0])  # a short paragraph is never split
        self.assertEqual([r["paragraph"] for r in chunks[1]], [1, 1])
        self.assertGreater(len(chunks), 3)  # the long paragraph was split into several
        for c in chunks:
            self.assertLessEqual(sum(len(r["text"]) + 1 for r in c), 40 + 16)
        self.assertEqual(s3_voiceover._chunk_text(chunks[0]), "Alpha runs first. Beta runs next.")

    def test_a_video_needs_far_fewer_requests_than_sentences(self):
        script = "\n\n".join(" ".join(f"This is sentence number {p}.{i} of the script." for i in range(4)) for p in range(30))
        rows = sentences_with_paragraphs(script)
        self.assertEqual(len(rows), 120)
        self.assertLess(len(s3_voiceover._chunk_rows(rows, 1500)), 12)


class AlignTests(unittest.TestCase):
    def setUp(self):
        self.sents = [{"i": 1, "text": "Costco sold $297.2 billion.", "start": 0.0, "end": 2.0},
                      {"i": 2, "text": "Fees were half of it.", "start": 2.0, "end": 4.0}]
        # What a recognizer might hear: the number written differently, one word missed.
        self.heard = [(" Costco", 0.1, 0.5), (" sold", 0.5, 0.8), (" $297.2", 0.9, 2.2), (" billion.", 2.2, 2.7),
                      (" Fees", 3.1, 3.4), (" were", 3.4, 3.6), (" of", 3.9, 4.0), (" it.", 4.0, 4.3)]

    def test_matches_words_and_fills_gaps(self):
        from pipeline import align
        wt = align.match(self.sents, self.heard, 5.0)
        self.assertEqual([len(w) for w in wt], [4, 5])
        self.assertEqual(wt[1][0], [3.1, 3.4])
        half = wt[1][2]  # "half" was not heard; it sits between "were" and "of"
        self.assertTrue(3.6 <= half[0] <= half[1] <= 3.9)
        align.apply(self.sents, wt, 5.0)
        self.assertEqual(self.sents[1]["start"], 3.1)
        self.assertEqual(self.sents[0]["end"], 3.1)

    def test_poor_match_is_rejected(self):
        from pipeline import align
        self.assertIsNone(align.match(self.sents, [(" something", 0, 1), (" else", 1, 2)], 5.0))

    def test_captions_use_word_times(self):
        import tempfile
        from pathlib import Path
        from pipeline import align
        from pipeline.s6_assemble import write_srt
        long = {"i": 1, "start": 0.0, "end": 9.0,
                "text": "Membership fees bring in about five point nine billion dollars every single year for the company."}
        n = len(long["text"].split())
        heard = [(w, 0.5 * k, 0.5 * k + 0.4) for k, w in enumerate(long["text"].split())]
        heard[8:] = [(w, a + 2.0, b + 2.0) for w, a, b in heard[8:]]  # a slow stretch mid-sentence
        align.apply([long], align.match([long], heard, 12.0), 12.0)
        self.assertEqual(len(long["words"]), n)
        out = Path(tempfile.mkdtemp()) / "c.srt"
        write_srt([long], out)
        blocks = out.read_text().strip().split("\n\n")
        second_start = blocks[1].splitlines()[1].split(" --> ")[0]
        k = len(blocks[0].splitlines()[2].split())
        self.assertEqual(second_start, s6_assemble.fmt_ts(long["words"][k][0], srt=True))


class UpscaleTests(unittest.TestCase):
    """The upscaler is an outside program; a tiny fake stands in for it here."""

    def setUp(self):
        import sys
        import tempfile
        from pathlib import Path
        from PIL import Image
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.img = self.tmp / "shot_001.png"
        Image.new("RGB", (1376, 768), "white").save(self.img)
        fake = self.tmp / "fake-upscaler"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import sys\nfrom PIL import Image\n"
            "a = sys.argv\nsrc, dst = a[a.index('-i') + 1], a[a.index('-o') + 1]\n"
            "im = Image.open(src)\nim.resize((im.width * 2, im.height * 2)).save(dst)\n")
        fake.chmod(0o755)
        self.fake = fake
        s6_assemble.find_upscaler.cache_clear()
        self.addCleanup(s6_assemble.find_upscaler.cache_clear)
        self.addCleanup(s6_assemble._upscale_failed.clear)

    def test_upscales_small_pictures_and_caches(self):
        from PIL import Image
        with mock.patch.object(config, "UPSCALER_BIN", str(self.fake)):
            out = s6_assemble.upscale(self.img)
            self.assertNotEqual(out, self.img)
            self.assertEqual(Image.open(out).size, (2752, 1536))
            self.assertEqual(s6_assemble.upscale(self.img), out)

    def test_falls_back_when_the_upscaler_fails(self):
        self.fake.write_text("#!/bin/sh\nexit 3\n")
        with mock.patch.object(config, "UPSCALER_BIN", str(self.fake)), mock.patch("pipeline.s6_assemble.log"):
            self.assertEqual(s6_assemble.upscale(self.img), self.img)

    def test_no_upscaler_means_original(self):
        with mock.patch.object(config, "UPSCALER_BIN", str(self.tmp / "missing")), \
                mock.patch("shutil.which", return_value=None):
            self.assertEqual(s6_assemble.upscale(self.img), self.img)


class FfmpegDetectionTests(unittest.TestCase):
    PLAIN = " ... zoompan  V->V\n"
    FULL = " ... zoompan  V->V\n ... subtitles  V->V\n"

    def _find(self, filters_by_path):
        s6_assemble.find_ffmpeg.cache_clear()
        try:
            with mock.patch.object(s6_assemble.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                    mock.patch.object(s6_assemble.os.path, "exists", side_effect=lambda p: p in filters_by_path), \
                    mock.patch.object(s6_assemble, "_filters", side_effect=lambda p: filters_by_path[p]), \
                    mock.patch.dict(s6_assemble.os.environ, {}, clear=False):
                return s6_assemble.find_ffmpeg()
        finally:
            s6_assemble.find_ffmpeg.cache_clear()

    def test_prefers_ffmpeg_full_when_regular_cannot_burn(self):
        full = s6_assemble.FFMPEG_FULL[0]
        self.assertEqual(self._find({"/usr/bin/ffmpeg": self.PLAIN, full: self.FULL}), (full, True))

    def test_falls_back_to_regular_without_captions(self):
        self.assertEqual(self._find({"/usr/bin/ffmpeg": self.PLAIN}), ("/usr/bin/ffmpeg", False))

    def test_none_usable(self):
        self.assertEqual(self._find({"/usr/bin/ffmpeg": ""}), ("", False))


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

    def test_revise_after_failed_factcheck(self):
        from pipeline import llm, s2_factcheck
        self.assertEqual(cli.main(["--stub", "new", "How vending machines make money", "--slug", self.slug]), 0)
        self.assertEqual(cli.main(["--stub", "script", self.slug]), 0)
        self.assertEqual(cli.main(["--stub", "factcheck", self.slug]), 0)
        d = config.OUT / self.slug
        fc = json.loads((d / "factcheck.json").read_text())
        fc["facts"][1].update(verdict="WRONG", note="the source says $4,000", fix="Say $4,000.")
        fc["facts"].append({"id": "NEW1", "verdict": "UNSUPPORTED", "claim": "earns more than the gift shop",
                            "note": "no source", "fix": "Cut it."})
        fc["policy"] = [{"quote": "nobody warned Bob", "problem": "test", "fix": "drop it"}]
        fc["passed"] = False
        (d / "factcheck.json").write_text(json.dumps(fc))
        (d / "factcheck.md").write_text("old report")

        real = llm.ask
        with mock.patch.object(s2_factcheck.llm, "ask", side_effect=real) as ask:
            self.assertEqual(cli.main(["--stub", "revise", self.slug]), 0)
        sent = ask.call_args_list[0].args[0]
        for needle in ("F2 WRONG", "the source says $4,000", "NEW1 UNSUPPORTED", "earns more than the gift shop",
                       "POLICY", "nobody warned Bob"):
            self.assertIn(needle, sent)
        self.assertEqual((d / "raw" / "v1" / "factcheck.md").read_text(), "old report")
        self.assertTrue((d / "raw" / "v1" / "script.txt").exists())
        self.assertIn("Revision 1", (d / "changes.md").read_text())
        self.assertTrue(json.loads((d / "factcheck.json").read_text())["passed"])
        # Once it passes there is nothing left to revise, unless Tejas sends editor notes.
        self.assertEqual(cli.main(["--stub", "revise", self.slug]), 0)
        self.assertFalse((d / "raw" / "v2").exists())
        notes = d / "notes.md"
        notes.write_text("Say membership card, not card.")
        with mock.patch.object(s2_factcheck.llm, "ask", side_effect=real) as ask:
            self.assertEqual(cli.main(["--stub", "revise", self.slug, "--notes", str(notes)]), 0)
        self.assertIn("EDITOR NOTES FROM TEJAS:\nSay membership card, not card.", ask.call_args_list[0].args[0])
        self.assertTrue((d / "raw" / "v2" / "script.txt").exists())

    def test_factcheck_rechecks_only_what_changed(self):
        from pipeline import llm, s2_factcheck
        for args in (["new", "How vending machines make money", "--slug", self.slug], ["script", self.slug],
                     ["factcheck", self.slug]):
            self.assertEqual(cli.main(["--stub", *args]), 0)
        d = config.OUT / self.slug
        fc = json.loads((d / "factcheck.json").read_text())
        self.assertEqual(set(fc["verified"]), {"F1", "F2", "F3"})

        script = (d / "script.txt").read_text().replace("Meet Bob.", "Meet Bob, a former bus driver.")
        (d / "script.txt").write_text(script)
        facts = (d / "facts.txt").read_text().replace("Host commissions are usually", "Host commissions are typically")
        (d / "facts.txt").write_text(facts)
        real = llm.ask
        with mock.patch.object(s2_factcheck.llm, "ask", side_effect=real) as ask:
            self.assertEqual(cli.main(["--stub", "factcheck", self.slug]), 0)
        sent = ask.call_args_list[0].args[0]
        facts_block = sent.split("<facts>")[1].split("</facts>")[0]
        self.assertIn("F3 |", facts_block)
        self.assertNotIn("F1 |", facts_block)
        scope = sent.split("SCOPE:")[1].split("<script>")[0]
        self.assertIn("Meet Bob, a former bus driver.", scope)
        self.assertNotIn("Chocolate melts", scope)
        self.assertEqual(len(json.loads((d / "factcheck.json").read_text())["facts"]), 3)

        # Nothing changed: no Claude call at all.
        with mock.patch.object(s2_factcheck.llm, "ask", side_effect=real) as ask:
            self.assertEqual(cli.main(["--stub", "factcheck", self.slug]), 0)
        ask.assert_not_called()
        # --full checks everything again.
        with mock.patch.object(s2_factcheck.llm, "ask", side_effect=real) as ask:
            self.assertEqual(cli.main(["--stub", "factcheck", self.slug, "--full"]), 0)
        self.assertIn("F1 |", ask.call_args_list[0].args[0])

    def test_migrate_old_factcheck(self):
        from pipeline import s2_factcheck
        from pipeline.common import Video
        for args in (["new", "How vending machines make money", "--slug", self.slug], ["script", self.slug],
                     ["factcheck", self.slug]):
            self.assertEqual(cli.main(["--stub", *args]), 0)
        d = config.OUT / self.slug
        fc = json.loads((d / "factcheck.json").read_text())
        for k in ("verified", "clean_sentences"):
            fc.pop(k)
        fc["facts"][0]["verdict"] = "UNSUPPORTED"
        fc["policy"] = [{"quote": "nobody warned Bob about that", "problem": "x", "fix": "y"}]
        (d / "factcheck.json").write_text(json.dumps(fc))
        s2_factcheck.migrate(Video(self.slug))
        fc = json.loads((d / "factcheck.json").read_text())
        self.assertEqual(set(fc["verified"]), {"F2", "F3"})
        flagged = s2_factcheck._h("Chocolate melts in summer, and nobody warned Bob about that.")
        self.assertNotIn(flagged, fc["clean_sentences"])
        self.assertIn(s2_factcheck._h("Meet Bob."), fc["clean_sentences"])

    def test_run_without_caption_burn_in(self):
        real = s6_assemble.find_ffmpeg()[0]
        with mock.patch.object(s6_assemble, "find_ffmpeg", return_value=(real, False)):
            code = cli.main(["--stub", "new", "How vending machines make money", "--slug", self.slug, "--run"])
        self.assertEqual(code, 0)
        d = config.OUT / self.slug
        self.assertFalse(json.loads((d / "assembly.json").read_text())["burned_captions"])
        self.assertTrue((d / "captions.srt").exists())
        self.assertIn("no burned-in captions", (d / "review" / "REVIEW.md").read_text())


if __name__ == "__main__":
    unittest.main()
