# Uploader plan (not built yet: blocked on YouTube's API audit)

The last stage: upload an approved video to YouTube as **private**, with a scheduled publish time. It is built only after a few videos have gone through the review package by hand.

## Rules it will enforce

1. It refuses any video without `approval.json`, and any video whose files changed after approval (`s9_review.is_approved`).
2. It always uploads with `privacyStatus: private`. With a planned time, it also sets `publishAt` so YouTube publishes it then. Without one, the video stays private until Tejas publishes it in Studio.
3. It never uploads stub runs (approval already refuses them).
4. It writes `upload.json` with the video ID, so a re-run never uploads twice.

## What Tejas sets up first (about 15 minutes, once)

1. In the same Google Cloud project, turn on **YouTube Data API v3**.
2. Configure the OAuth consent screen (External, Testing), and add his own Google account as a test user.
3. Create an OAuth client of type **Desktop app**, download it as `client_secret.json` into the repo folder. It is gitignored.
4. The first run opens a browser to sign in to the Who Pays Who channel's Google account. The refresh token is saved to `token.json`, which is also gitignored.

## Limits to know

- **Blocker: unverified projects can't publish at all.** Google's docs: "All videos uploaded via the videos.insert endpoint from unverified API projects created after 28 July 2020 will be restricted to private viewing mode" (https://developers.google.com/youtube/v3/docs/videos/insert). YouTube's help page says these videos are *locked* private, the lock can't be appealed, and the fix is to re-upload through the website or a verified service (https://support.google.com/youtube/answer/7300965). So until the project passes YouTube's API audit, the uploader can only make videos nobody can ever see. Checked 2026-10-04. Don't build it before the audit is approved; upload by hand in Studio until then.
- **Quota:** 10,000 units a day by default. An upload costs about 1,600 units, plus 50 for the thumbnail and 400 for the captions track, so about 4 videos a day. That's plenty for 3 to 7 a week.
- Thumbnails need a verified channel (phone verification in Studio). Without it, `thumbnails.set` fails.
- Captions go up as a track from `captions.srt`, even though they are also burned in, so YouTube can index the text.
- Tick "altered or synthetic content" only if a video ever uses realistic synthetic people or footage. The stick-figure style with an AI narrator most likely doesn't need it (see the style spec).

## Shape of the code

`pipeline/s10_upload.py`, using `google-api-python-client` and `google-auth-oauthlib`:

1. `videos.insert` (resumable) with the title, `description.txt`, tags, `categoryId` 27 (Education), `privacyStatus: private`, an optional `publishAt`, `selfDeclaredMadeForKids: false` and `containsSyntheticMedia` set from the brief.
2. `thumbnails.set` with `thumbnail.png`.
3. `captions.insert` with `captions.srt`.
4. Write `upload.json` and print the Studio link for a last look.

Command: `python -m pipeline upload <video>`. It will not run on a timer until Tejas says the review step is working well.
