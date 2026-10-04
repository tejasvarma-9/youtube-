# Who Pays Who: video pipeline

Turns a topic into a finished, fact-checked video for [@WhoPaysWhoOfficial](https://www.youtube.com/@WhoPaysWhoOfficial), plus a review package. Nothing is uploaded: Tejas watches every video and approves it first. The uploader is planned in [docs/UPLOADER_PLAN.md](docs/UPLOADER_PLAN.md) and not built yet.

Everything runs on your Mac. The channel's voice, structure and visual rules live in [voice.md](voice.md).

## Every new video, in order

Open Terminal, then (replace `<slug>` with the name the pipeline prints, for example `how-costco-makes-money`):

```bash
cd ~/who-pays-who
source .venv/bin/activate
git pull
pip install -r requirements.txt
python -m pipeline doctor
python -m pipeline new "How <business> makes money" --angle "<one insight the numbers support>"
python -m pipeline run <slug>
open out/<slug>/review/REVIEW.md
python -m pipeline approve <slug>
```

- If `run` stops at the fact-check: `python -m pipeline revise <slug>`, then `python -m pipeline run <slug>` (section 5).
- If it stops for a quota or login problem, re-run the same `run` command later; finished stages are skipped.
- Then upload by hand in YouTube Studio: `video.mp4`, `thumbnail.png`, the title and `description.txt` from `metadata.json`. Save as Private, check it on your phone, then make it Public.

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
