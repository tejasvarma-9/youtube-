# Who Pays Who: video pipeline

Turns a topic into a finished, fact-checked video for [@WhoPaysWhoOfficial](https://www.youtube.com/@WhoPaysWhoOfficial), plus a review package. Nothing is uploaded: Tejas watches every video and approves it first. The uploader is planned in [docs/UPLOADER_PLAN.md](docs/UPLOADER_PLAN.md) and not built yet.

Everything runs on your Mac. The channel's voice, structure and visual rules live in [voice.md](voice.md).

## 1. One-time setup

```bash
git clone https://github.com/tejasvarma-9/youtube-.git ~/who-pays-who
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

## 2. The Google key (do this once, never paste it in chat)

1. Go to https://aistudio.google.com/apikey and click **Create API key**. Create it in a new project called `who-pays-who`.
2. On that project, turn on billing (the image model has no free tier), then turn on Text-to-Speech: https://console.cloud.google.com/apis/library/texttospeech.googleapis.com
3. In https://console.cloud.google.com/apis/credentials open the key. Under **API restrictions**, allow both **Generative Language API** and **Cloud Text-to-Speech API**.
4. Set a budget alert at $15: https://console.cloud.google.com/billing/budgets
5. Open the `.env` file and paste the key after `GOOGLE_API_KEY=`:

```bash
open -e .env
```

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

## 5. Make a video

```bash
python -m pipeline new "How Costco makes money" --angle "Costco's profit is mostly membership fees, so the store itself runs near break-even"
python -m pipeline run how-costco-makes-money
```

`--angle` is the one original insight that keeps the channel clear of YouTube's "inauthentic content" rule. Leave it out and the writer has to find one.

If the run stops at the fact-check:

```bash
open out/how-costco-makes-money/factcheck.md
# fix script.txt and facts.txt in that folder, then:
python -m pipeline factcheck how-costco-makes-money
python -m pipeline run how-costco-makes-money
```

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
| Voiceover | `voice` | Google Chirp 3 HD, one request per sentence | `voiceover.wav`, `timeline.json` |
| Shots | `shots` | Claude Code writes one scene per 10+ seconds | `shots.json` |
| Images | `images` | Gemini 3.1 Flash Lite Image, batch mode | `images/` |
| Assembly | `assemble` | ffmpeg: slow zoom and pan, burned-in captions | `video.mp4`, `captions.srt` |
| Metadata | `metadata` | Claude Code; title and chapter checks | `metadata.json`, `description.txt` |
| Thumbnail | `thumbnail` | Gemini draws the subject, code adds the headline and logo | `thumbnail.png` |
| Review | `review` | | `review/REVIEW.md` |

`run` skips stages that are already done. Each stage also runs on its own, for example `python -m pipeline voice how-costco-makes-money`.

## Costs at 3 videos a week (prices checked 2026-10-03)

| Item | Per video | Per month |
|---|---|---|
| Images, about 70 per video at $0.0168 (batch) | about $1.20 | about $15 |
| Thumbnail art, 1 image | $0.03 | $0.40 |
| Voice, about 14,000 characters | $0 | $0 (1M characters free, about 170,000 used) |
| Script, fact-check, shots, metadata | $0 | $0 (your Claude plan) |

The image count is the cost lever. `MIN_SHOT_SECONDS=10` in `.env` keeps each image on screen for at least 10 seconds. Setting it to 6 matches the style spec's one image per sentence, but roughly doubles the image bill to about $30 a month.

## Settings you can put in `.env`

| Setting | Default | |
|---|---|---|
| `TTS_VOICE` | `en-US-Chirp3-HD-Charon` | Other male voices to try: Orus, Iapetus, Algieba |
| `TTS_PACE` | `1.0` | 0.25 to 2.0 |
| `MIN_SHOT_SECONDS` | `10` | Lower means more images and more cost |
| `CLAUDE_MODEL` | Claude Code's default | |

## Tests

```bash
python -m unittest discover tests
```

## Credits

The title, thumbnail-text and chapter checks follow the approach of youtube-agent-skill by Jake Schincariol (MIT). No code was copied from it, so its license does not apply here. Its `/yt-*` skills can still be installed on the Mac separately, for research and post-publish analysis.
