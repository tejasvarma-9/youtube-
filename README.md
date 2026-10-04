# Who Pays Who: video pipeline

Turns a topic into a finished, fact-checked video for [@WhoPaysWhoOfficial](https://www.youtube.com/@WhoPaysWhoOfficial), plus a review package. Nothing is uploaded: Tejas watches every video and approves it first. The uploader is planned in [docs/UPLOADER_PLAN.md](docs/UPLOADER_PLAN.md) and not built yet.

Everything runs on your Mac. The channel's voice, structure and visual rules live in [voice.md](voice.md).

## Every new video, in order

Do these steps in this order. Paste one line at a time in Terminal. Replace `<slug>` with the name the pipeline prints (for example `how-costco-makes-money`).

**Step 1. Get ready (2 minutes)**

```bash
cd ~/who-pays-who
source .venv/bin/activate
git pull
pip install -r requirements.txt
python -m pipeline doctor
```

`doctor` should end with "Everything is set up." A line starting with `!` is a note, not a problem. A line starting with `✗` must be fixed first.

**Step 2. Pick the business and one insight**

The insight (`--angle`) is the one idea that makes the video yours. It must be something the numbers show. "Fees are about half of operating profit" is fine. "The card is the profit" is not, because it overstates. Claims in the angle, the title and the thumbnail text must all be safe to say out loud.

Every Sunday the topic scout posts three ideas in the project, each with a checked angle and the exact command to paste (see `docs/TOPIC_SCOUT.md`). Reply with the number you want, or use your own topic.

**Step 3. Make the video with one command (mostly waiting)**

```bash
python -m pipeline auto "How <business> makes money" --angle "<your insight>"
```

This is the producer. It writes the script, fact-checks it and fixes what the fact-check flags (up to 3 rounds), then makes the voice, pictures, video, title, description and thumbnail, and finally runs the quality check. If it stops, read the line that starts with `STOPPED:`:

- Fact-check still failing after 3 rounds: read `out/<slug>/factcheck.md`, then `python -m pipeline revise <slug> --notes my-notes.txt`, then run the same `auto` command again.
- Voice quota used up, or a Claude login error: run the same `auto` command again later, or run `claude` and then `/login` first. Finished steps are skipped, nothing is lost.

Replace everything in `<...>` and the quotes with a real business and a real insight. The command refuses the README's own placeholders ("How X makes money", "...").

The step-by-step commands (`new`, then `run <slug>`) still work if you prefer them.

**Step 4. Read the quality check, then watch it**

```bash
open out/<slug>/qc.md
open out/<slug>/review/REVIEW.md
open out/<slug>/video.mp4
```

The quality check runs on its own at the end of `auto`. It listens to the finished video again and flags caption lines more than half a second off the voice, words the voice skipped or garbled, loudness, long silences, black frames and soft pictures. Claude also looks at every picture and the thumbnail (garbled text, real logos or people, wrong style) and checks the title, thumbnail text and description opening against the fact list. "Must fix" items block `approve`. To re-run it after a fix: `python -m pipeline qc <slug>`.

Then check yourself:

- Does the script open with the hook, then "This is Who Pays Who..." and end with one like-and-subscribe line? (The rule check enforces this.)
- Do the captions match the voice? Do the pictures look sharp?
- Does the thumbnail text claim only what the numbers show (the quality check also checks this)? To change it, edit `"thumbnail_text"` in `out/<slug>/metadata.json`, then run `python -m pipeline thumbnail <slug>`.

**Step 5. Approve**

```bash
python -m pipeline approve <slug>
```

`approve` refuses if the quality check failed or hasn't run on this exact video. If you've checked a flagged problem yourself and it's fine, `python -m pipeline approve <slug> --force`.

**Step 6. Upload by hand in YouTube Studio**

The upload steps are in the section "Upload to YouTube, step by step" below. Always save as Private first, check it on your phone, then make it Public.

## 1. One-time setup

```bash
git clone -b claude/video-pipeline-jnnk29 https://github.com/tejasvarma-9/youtube-.git ~/who-pays-who
cd ~/who-pays-who
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

ffmpeg and Claude Code are already on your Mac. Check that everything is ready:

```bash
python -m pipeline doctor
```

Homebrew's regular ffmpeg can't draw captions onto the video. The pipeline still works without that: you get `captions.srt` but no on-screen captions, and `doctor` shows a `!` note instead of an error. To burn captions in, install the full build (it is large, and the pipeline finds it automatically), then re-run the assembly:

```bash
brew install ffmpeg-full
python -m pipeline run <video> --from assemble
```

### Sharper images (free, optional)

The image model only makes 1K pictures, smaller than the 1920x1080 video, so frames look a little soft on a laptop. Real-ESRGAN, a free upscaler (BSD license) that suits flat 2D drawings, doubles each picture on your Mac before the video is built. Install it once, from the `who-pays-who` folder:

```bash
mkdir -p tools/realesrgan
curl -L -o tools/realesrgan.zip https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-macos.zip
unzip -o tools/realesrgan.zip -d tools/realesrgan && rm tools/realesrgan.zip
chmod +x tools/realesrgan/realesrgan-ncnn-vulkan
xattr -dr com.apple.quarantine tools/realesrgan
python -m pipeline doctor
```

`doctor` stops showing the upscaler note once it's found. The assembly then upscales each new picture (cached in `images/upscaled/`, a few minutes per video). If the upscaler ever fails, the video is still made from the original pictures and the log says so.

### Captions in sync with the voice

The voice model returns audio without timestamps, so caption timing used to be estimated and could drift in places. `pip install -r requirements.txt` now also installs faster-whisper, a small speech recognizer that runs on your Mac for free. After each voiceover it listens to the audio and times every caption line to the moment its words are spoken. The first run downloads its model (about 150 MB). If it isn't installed or doesn't match the script well, the old timing is used and the log says so.

To fix the timing of a video that's already made, without making new audio or pictures:

```bash
python -m pipeline align <video>
python -m pipeline run <video> --from assemble
```

## 2. The Google key (do this once, never paste it in chat)

One key covers images and the voiceover.

1. Go to https://aistudio.google.com/apikey. Under **Projects** choose **Create a new project** and name it `who-pays-who`, then **Create API key** and pick that project.
2. Set up billing for that project from the AI Studio **Projects** page. Prepaid credits work: they pay for both images and voice, and the balance is a hard spending cap. Turn **auto-reload** off. About 1,000 rupees covers roughly the first month.
3. Open the `.env` file and paste the key after `GOOGLE_API_KEY=`:

```bash
open -e .env
```

Prepaid Gemini credits only pay for the Gemini API, not for other Google Cloud services. That is why the voice defaults to the Gemini voice model. To use Google Cloud's Chirp 3 HD voice instead, set `TTS_PROVIDER=chirp` in `.env`, turn on Cloud Text-to-Speech, and link a regular Google Cloud billing account.

## 3. Try it with no keys

This runs every stage on placeholders (silent audio, text cards instead of images) to show that the plumbing works:

```bash
python -m pipeline --stub new "How vending machines make money" --run
open out/how-vending-machines-make-money/review/REVIEW.md
```

## 4. Pick the style frames (once)

```bash
python -m pipeline style-frames
open assets/style-refs/candidates
```

Move the 2 or 3 frames you like into `assets/style-refs/` and commit them. Every image request sends them along, so all images share one look. This costs about $0.14.

## 4b. Choose the narrator voice (once)

```bash
python -m pipeline voice-test
open out/voice-samples
```

Play the five samples. Put the winner in `.env` as `GEMINI_TTS_VOICE=Orus` (or whichever you chose). This costs about a cent. Listen for numbers read correctly: "$1,500 to $3,000" and "10% to 20%".

### More lively delivery (optional)

The first video used "a calm, confident ... voice at a steady pace", which sounded slow and flat. The default is now the upbeat, quick "energetic" style. To compare other styles in your chosen voice (about 3 cents):

```bash
python -m pipeline style-test
open out/voice-samples
```

Play `style-current`, `style-engaged` and `style-energetic`. To use one, copy its wording from `STYLE_PRESETS` in `pipeline/s3_voiceover.py` after `GEMINI_TTS_STYLE=` in `.env`. Existing videos keep their old audio; new videos use the new style.

## 5. Make a video

```bash
python -m pipeline new "How Costco makes money" --angle "About half of Costco's operating profit is membership fees, so shoppers pay for the low prices before they buy anything"
python -m pipeline run how-costco-makes-money
```

`--angle` is the one original insight that keeps the channel clear of YouTube's "inauthentic content" rule. Leave it out and the writer has to find one. Only claim what the numbers show: an angle that overstates them makes the writer overstate too, and the fact-check will fail it.

If the run stops at the fact-check, let Claude fix what it flagged. This rewrites only the flagged sentences, then fact-checks again:

```bash
python -m pipeline revise how-costco-makes-money
open out/how-costco-makes-money/changes.md
python -m pipeline run how-costco-makes-money
```

`changes.md` lists every change. The previous version is kept in `raw/v1/`. After a revision the fact-check only re-checks the facts and sentences that changed; add `--full` to `factcheck` to check everything again.

To make your own edits through the writer (or apply someone's review), put the notes in a text file and pass it:

```bash
python -m pipeline revise how-costco-makes-money --notes my-notes.txt
```

If it still fails, run `revise` once more, or fix `script.txt` and `facts.txt` yourself and run `python -m pipeline factcheck how-costco-makes-money`.

If the quality check says the voice skipped or garbled a sentence, re-record just that part (use a few words from the sentence):

```bash
python -m pipeline redo-voice how-costco-makes-money "a way to fail"
python -m pipeline voice how-costco-makes-money
python -m pipeline run how-costco-makes-money --from assemble
```

Don't use `--from voice` for this: it would also write a new shot list, and the pictures you already have would no longer match it.

To change one image, edit its `scene` in `shots.json`, delete `images/shot_NNN.png`, then:

```bash
python -m pipeline run how-costco-makes-money --from images
```

## 6. Review and approve

```bash
open out/how-costco-makes-money/review/REVIEW.md
python -m pipeline approve how-costco-makes-money
```

Approving records a fingerprint of the exact video, thumbnail and description you watched. Nothing is uploaded.

```bash
python -m pipeline status
```

## Upload to YouTube, step by step

The pipeline does not upload for you. Videos uploaded through YouTube's API from an unverified project are locked private permanently and cannot be appealed, so upload by hand until the project passes YouTube's API audit.

**Once only:** verify the channel's phone number at https://www.youtube.com/verify. Without it, custom thumbnails and links in descriptions are locked.

For each video:

1. In Terminal: `open out/<slug>`. You need `video.mp4`, `thumbnail.png`, `description.txt` and `metadata.json`.
2. Open https://studio.youtube.com, signed in as Who Pays Who. Click **Create**, then **Upload videos**, and drag in `video.mp4`.
3. **Title:** copy the `"title"` line from `metadata.json` (open it with TextEdit). Use a plain apostrophe.
4. **Description:** paste all of `description.txt`. It already has the intro, chapters and sources.
5. **Thumbnail:** click **Upload file** and pick `thumbnail.png`. Check that it shows the new text, not an old one.
6. **Audience:** choose "No, it's not made for kids".
7. Click **Show more**, set the category to **Education**. Do not upload `captions.srt` (the captions are already in the video).
8. **Altered content:** choose **No**. The narrator is a synthetic voice but doesn't pretend to be a real person, and the pictures are illustrations. If a scene ever looks like realistic footage of a real person or event, choose Yes.
9. Click **Next** through Video elements (skip subtitles, end screen and cards for now) and Checks.
10. **Visibility:** choose **Private** and click **Save**.
11. Watch it on your phone in the YouTube app: the sound, the captions, the chapters on the timeline and the thumbnail.
12. In Studio, open **Content**, click the Private lock on that row, choose **Public** (or **Schedule**), and save.
13. Open the video link in a private browser window to confirm it plays when signed out.

To replace a video that is already public, delete it in Studio and upload the new file. The old link stops working, so upload the new one as Private first and make it Public after checking.

## Rules to remember

- **Script shape:** hook first, then a two-sentence channel intro ("This is Who Pays Who..."), the video, and one like-and-subscribe sentence naming the channel as the last line. Never ask for the subscribe in the middle.
- **Three hooks:** every script has a hook at the start, in the middle (about the halfway mark, a question or twist the second half pays off) and at the end (a last question or pointer to the next video, just before the subscribe line). The rule check warns when the middle or end hook is missing.
- **Claims:** no figure without a source or an "estimate" tag. The title, thumbnail text and angle must not claim more than the numbers show. The fact-check covers the script; the quality check covers the title, thumbnail text and description opening.
- **Never** paste an API key into chat. Keys live only in `.env` on your Mac.
- **Approve** only after you have watched the exact video. If you change any file after approving, approve again.
- **Costs:** about $1 per video for voice and images. Script, fact-check, shot list and metadata use your Claude plan and cost nothing extra.

## What each stage does

| Stage | Command | Uses | Output in `out/<video>/` |
|---|---|---|---|
| Script | `script` | Claude Code with web research | `script.txt`, `facts.txt`, `sources.txt` |
| Fact-check | `factcheck` | Claude Code fetches every source; rule checks | `factcheck.md`, `sources.md` (blocks the rest if it fails) |
| Voiceover | `voice` | Gemini voice model, about 7 requests per video (a few paragraphs each) | `voiceover.wav`, `timeline.json` |
| Shots | `shots` | Claude Code writes one scene per 10+ seconds | `shots.json` |
| Images | `images` | Gemini 3.1 Flash Lite Image, batch mode | `images/` |
| Assembly | `assemble` | ffmpeg: slow zoom and pan, burned-in captions | `video.mp4`, `captions.srt` |
| Metadata | `metadata` | Claude Code; title and chapter checks | `metadata.json`, `description.txt` |
| Thumbnail | `thumbnail` | Gemini draws the subject, code adds the headline and logo | `thumbnail.png` |
| Quality check | `qc` | faster-whisper re-listens; ffmpeg measures sound and picture; Claude checks pictures and claims | `qc.md`, `qc.json` |
| Review | `review` | | `review/REVIEW.md` |

Every script opens with the hook, then a two-sentence channel intro ("This is Who Pays Who..."), and ends with one like-and-subscribe sentence naming the channel. The rule check (`script_lint.json`) fails a script that lacks either, or that asks for the subscribe anywhere else.

`run` skips stages that are already done. Each stage also runs on its own, for example `python -m pipeline voice how-costco-makes-money`.

## Costs at 3 videos a week (prices checked 2026-10-03)

| Item | Per video | Per month |
|---|---|---|
| Images, about 70 per video at $0.0168 (batch) | about $1.20 | about $15 |
| Thumbnail art, 1 image | $0.03 | $0.40 |
| Voice, about 14 minutes of audio (Gemini, $6 per million audio tokens; doubles in January 2027) | about $0.13 | about $1.70 |
| Script, fact-check, shots, metadata | $0 | $0 (your Claude plan) |

The image count is the cost lever. `MIN_SHOT_SECONDS=10` in `.env` keeps each image on screen for at least 10 seconds. Setting it to 6 matches the style spec's one image per sentence, but roughly doubles the image bill to about $30 a month.

## Settings you can put in `.env`

| Setting | Default | |
|---|---|---|
| `GEMINI_TTS_VOICE` | `Charon` | Other voices to try: Orus, Iapetus, Fenrir, Puck |
| `GEMINI_TTS_STYLE` | calm, confident, slightly wry narrator | The delivery instruction sent with every sentence |
| `TTS_PROVIDER` | `gemini` | `chirp` for Google Cloud Chirp 3 HD (needs Cloud billing) |
| `GEMINI_TTS_MODEL` | `gemini-3.8-flash-lite-tts` | The voice model. Each model has its own daily request limit; `gemini-2.5-flash-preview-tts` is a paid fallback ($10 per million audio tokens, about $0.17 a video) |
| `GEMINI_TTS_CHUNK_CHARS` | `1500` | Text per voice request. Google limits requests per day (100 on the default model), so the script goes out in chunks. Lower it (800) if the voice cuts passages short |
| `GEMINI_TTS_MIN_INTERVAL_S` | `0` | Seconds to wait between voice requests. Set to `7` if the voice step keeps hitting rate limits |
| `MIN_SHOT_SECONDS` | `10` | Lower means more images and more cost |
| `CLAUDE_MODEL` | Claude Code's default | |

## Tests

```bash
python -m unittest discover tests
```

## Credits

The title, thumbnail-text and chapter checks follow the approach of [youtube-agent-skill](https://github.com/Jakeschincariol/youtube-agent-skill) by Jake Schincariol (MIT). No code was copied from it, so its license does not apply here. Its `/yt-*` skills can still be installed on the Mac separately, for research and post-publish analysis.
