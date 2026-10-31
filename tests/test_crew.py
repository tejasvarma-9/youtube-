"""The agent crew. Run with: python -m unittest tests.test_crew   (the end-to-end part takes a few minutes)"""

import argparse
import io
import json
import re
import shutil
import unittest
from pathlib import Path
from unittest import mock

from pipeline import cli, config, crew, llm, s11_shorts
from pipeline.common import StageError, Video, replaced_dir, retire


def _args(**kw):
    base = dict(stub=False, sync=False, no_captions=False, yes=False, count=3, replan=False, topic=None, angle=None,
                character=None, notes=None, words=config.TARGET_WORDS, slug=None, pick_top=False, from_agent=None)
    base.update(kw)
    return argparse.Namespace(**base)


class CrewEndToEnd(unittest.TestCase):
    """The whole crew on placeholders, including the topic scout (--pick-top) and the Shorts agent.
    The first review rejects Short 2, so the Shorts agent has to choose it again."""

    slug = "zz-test-crew"
    calls: list = []
    prompts: dict = {}

    @classmethod
    def setUpClass(cls):
        shutil.rmtree(config.OUT / cls.slug, ignore_errors=True)
        real = llm.ask
        state = {"rejected": False}

        def fake(prompt_text, kind, **kw):
            cls.calls.append(kind)
            cls.prompts.setdefault(kind, []).append(prompt_text)
            if kind == "shorts_review" and not state["rejected"]:
                state["rejected"] = True
                shorts = (kw.get("context") or {}).get("shorts", [])
                reviews = [{"short": n, "verdict": {2: "REJECT", 3: "FIX"}.get(n, "PASS"),
                            "problems": {2: ["It keeps the study's figure but drops the sentence stating its limits."],
                                         3: ["The hook gives away the answer."]}.get(n, [])}
                           for n in shorts]
                return "===JSON===\n" + json.dumps({"reviews": reviews}) + "\n===END==="
            return real(prompt_text, kind, **kw)

        with mock.patch.object(llm, "ask", fake):
            cls.code = cli.main(["--stub", "crew", "--pick-top", "--slug", cls.slug])
        cls.d = config.OUT / cls.slug

    @classmethod
    def tearDownClass(cls):
        from pipeline import dashboard
        shutil.rmtree(config.OUT / cls.slug, ignore_errors=True)
        dashboard.build()
        shutil.rmtree(config.OUT / "_crew" / "stub", ignore_errors=True)
        if (config.OUT / "SHORTS_RESULTS.md").exists():
            crew.write_results_tables()

    def test_1_every_agent_reported_and_nothing_was_approved(self):
        self.assertEqual(self.code, 0)
        for name in crew.AGENTS:
            r = json.loads((self.d / "agents" / f"{name}.json").read_text())
            self.assertTrue(r["passed"], name)
            self.assertTrue((self.d / "agents" / f"{name}.md").exists(), name)
        self.assertEqual(self.calls[0], "topic_scout")
        self.assertIn("overseer", self.calls)
        self.assertFalse((self.d / "approval.json").exists())
        self.assertIn("Primary sources the topic scout found", json.loads((self.d / "brief.json").read_text())["notes"])

    def test_2_verdict(self):
        verdict = json.loads((self.d / "agents" / "VERDICT.json").read_text())
        self.assertEqual(verdict["full_video"]["verdict"], "NO-GO")  # stub runs are never GO
        self.assertTrue(any("Stub run" in r for r in verdict["full_video"]["reasons"]))
        self.assertEqual(len(verdict["shorts"]), 3)
        self.assertTrue(all(s["verdict"] == "NO-GO" for s in verdict["shorts"]))
        text = (self.d / "agents" / "VERDICT.md").read_text()
        self.assertIn("No agent approved, uploaded or posted anything", text)

    def test_3_rejected_short_chosen_again_without_reusing_sentences(self):
        plan = json.loads((self.d / "shorts" / "plan.json").read_text())
        s1, s2, s3 = (set(it["sentences"]) for it in plan["shorts"])
        self.assertEqual(plan["shorts"][1]["review"]["verdict"], "PASS")
        history = plan["shorts"][1]["repicks"]
        self.assertEqual(len(history), 1)
        self.assertFalse(s2 & (s1 | s3))
        self.assertNotEqual(sorted(s2), sorted(history[0]["sentences"]))
        # The reviewer's reasons went back to the picker, with the other Shorts' sentences blocked.
        second_pick = self.prompts["shorts"][1]
        self.assertIn("drops the sentence stating its limits", second_pick)
        blocked = re.search(r"may NOT use.*?: ([\d, ]+)", second_pick).group(1)
        self.assertTrue(s1 | s3 <= {int(x) for x in blocked.split(",")})
        # The new picker rules are in the prompt.
        self.assertIn("Keep the limits with the claim", second_pick)
        self.assertIn("Say what every name is", second_pick)
        self.assertIn("Three different jobs", second_pick)
        rep = json.loads((self.d / "agents" / "shorts.json").read_text())
        self.assertEqual(rep["retries_used"], 2)  # the re-pick of Short 2, and the extra rewrite round for Short 3
        self.assertTrue(any("chosen again" in f for f in rep["fixed"]))

    def test_3b_unconfirmed_fix_gets_one_more_round(self):
        plan = json.loads((self.d / "shorts" / "plan.json").read_text())
        s3 = plan["shorts"][2]
        self.assertEqual(s3["fix_rounds"], 1)
        self.assertEqual(s3["review"]["verdict"], "PASS")
        self.assertIn("The hook gives away the answer.", s3["review_history"][-1]["problems"])
        rep = json.loads((self.d / "agents" / "shorts.json").read_text())
        self.assertTrue(any("one more rewrite round" in f for f in rep["fixed"]))

    def test_3c_dashboard_and_live_status(self):
        from pipeline import dashboard
        st = json.loads((self.d / "agents" / "status.json").read_text())
        self.assertFalse(st["running"])
        self.assertEqual(st["outcome"], "finished")
        self.assertTrue(all(st["agents"][n]["state"] == "passed" for n in crew.AGENTS[1:]), st["agents"])
        log = (self.d / "agents" / "run.log").read_text()
        self.assertIn("=== agent: shorts ===", log)
        self.assertIn("=== agent: overseer ===", log)
        page = dashboard.page_path().read_text()
        for needle in (self.slug, "Overseer", "Shorts", "NO-GO", "Live log"):
            self.assertIn(needle, page)
        self.assertEqual(cli.main(["dashboard", "--no-open"]), 0)

    def test_4_short_qc_and_upload_notes(self):
        for n in (1, 2, 3):
            sd = self.d / "shorts" / f"short_{n}"
            qc = json.loads((sd / "qc.json").read_text())
            self.assertTrue(qc["crew_qc"])
            self.assertTrue(qc["passed"], qc)
            self.assertTrue((sd / "qc_sheet.png").exists())
            up = (sd / "upload.md").read_text()
            for needle in ("only after the full video is public", "not made for kids", "Category: Education",
                           "Altered or synthetic content: No", "Related video: the full video", "Sources (from the full video)",
                           "shorts-results"):
                self.assertIn(needle, up)

    def test_5_finished_agents_skip_and_from_agent_reruns(self):
        before = json.loads((self.d / "agents" / "script_factcheck.json").read_text())["timestamp"]
        shorts_before = json.loads((self.d / "agents" / "shorts.json").read_text())["timestamp"]
        kinds = []
        real = llm.ask
        with mock.patch.object(llm, "ask", lambda t, k, **kw: (kinds.append(k), real(t, k, **kw))[1]):
            self.assertEqual(cli.main(["--stub", "crew", "--slug", self.slug]), 0)
        self.assertEqual(kinds, ["overseer"])
        self.assertEqual(json.loads((self.d / "agents" / "shorts.json").read_text())["timestamp"], shorts_before)
        kinds.clear()
        with mock.patch.object(llm, "ask", lambda t, k, **kw: (kinds.append(k), real(t, k, **kw))[1]):
            self.assertEqual(cli.main(["--stub", "crew", "--slug", self.slug, "--from-agent", "shorts"]), 0)
        self.assertIn("crew_short_qc", kinds)
        self.assertNotIn("topic_scout", kinds)
        self.assertEqual(json.loads((self.d / "agents" / "script_factcheck.json").read_text())["timestamp"], before)

    def test_6_shorts_results(self):
        args = ["shorts-results", self.slug, "1", "--views", "181", "--avg", "0:21", "--length", "0:45", "--swiped", "49",
                "--subs", "0"]
        with mock.patch("sys.stdin", io.StringIO("")):
            self.assertEqual(cli.main(args), 0)
            self.assertEqual(cli.main(["shorts-results", self.slug, "3", "--views", "70", "--avg", "0:15", "--length", "0:43",
                                       "--swiped", "66"]), 0)
        data = json.loads((self.d / "shorts" / "results.json").read_text())
        self.assertAlmostEqual(data["shorts"]["1"]["retention"], 21 / 45, places=3)
        self.assertEqual([c["short"] for c in data["comparison"]], [1, 3])
        self.assertIn(self.slug, (config.OUT / "SHORTS_RESULTS.md").read_text())
        self.assertIn("held 47%", s11_shorts.past_results_text())
        # Missing numbers with no one to ask: refused, nothing written.
        with mock.patch("sys.stdin", io.StringIO("")):
            self.assertEqual(cli.main(["shorts-results", self.slug, "2", "--views", "5"]), 1)
        self.assertNotIn("2", json.loads((self.d / "shorts" / "results.json").read_text())["shorts"])


class CrewParts(unittest.TestCase):
    """Each agent's retry logic on a tiny fake video (no ffmpeg, no Claude)."""

    slug = "zz-test-crew-parts"

    def setUp(self):
        self.d = config.OUT / self.slug
        shutil.rmtree(self.d, ignore_errors=True)
        (self.d / "images").mkdir(parents=True)
        (self.d / "brief.json").write_text(json.dumps({"topic": "Test topic", "words": 1800}))
        sents = [{"i": k, "paragraph": 0, "text": f"Sentence number {k} is here.", "start": k * 3.0, "end": k * 3.0 + 3}
                 for k in range(1, 6)]
        (self.d / "timeline.json").write_text(json.dumps({"voice": "Charon", "provider": "gemini", "duration": 18.0,
                                                          "characters": 120, "new_characters": 0, "sentences": sents}))
        shots = [{"id": k, "scene": f"Scene {k}.", "start": 0, "end": 1, "sentences": [k]} for k in range(1, 4)]
        (self.d / "shots.json").write_text(json.dumps({"shots": shots}))
        for k in range(1, 4):
            (self.d / "images" / f"shot_{k:03d}.png").write_bytes(b"png")
        self.v = Video(self.slug)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_retire_moves_and_never_deletes(self):
        p = self.d / "images" / "shot_001.png"
        dest = retire(p)
        self.assertFalse(p.exists())
        self.assertEqual(dest, replaced_dir(self.d) / "images" / "shot_001.png")
        p.write_bytes(b"second")
        dest2 = retire(p)
        self.assertEqual(dest2.name, "shot_001.2.png")
        self.assertEqual(dest.read_bytes(), b"png")
        self.assertIsNone(retire(p))

    def test_voice_agent_rerecords_flagged_lines(self):
        checks = iter([{"checked": True, "failed": True, "flagged": [2], "lines": 5, "late": [], "bad": [], "missing": [],
                        "matched": 0.9},
                       {"checked": True, "failed": False, "flagged": [], "lines": 5, "late": [], "bad": [], "missing": [],
                        "matched": 0.98}])
        rep = crew.Report("voice_captions", self.slug)
        with mock.patch.object(crew.s3_voiceover, "run") as run, \
                mock.patch.object(crew.s3_voiceover, "redo", return_value=1) as redo, \
                mock.patch.object(crew, "caption_check", side_effect=lambda *a, **k: next(checks)):
            crew.agent_voice_captions(self.v, _args(), rep, False)
        redo.assert_called_once_with(self.v, "Sentence number 2 is here.", stub=False)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(rep.passed)
        self.assertEqual(rep.retries, 1)

    def test_voice_agent_gives_up_after_two_retries(self):
        bad = {"checked": True, "failed": True, "flagged": [3], "lines": 5, "late": [], "bad": [], "missing": [], "matched": 0.9}
        rep = crew.Report("voice_captions", self.slug)
        with mock.patch.object(crew.s3_voiceover, "run"), mock.patch.object(crew.s3_voiceover, "redo", return_value=1) as redo, \
                mock.patch.object(crew, "caption_check", return_value=bad):
            crew.agent_voice_captions(self.v, _args(), rep, False)
        self.assertEqual(redo.call_count, crew.MAX_VOICE_RETRIES)
        self.assertFalse(rep.passed)

    def test_redo_voice_moves_the_recording(self):
        from pipeline import s3_voiceover
        (self.d / "script.txt").write_text("Sentence number 1 is here.\n")
        rows = [{"i": 1, "paragraph": 0, "text": "Sentence number 1 is here."}]
        import hashlib
        voice_id = f"gemini:{config.GEMINI_TTS_MODEL}:{config.GEMINI_TTS_VOICE}"
        key = hashlib.sha1(f"{voice_id}|{config.TTS_PACE}|{config.GEMINI_TTS_STYLE}|{s3_voiceover._chunk_text(rows)}".encode()).hexdigest()[:12]
        wav = self.d / "audio" / f"{key}.wav"
        wav.parent.mkdir()
        wav.write_bytes(b"RIFF")
        with mock.patch.object(config, "TTS_PROVIDER", "gemini"):
            self.assertEqual(s3_voiceover.redo(self.v, "number 1"), 1)
        self.assertFalse(wav.exists())
        self.assertTrue((replaced_dir(self.d) / "audio" / wav.name).exists())

    def test_images_agent_redraws_flagged_pictures(self):
        rounds = iter([{2: {"problem": "a number on the sign", "severity": "high"}, 3: {"problem": "slightly 3D", "severity": "low"}},
                       {}])

        def draw(v, stub=False, sync=False):
            for k in range(1, 4):
                p = self.d / "images" / f"shot_{k:03d}.png"
                if not p.exists():
                    p.write_bytes(b"new")

        rep = crew.Report("images", self.slug)
        with mock.patch.object(crew.s5_images, "run", side_effect=draw), \
                mock.patch.object(crew, "picture_check", side_effect=lambda *a, **k: next(rounds)):
            crew.agent_images(self.v, _args(), rep, False)
        self.assertTrue(rep.passed, rep.could_not_fix)
        self.assertEqual((self.d / "images" / "shot_002.png").read_bytes(), b"new")
        self.assertEqual((replaced_dir(self.d) / "images" / "shot_002.png").read_bytes(), b"png")
        scene = json.loads((self.d / "shots.json").read_text())["shots"][1]["scene"]
        self.assertIn("a number on the sign", scene)
        self.assertTrue((replaced_dir(self.d) / "shots.json").exists())
        self.assertTrue(any("Shot 3" in w for w in rep.warnings))

    def test_images_agent_stops_when_pictures_stay_broken(self):
        def draw(v, stub=False, sync=False):
            p = self.d / "images" / "shot_001.png"
            if not p.exists():
                p.write_bytes(b"new")

        rep = crew.Report("images", self.slug)
        with mock.patch.object(crew.s5_images, "run", side_effect=draw), \
                mock.patch.object(crew, "picture_check", return_value={1: {"problem": "garbled text", "severity": "high"}}):
            crew.agent_images(self.v, _args(), rep, False)
        self.assertFalse(rep.passed)
        self.assertEqual(rep.retries, crew.MAX_IMAGE_RETRIES)
        self.assertEqual(len(list(replaced_dir(self.d).joinpath("images").glob("shot_001*.png"))), crew.MAX_IMAGE_RETRIES)

    def test_packaging_applies_claim_fix_once(self):
        (self.d / "metadata.json").write_text(json.dumps({"title": "T", "thumbnail_text": "AD REVENUE", "description_intro": "D"}))
        (self.d / "video.mp4").write_bytes(b"mp4")
        bad = {"passed": False, "failures": ["Thumbnail text overstated"], "warnings": [], "checks": {"claude_review": {"claims": [
            {"where": "Thumbnail text", "text": "AD REVENUE", "verdict": "OVERSTATED", "note": "fees, not ads", "fix": "THEY SELL LEADS"}]}}}
        good = {"passed": True, "failures": [], "warnings": [], "checks": {}}
        results = iter([bad, good])
        rep = crew.Report("packaging", self.slug)
        with mock.patch.object(crew.s6_assemble, "run"), mock.patch.object(crew.s7_metadata, "run"), \
                mock.patch.object(crew.s8_thumbnail, "run") as thumb, mock.patch.object(crew.s9_review, "run"), \
                mock.patch.object(crew.s10_qc, "run", side_effect=lambda *a, **k: next(results)):
            crew.agent_packaging(self.v, _args(stub=True), rep, False)
        self.assertTrue(rep.passed)
        self.assertEqual(json.loads((self.d / "metadata.json").read_text())["thumbnail_text"], "THEY SELL LEADS")
        self.assertEqual(json.loads((replaced_dir(self.d) / "metadata.json").read_text())["thumbnail_text"], "AD REVENUE")
        self.assertEqual(thumb.call_count, 2)

    def test_packaging_fixes_the_small_thumbnail_label_named_in_prose(self):
        # What a real run produced: the check calls it "Thumbnail text (small label)" and answers in a sentence.
        (self.d / "metadata.json").write_text(json.dumps({"title": "T", "thumbnail_text": "$2.6B REVENUE",
                                                          "thumbnail_before": "FREE SCORE", "thumbnail_after": "AD REVENUE",
                                                          "description_intro": "D"}))
        (self.d / "video.mp4").write_bytes(b"mp4")
        bad = {"passed": False, "failures": ["x"], "warnings": [], "checks": {"claude_review": {"claims": [
            {"where": "Thumbnail text (small label)", "text": "AD REVENUE", "verdict": "OVERSTATED", "note": "n",
             "fix": "Replace 'AD REVENUE' with 'WHO PAYS?' or 'PAID BY ADVERTISERS?'"}]}}}
        results = iter([bad, {"passed": True, "failures": [], "warnings": [], "checks": {}}])
        rep = crew.Report("packaging", self.slug)
        with mock.patch.object(crew.s6_assemble, "run"), mock.patch.object(crew.s7_metadata, "run"), \
                mock.patch.object(crew.s8_thumbnail, "run"), mock.patch.object(crew.s9_review, "run"), \
                mock.patch.object(crew.s10_qc, "run", side_effect=lambda *a, **k: next(results)):
            crew.agent_packaging(self.v, _args(stub=True), rep, False)
        meta = json.loads((self.d / "metadata.json").read_text())
        self.assertEqual((meta["thumbnail_after"], meta["thumbnail_text"]), ("WHO PAYS?", "$2.6B REVENUE"))
        self.assertTrue(rep.passed)

    def test_usable_fix_refuses_prose(self):
        self.assertEqual(crew.usable_fix("Cut it.", "thumbnail_after"), "")
        self.assertEqual(crew.usable_fix("Remove the word revenue and rethink the whole label", "thumbnail_after"), "")
        self.assertEqual(crew.usable_fix("THEY SELL LEADS", "thumbnail_text"), "THEY SELL LEADS")

    def test_overseer_reads_files_bigger_than_its_prompt_limit(self):
        # A real fact-check is far bigger than the 20,000 characters the overseer puts in its prompt.
        facts = [{"id": f"F{k}", "verdict": "SUPPORTED", "note": "x" * 200} for k in range(200)]
        (self.d / "factcheck.json").write_text(json.dumps({"passed": True, "facts": facts}))
        (self.d / "qc.json").write_text(json.dumps({"passed": True}))
        big = json.loads((self.d / "timeline.json").read_text())
        big["pad"] = "y" * 60000
        (self.d / "timeline.json").write_text(json.dumps(big))
        sent = []
        real = llm.ask
        rep = crew.Report("overseer", self.slug)
        with mock.patch.object(llm, "ask", lambda t, k, **kw: (sent.append(t), real(t, k, **kw))[1]):
            crew.agent_overseer(self.v, _args(stub=True), rep, False)
        self.assertEqual(len(sent), 1)
        self.assertTrue((self.d / "agents" / "VERDICT.md").exists())

    def test_an_unexpected_error_in_an_agent_leaves_a_report_and_no_go(self):
        (self.d / "factcheck.json").write_text(json.dumps({"passed": True}))
        with mock.patch.object(crew, "start", return_value=self.v), \
                mock.patch.object(crew, "confirm_cost"), \
                mock.patch.dict(crew.AGENT_FUNCS, {"script_factcheck": mock.Mock(side_effect=KeyError("boom"))}):
            with self.assertRaises(StageError):
                crew.cmd_crew(_args(stub=True))
        r = json.loads((self.d / "agents" / "script_factcheck.json").read_text())
        self.assertFalse(r["passed"])
        self.assertIn("unexpected error", r["could_not_fix"][0])
        self.assertEqual(json.loads((self.d / "agents" / "VERDICT.json").read_text())["full_video"]["verdict"], "NO-GO")

    def test_internal_notes_in_the_description_stop_packaging(self):
        (self.d / "metadata.json").write_text(json.dumps({"title": "T", "thumbnail_text": "X", "description_intro": "D"}))
        (self.d / "video.mp4").write_bytes(b"mp4")
        (self.d / "sources.md").write_text("S1. SEC: https://sec.gov/x\nS2. Intuit results. The figure was reported from "
                                           "search-result text, so the fact-checker should confirm it: https://x\n")
        (self.d / "description.txt").write_text("Intro.\n\nSources:\n" + (self.d / "sources.md").read_text())
        ok = {"passed": True, "failures": [], "warnings": [], "checks": {}}
        rep = crew.Report("packaging", self.slug)
        with mock.patch.object(crew.s6_assemble, "run"), mock.patch.object(crew.s7_metadata, "run"), \
                mock.patch.object(crew.s8_thumbnail, "run"), mock.patch.object(crew.s9_review, "run"), \
                mock.patch.object(crew.s10_qc, "run", return_value=ok):
            crew.agent_packaging(self.v, _args(stub=False), rep, False)
        self.assertFalse(rep.passed)
        self.assertIn("fact-checker should confirm", rep.could_not_fix[0])
        self.assertIn("sources.txt", rep.could_not_fix[0])
        (self.d / "sources.md").write_text("S1. SEC filing, how we verify claims is in the video: https://sec.gov/x\n")
        (self.d / "description.txt").write_text("Intro.\n")
        self.assertEqual(crew.internal_notes(self.v), [])

    def test_overseer_gets_real_commands(self):
        (self.d / "factcheck.json").write_text(json.dumps({"passed": True}))
        (self.d / "qc.json").write_text(json.dumps({"passed": True}))
        sent = []
        real = llm.ask
        with mock.patch.object(llm, "ask", lambda t, k, **kw: (sent.append(t), real(t, k, **kw))[1]):
            crew.agent_overseer(self.v, _args(stub=True), crew.Report("overseer", self.slug), False)
        self.assertIn(f"python -m pipeline crew --slug {self.slug} --from-agent packaging", sent[0])
        self.assertNotIn("{{COMMANDS}}", sent[0])
        self.assertNotIn("approve", crew.fix_commands(self.slug))

    def test_spoken_question_captions_use_heard_word_times(self):
        from pipeline import align
        wav = self.d / "question_trimmed.wav"
        wav.write_bytes(b"RIFF")
        heard = [("How", 0.30, 0.45), ("does", 0.45, 0.60), ("it", 0.60, 0.70), ("pay?", 0.70, 1.10)]
        with mock.patch.object(align, "available", return_value=True), mock.patch.object(align, "transcribe", return_value=heard):
            times = s11_shorts.question_word_times(wav, "How does it pay?", 1.4)
        self.assertEqual(times[0], [0.30, 0.45])
        srt = self.d / "q.srt"
        s11_shorts.write_srt([{"text": "How does it pay?", "words": times, "cut": 0.0, "offset": 0.0, "length": 1.4}], srt)
        self.assertIn("00:00:00,300 -->", srt.read_text())  # not 00:00:00,000, where even spreading started it
        self.assertIsNone(s11_shorts.question_word_times(wav, "How does it pay?", 1.4, stub=True))

    def test_dashboard_shows_running_and_interrupted_runs(self):
        from pipeline import dashboard
        plan = {n: "waiting" for n in crew.AGENTS[1:]}
        plan["script_factcheck"] = "skipped"
        dashboard.run_started(self.v, plan)
        dashboard.agent_started(self.v, "voice_captions")
        page = dashboard.render(dashboard.collect())
        self.assertIn("data-live='1'", page)
        self.assertIn("Voice and captions", page)
        self.assertIn("Already done", page)
        # A run that died (no update for a long time, its process gone) shows as interrupted, not running forever.
        st = dashboard.read_status(self.v)
        st.update(updated="2026-01-01T00:00:00Z", pid=999999)
        (self.d / "agents" / "status.json").write_text(json.dumps(st))
        vid = next(v for v in dashboard.collect() if v["slug"] == self.slug)
        self.assertEqual(vid["status"]["outcome"], "interrupted")
        self.assertEqual(dashboard._state("voice_captions", vid), "stopped")

    def test_cost_estimate_and_confirmation(self):
        rows = crew.estimate(self.v, _args())
        self.assertTrue(any("Pictures" in r[0] for r in rows))
        self.assertTrue(any(r[2] > 0 for r in rows))  # retry ceilings are shown
        with mock.patch("sys.stdin", io.StringIO("")):
            with self.assertRaises(StageError):
                crew.confirm_cost([("Voice", 0.5, 0.1)], _args())
            crew.confirm_cost([("Voice", 0.5, 0.1)], _args(yes=True))  # --yes goes ahead
        self.assertEqual(crew.estimate(self.v, _args(stub=True)), [])

    def test_overseer_cannot_say_go_against_the_rules(self):
        (self.d / "factcheck.json").write_text(json.dumps({"passed": True}))
        (self.d / "qc.json").write_text(json.dumps({"passed": True}))
        plan = {"shorts": [{"title": "A"}, {"title": "B"}]}
        data = {"full_video": {"verdict": "GO", "reasons": ["fine"]},
                "shorts": [{"short": 1, "verdict": "GO"}, {"short": 2, "verdict": "GO"}],
                "rechecked_claims": [{"claim": "x", "result": "CONFIRMED"}]}
        v = crew.enforce(self.v, data, plan, {"1": {"status": "READY"}, "2": {"status": "QC FAILED"}}, stub=False)
        self.assertEqual(v["full_video"]["verdict"], "GO")
        self.assertEqual([s["verdict"] for s in v["shorts"]], ["GO", "NO-GO"])
        data["rechecked_claims"].append({"claim": "y", "result": "OVERSTATED", "source_url": "https://example.com"})
        v = crew.enforce(self.v, data, plan, {"1": {"status": "READY"}, "2": {"status": "READY"}}, stub=False)
        self.assertEqual(v["full_video"]["verdict"], "NO-GO")
        self.assertTrue(all(s["verdict"] == "NO-GO" for s in v["shorts"]))

    def test_topic_scout_needs_three_primary_sources_and_skips_made_topics(self):
        src = [{"url": f"https://sec.gov/{k}"} for k in range(3)]
        data = {"topics": [{"topic": "How Costco makes money", "angle": "a", "sources": src},
                           {"topic": "How airports make money", "angle": "a", "sources": src[:2]},
                           {"topic": "How X makes money", "angle": "a", "sources": src},
                           {"topic": "How toll roads make money", "angle": "a", "sources": src}]}
        good, problems = crew.check_topics(data, {"how-costco-makes-money"})
        self.assertEqual([t["topic"] for t in good], ["How toll roads make money"])
        self.assertEqual(len(problems), 3)


class ShortsRules(unittest.TestCase):
    def test_question_hook_and_first_line_must_differ(self):
        text = "Why would anyone pay $1.95 billion for a hotel they don't run? Because the brand runs it."
        probs = s11_shorts.written_problems("S", text, "Pay $1.95 billion for a hotel?", "T", "D", "Who runs the hotel?")
        self.assertTrue(any("say the same thing" in p for p in probs))
        ok = s11_shorts.written_problems("S", text, "They own it. They can't run it.", "T", "D", "Who runs a hotel you own?")
        self.assertFalse([p for p in ok if "say the same thing" in p])

    def test_validate_plan_excludes_other_shorts(self):
        rows = [{"i": k + 1, "paragraph": 0 if k < 6 else 1 + (k - 6), "text": t, "start": k * 6.0, "end": k * 6.0 + 5.5}
                for k, t in enumerate(["How much does it cost?", "Robinhood earned $776 million in 2026.", "Who paid?", "Welcome to Who Pays Who.",
                                       "The answer is three places.", "Fees.", "End.", "Like and subscribe."])]
        plan = {"shorts": [{"hook_text": "Free, so who paid?", "title": "Who paid $776 million?", "sentences": [1, 2],
                            "description_intro": "A look at the money.", "tags": ["a"]}]}
        _, problems = s11_shorts.validate_plan(plan, rows, 1, stub=True, exclude={2})
        self.assertTrue(any("another Short already uses" in p for p in problems))


class CrewHardRules(unittest.TestCase):
    def test_crew_never_approves_uploads_or_deletes(self):
        src = (config.ROOT / "pipeline" / "crew.py").read_text()
        code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
        self.assertNotIn("approve(", code)
        self.assertNotIn("s9_review.approve", code)
        self.assertNotRegex(code, r"\.unlink\(|shutil\.rmtree|os\.remove|rmdir\(")
        self.assertNotRegex(code, r"(?i)youtube\.upload|videos\(\)\.insert|vidiq_")


class TrendsTests(unittest.TestCase):
    """The trends agent on made-up YouTube data, in a copy of the repo so applying a proposal never edits real prompts."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(config.ROOT / "prompts", self.tmp / "prompts")
        shutil.copy2(config.ROOT / "voice.md", self.tmp / "voice.md")
        (self.tmp / "out").mkdir()
        self.patches = [mock.patch.object(config, "ROOT", self.tmp), mock.patch.object(config, "OUT", self.tmp / "out"),
                        mock.patch.object(config, "PROMPTS", self.tmp / "prompts")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_each_video_is_measured_against_its_own_channel(self):
        from pipeline import stubs, trends
        g = trends.measure(stubs.youtube_data())
        self.assertEqual({o["id"] for o in g["videos"]["outliers"]}, {"Av10", "Bv10"})
        self.assertEqual(g["videos"]["outliers"][0]["ratio"], 7.5)
        self.assertIn("Av11", {f["id"] for f in g["videos"]["flops"]})
        self.assertEqual({o["id"] for o in g["shorts"]["outliers"]}, {"Av30", "Bv30"})

    def test_report_proposals_and_apply(self):
        from pipeline import trends
        self.assertEqual(cli.main(["--stub", "trends"]), 0)
        r = trends.latest(stub=True)
        self.assertEqual(len(r["proposals"]), 1)
        self.assertTrue(any("not a file proposals may edit" in d for d in r["dropped"]))
        self.assertIn("Stub note", trends.writer_notes(stub=True))
        md = next((self.tmp / "out" / "_crew" / "stub").glob("trends_*.md")).read_text()
        self.assertIn("Proposed changes (you decide)", md)
        before = (self.tmp / "prompts" / "shorts.md").read_text()
        self.assertEqual(before, (config.ROOT / "prompts" / "shorts.md").read_text())  # the report alone changed nothing
        self.assertEqual(cli.main(["--stub", "trends", "--apply", "1"]), 0)
        after = (self.tmp / "prompts" / "shorts.md").read_text()
        self.assertIn("3 to 6 search phrases people really type.", after)
        kept = replaced_dir(self.tmp / "out" / "_crew" / "stub") / "prompts" / "shorts.md"
        self.assertEqual(kept.read_text(), before)
        self.assertEqual(trends.latest(stub=True)["applied"], [1])
        # Applying it again finds nothing to replace and changes nothing.
        self.assertEqual(cli.main(["--stub", "trends", "--apply", "1"]), 1)
        self.assertEqual((self.tmp / "prompts" / "shorts.md").read_text(), after)
        self.assertEqual(cli.main(["--stub", "trends", "--apply", "5"]), 1)

    def test_old_notes_are_not_used(self):
        from pipeline import trends
        self.assertEqual(cli.main(["--stub", "trends"]), 0)
        p = self.tmp / "out" / "_crew" / "stub" / "trends_latest.json"
        r = json.loads(p.read_text())
        r["timestamp"] = "2026-01-01T00:00:00Z"
        p.write_text(json.dumps(r))
        self.assertIn("too old", trends.writer_notes(stub=True))

    def test_proposals_must_match_the_file_and_say_how_we_stay_distinct(self):
        from pipeline import trends
        ok = {"file": "prompts/shorts.md", "before": "- tags: 3 to 6 search phrases.", "after": "x", "keeps_us_distinct": "y"}
        good, dropped = trends.check_proposals([ok, {**ok, "before": "not in the file"}, {**ok, "keeps_us_distinct": ""},
                                                {**ok, "template_risk": "copies Channel X's thumbnail"}])
        self.assertEqual(len(good), 1)
        self.assertEqual(len(dropped), 3)

    def test_api_errors_never_show_the_key(self):
        from pipeline import trends
        res = mock.Mock(status_code=403, text="API key SECRET123: YouTube Data API v3 has not been used in project 1")
        with mock.patch.object(trends.requests, "get", return_value=res):
            with self.assertRaises(StageError) as e:
                trends.YouTube("SECRET123").get("search", q="x")
        self.assertNotIn("SECRET123", str(e.exception))
        self.assertIn("YouTube Data API v3", str(e.exception))

    def test_a_gemini_only_key_gets_clear_instructions(self):
        from pipeline import trends
        res = mock.Mock(status_code=401, text="API keys are not supported by this API. Expected OAuth2 access token")
        with mock.patch.object(trends.requests, "get", return_value=res):
            with self.assertRaises(StageError) as e:
                trends.YouTube("SECRET123").get("search", q="x")
        self.assertIn("client_secret.json", str(e.exception))

    def test_aq_key_without_sign_in_explains_the_setup(self):
        from pipeline import trends
        with mock.patch.object(trends, "api_key", return_value="AQ.Abcdef"):
            with self.assertRaises(StageError) as e:
                trends.client_for_run()
        self.assertIn("Desktop app", str(e.exception))

    def _client_secret(self):
        (self.tmp / "client_secret.json").write_text(json.dumps({"installed": {
            "client_id": "cid", "client_secret": "csec", "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token"}}))

    def test_browser_sign_in_saves_a_private_token(self):
        import threading
        import urllib.parse
        import urllib.request
        from pipeline import trends
        self._client_secret()

        def browser(url):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            self.assertEqual(q["scope"], [trends.SCOPE])
            self.assertEqual(q["code_challenge_method"], ["S256"])
            back = q["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "the-code", "state": q["state"][0]})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            threading.Thread(target=lambda: opener.open(back, timeout=10).read(), daemon=True).start()
            return True

        exchange = mock.Mock(status_code=200)
        exchange.json.return_value = {"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600}
        with mock.patch("webbrowser.open", side_effect=browser), \
                mock.patch.object(trends.requests, "post", return_value=exchange) as post:
            self.assertEqual(cli.main(["trends", "--login"]), 0)
        sent = post.call_args.kwargs["data"]
        self.assertEqual((sent["code"], sent["grant_type"]), ("the-code", "authorization_code"))
        self.assertTrue(sent["code_verifier"])
        tok = self.tmp / "token_youtube.json"
        self.assertEqual(json.loads(tok.read_text())["refresh_token"], "rt1")
        self.assertEqual(tok.stat().st_mode & 0o777, 0o600)

    def test_saved_sign_in_is_reused_and_refreshed(self):
        import time
        from pipeline import trends
        self._client_secret()
        tok = self.tmp / "token_youtube.json"
        tok.write_text(json.dumps({"access_token": "fresh", "refresh_token": "rt", "expires_at": time.time() + 3000}))
        with mock.patch.object(trends.requests, "post", side_effect=AssertionError("no network needed")):
            self.assertEqual(trends.oauth_token(), "fresh")
        tok.write_text(json.dumps({"access_token": "old", "refresh_token": "rt", "expires_at": 0}))
        refreshed = mock.Mock(status_code=200)
        refreshed.json.return_value = {"access_token": "new", "expires_in": 3600}
        with mock.patch.object(trends.requests, "post", return_value=refreshed):
            self.assertEqual(trends.oauth_token(), "new")
        self.assertEqual(json.loads(tok.read_text())["refresh_token"], "rt")
        # Expired and refused, with no one to click: a clear instruction, not a hang.
        tok.write_text(json.dumps({"access_token": "old", "refresh_token": "dead", "expires_at": 0}))
        with mock.patch.object(trends.requests, "post", return_value=mock.Mock(status_code=400, text="invalid_grant")), \
                mock.patch("sys.stdin", io.StringIO("")):
            with self.assertRaises(StageError) as e:
                trends.oauth_token()
        self.assertIn("--login", str(e.exception))

    def test_signed_in_requests_send_a_token_not_a_key(self):
        from pipeline import trends
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"items": []}
        with mock.patch.object(trends.requests, "get", return_value=ok) as get:
            trends.YouTube(token="tok123").get("search", q="x")
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer tok123"})
        self.assertNotIn("key", get.call_args.kwargs["params"])

    def test_every_trends_placeholder_is_filled(self):
        for name, code in (("metadata.md", "s7_metadata.py"), ("shorts.md", "s11_shorts.py"), ("topic_scout.md", "crew.py")):
            self.assertIn("{{TRENDS}}", (config.PROMPTS / name).read_text())
            self.assertIn("TRENDS=trends.writer_notes(", (Path(__file__).parent.parent / "pipeline" / code).read_text())


if __name__ == "__main__":
    unittest.main()


class AutopilotTests(unittest.TestCase):
    """The autopilot: safe fixes only, a spending limit, never approve; and the local Start server."""

    slug = "zz-test-autopilot"

    def setUp(self):
        self.d = config.OUT / self.slug
        shutil.rmtree(self.d, ignore_errors=True)
        (self.d / "images").mkdir(parents=True)
        (self.d / "agents").mkdir()
        (self.d / "brief.json").write_text(json.dumps({"topic": "Test topic", "words": 1800}))
        style = "\n\nStyle: simple 2D hand-drawn. No text of any kind."
        shots = [{"id": k, "text": f"Narration {k}.", "scene": f"Scene {k}. (Redraw note: old problem. Avoid it.)",
                  "prompt": f"Scene {k}.{style}", "start": 0, "end": 1, "sentences": [k]} for k in range(1, 4)]
        (self.d / "shots.json").write_text(json.dumps({"shots": shots}))
        for k in range(1, 4):
            (self.d / "images" / f"shot_{k:03d}.png").write_bytes(b"png")
        self.v = Video(self.slug)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _state(self):
        from pipeline import autopilot
        return autopilot.State(self.v, 2.0, 4)

    def _images_failed(self, ids=(2,)):
        rep = crew.Report("images", self.slug)
        rep.fail("These pictures still have serious problems")
        rep.details["findings"] = {str(i): {"severity": "high", "problem": "the queue is cut off"} for i in ids}
        rep.write(self.v)
        (self.d / "agents" / "status.json").write_text(json.dumps({"agents": {"images": {"state": "stopped"}}}))

    def test_bad_picture_gets_a_new_scene_and_is_set_aside_not_deleted(self):
        from pipeline import autopilot
        self._images_failed()
        st = self._state()
        done, first, replan = autopilot.plan(self.v, None, st, True)
        self.assertEqual(first, "images")
        self.assertEqual(len(done), 1)
        shots = json.loads((self.d / "shots.json").read_text())["shots"]
        s2 = next(s for s in shots if s["id"] == 2)
        self.assertNotIn("Redraw note", s2["scene"])
        self.assertTrue(s2["prompt"].startswith(s2["scene"]) and "Style:" in s2["prompt"])
        self.assertFalse((self.d / "images" / "shot_002.png").exists())
        self.assertTrue((replaced_dir(self.d) / "images" / "shot_002.png").exists())
        self.assertTrue((replaced_dir(self.d) / "shots.json").exists())
        self.assertTrue((self.d / "images" / "shot_001.png").exists())

    def test_a_picture_is_rewritten_at_most_twice(self):
        from pipeline import autopilot
        self._images_failed()
        st = self._state()
        for _ in range(2):
            self.assertTrue(autopilot.plan(self.v, None, st, True)[0])
        done, first, _ = autopilot.plan(self.v, None, st, True)
        self.assertEqual((done, first), ([], None))

    def test_an_old_report_does_not_trigger_a_fix(self):
        from pipeline import autopilot
        self._images_failed()
        (self.d / "agents" / "status.json").write_text(json.dumps({"agents": {"images": {"state": "skipped"}}}))
        self.assertEqual(autopilot.plan(self.v, None, self._state(), True)[0], [])

    def test_pipeline_note_is_cut_from_sources_but_ids_and_urls_stay(self):
        from pipeline import autopilot
        (self.d / "sources.txt").write_text("S1 | Example Publisher, Real Title | https://example.com/a\n"
                                            "S2 | Other Publisher, Other Title | https://example.com/b | the fact-checker should confirm this\n")
        changed, stuck = autopilot.strip_source_notes(self.v, True)
        self.assertEqual((changed, stuck), (["S2"], []))
        text = (self.d / "sources.txt").read_text()
        self.assertIn("S2 | Other Publisher, Other Title | https://example.com/b\n", text)
        self.assertNotIn("fact-checker", text)
        self.assertTrue((replaced_dir(self.d) / "sources.txt").exists())

    def test_a_note_it_cannot_cut_safely_is_left_for_you(self):
        from pipeline import autopilot
        line = "S2 | Publisher, Title (fact-checker should confirm) | https://example.com/b"
        (self.d / "sources.txt").write_text(line + "\n")
        changed, stuck = autopilot.strip_source_notes(self.v, True)  # stub Claude hands the line back unchanged
        self.assertEqual(changed, [])
        self.assertEqual(len(stuck), 1)
        self.assertEqual((self.d / "sources.txt").read_text().strip(), line)

    def test_it_stops_before_a_run_that_would_pass_the_spending_limit(self):
        from pipeline import autopilot
        a = argparse.Namespace(topic=None, slug=self.slug, stub=False, cap=0.5, max_cycles=4)
        with mock.patch.object(crew, "start", return_value=self.v), \
             mock.patch.object(crew, "estimate", return_value=[("pictures", 1.0, 0.0)]), \
             mock.patch.object(crew, "cmd_crew") as ran:
            st = autopilot.run(a)
        self.assertEqual(st.status, "cap")
        ran.assert_not_called()

    def test_all_go_stops_as_ready_and_never_approves(self):
        from pipeline import autopilot, s9_review
        verdict = {"full_video": {"verdict": "GO", "reasons": [], "fixes": []},
                   "shorts": [{"short": 1, "verdict": "GO", "reasons": [], "fixes": []}]}
        a = argparse.Namespace(topic=None, slug=self.slug, stub=True, cap=2.0, max_cycles=4)
        with mock.patch.object(crew, "start", return_value=self.v), mock.patch.object(crew, "cmd_crew") as ran, \
             mock.patch.object(s9_review, "approve") as approve:
            (self.d / "agents" / "VERDICT.json").write_text(json.dumps(verdict))
            st = autopilot.run(a)
        self.assertEqual(st.status, "ready")
        ran.assert_called_once()
        approve.assert_not_called()
        self.assertIn("Autopilot", (self.d / "agents" / "AUTOPILOT.md").read_text())

    def test_stub_run_ends_needs_you_without_looping(self):
        from pipeline import autopilot
        a = argparse.Namespace(topic="Autopilot stub topic", slug="zz-test-autopilot-stub", stub=True, cap=2.0, max_cycles=4,
                               count=3)
        d = config.OUT / "zz-test-autopilot-stub"
        shutil.rmtree(d, ignore_errors=True)
        try:
            with mock.patch("sys.stdin.isatty", return_value=False):
                st = autopilot.run(a)
            self.assertEqual(st.status, "needs_you")  # a stub video is always NO-GO, and nothing safe applies
            self.assertEqual(st.cycle, 1)
            self.assertTrue(st.needs_you)
            self.assertEqual(json.loads((d / "agents" / "autopilot.json").read_text())["status"], "needs_you")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_local_server_needs_the_token_and_the_right_host(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        from pipeline import dashboard, serve
        box = [0]
        server = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler("tok123", box))
        box[0] = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        host = f"127.0.0.1:{box[0]}"

        def call(method, path, body=None, headers=None):
            c = http.client.HTTPConnection("127.0.0.1", box[0])
            c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
            r = c.getresponse()
            return r.status, r.read().decode()
        try:
            code, page = call("GET", "/")
            self.assertEqual(code, 200)
            self.assertIn("Start a video", page)
            self.assertIn("tok123", page)
            self.assertEqual(call("POST", "/start", {"topic": "x"})[0], 403)                      # no token
            self.assertEqual(call("POST", "/start", {"topic": "x"}, {"X-Token": "wrong"})[0], 403)
            self.assertEqual(call("POST", "/start", {"topic": "x"}, {"X-Token": "tok123", "Host": "evil.example"})[0], 403)
            self.assertEqual(call("POST", "/start", {"slug": "no-such-video"}, {"X-Token": "tok123"})[0], 400)
            with mock.patch.object(dashboard, "autopilot_running", return_value=True):
                self.assertEqual(call("POST", "/start", {"topic": "x"}, {"X-Token": "tok123"})[0], 409)
            with mock.patch.object(dashboard, "autopilot_running", return_value=False), \
                 mock.patch("pipeline.serve.subprocess.Popen") as popen:
                code, _ = call("POST", "/start", {"topic": "--yes; rm -rf /"}, {"X-Token": "tok123"})
            self.assertEqual(code, 200)
            argv = popen.call_args[0][0]
            self.assertIn("--topic=--yes; rm -rf /", argv)       # one argument, never a flag, never a shell
            self.assertNotIn("approve", " ".join(argv))
        finally:
            server.shutdown()

    def test_dashboard_shows_the_autopilot_and_stays_live_while_it_runs(self):
        from pipeline import dashboard
        ap = {"slug": self.slug, "status": "needs_you", "pid": 1, "cycle": 2, "max_cycles": 4, "spent": 0.4, "cap": 2.0,
              "updated": "2026-10-31T00:00:00Z", "actions": [{"cycle": 1, "text": "wrote a new scene for shot 2"}],
              "needs_you": ["Full video: fix the claim"], "summary": "Stopped: needs you."}
        (self.d / "agents" / "autopilot.json").write_text(json.dumps(ap))
        page = dashboard.render(dashboard.collect(), token="t")
        for needle in ("Needs you", "wrote a new scene for shot 2", "Run it again", "$0.40 of $2.00"):
            self.assertIn(needle, page)
        self.assertNotIn("Run it again", dashboard.render(dashboard.collect()))   # a plain file has no Start button
        ap.update(status="running", pid=__import__("os").getpid())
        (self.d / "agents" / "autopilot.json").write_text(json.dumps(ap))
        self.assertIn("data-live='1'", dashboard.render(dashboard.collect()))
