# HHS Clips on your phone (Termux)

**What this is:** the same app, running entirely on your Android phone —
no Mac mini, no cloud, no computer needed. You open it in the phone's
browser, drop in a video, and the phone cuts the clips itself.

**What you need:** an Android phone with about 1 GB free, a Wi-Fi
connection for the one-time setup, and the charger plugged in for long
jobs. These steps were written for a Moto G Stylus (2025, Android 15)
and should work on any modern Android phone.

## Setup, step by step

### 1. Install Termux — from F-Droid, not the Play Store

Termux is a free app that gives your phone a Linux terminal. The
version on the Google Play Store is old and unmaintained — get it
from F-Droid instead:

- Install the **F-Droid** app (f-droid.org), then search F-Droid for
  **Termux** and install it.
- Also install **Termux:API** from F-Droid (same source). It lets
  Termux hold a wake lock so Android doesn't kill a long job when the
  screen locks.

Open Termux. You'll see a terminal prompt.

### 2. Install Python, ffmpeg, git

```bash
pkg update && pkg upgrade -y
pkg install -y python ffmpeg git
pip install -U yt-dlp
```

`yt-dlp` is what downloads a video when you paste a link instead of
uploading a file. Do **not** run `pip install -r requirements.txt`
here — that file includes `faster-whisper`, which does not install
on Android. On the phone you only need `yt-dlp`; everything else
is already covered.

### 3. Get the app

```bash
git clone https://github.com/hhsystems1/hhsclip.git
cd hhsclip
```

### 4. Get whisper-cli (the transcription engine)

The app normally transcribes with `faster-whisper`, but that has no
Android version. On the phone it uses `whisper-cli` instead — same
job, different engine. Download the ready-made Android binary:

```bash
cd ~
curl -L -o whisper-cli-android-arm64.tar.gz \
  https://github.com/uno-km/termux-stt/releases/download/v1.3.1/whisper-cli-android-arm64.tar.gz
mkdir -p ~/whisper-cli
tar xzf whisper-cli-android-arm64.tar.gz -C ~/whisper-cli
chmod +x ~/whisper-cli/whisper-cli
echo 'export PATH="$HOME/whisper-cli:$PATH"' >> ~/.bashrc
export PATH="$HOME/whisper-cli:$PATH"
whisper-cli --help | head -3
```

That archive is the `termux-stt` project's v1.3.1 release, built for
Android ARM64. Its published checksum is
`beab899246eb98335b519657c16515b8be4674d73a965e0e17308ab4f3115296`
— if you want to check it: `sha256sum whisper-cli-android-arm64.tar.gz`
should print that value. Keep the `.so` files in the same folder as
`whisper-cli`; the binary needs them next to it.

If that download ever stops working, the fallback is building it
yourself (takes a while — go make coffee):

```bash
pkg install -y cmake clang
git clone https://github.com/ggml-org/whisper.cpp.git
cd whisper.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j4 --target whisper-cli
cp build/bin/whisper-cli ~/whisper-cli/
```

### 5. Get the tiny model (~75 MB)

```bash
cd ~/hhsclip
mkdir -p models
curl -L -o models/ggml-tiny.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin
ls -la models/
```

If this file is missing, the clipper refuses with a plain message
saying where to get it — it never fails silently halfway through a job.

### 6. Let Termux see your files

```bash
termux-setup-storage
```

Tap Allow when Android asks. Your videos then live under
`~/storage/downloads/`, `~/storage/dcim/` (camera), and so on.

### 7. Keep the phone awake for long jobs

```bash
termux-wake-lock
```

This needs the Termux:API app from step 1. Run it before a long job
and `termux-wake-unlock` when you're done. Without it, Android may
pause Termux when the screen locks and the job stalls.

## Run it

```bash
cd ~/hhsclip
python3 server.py
```

Then open **http://localhost:8000** in the phone's browser. Same app
as on the computer: ＋ New to cut clips, Jobs to see them, Queue to
approve and post.

**Keep Termux open while it runs.** The server lives only as long as
the Termux window does — closing Termux stops the app. Plug the phone
in for long jobs.

From the command line instead:

```bash
python3 clipper.py ~/storage/downloads/your-video.mp4 --i-own-it
```

You don't need `--transcriber whisper-cpp`: the app picks
`whisper-cli` on its own when `faster-whisper` isn't installed.
The flag is there if you ever want to force it.

## What is different on the phone

- **Tiny model only.** The phone runs the `tiny` Whisper model.
  `small`/`base` would be slower and hungrier; `tiny` is the
  sensible one here.
- **Roughly real-time transcription.** A 20-minute video takes
  about 15–20 minutes just to transcribe, then the cutting on top.
  On the Mac mini the same job takes a few minutes. Plan
  accordingly — start a job, plug in, walk away.
- **No faster-whisper.** The transcription engine is `whisper-cli`
  (see step 4). The clips, scores, captions and approval flow are
  identical; the transcript itself can differ a little from the
  desktop version because it's a different engine hearing the same
  audio.
- **Battery and heat.** Transcription pins the CPU. The phone will
  get warm and drain fast — plug it in.
- **YouTube links can still be bot-gated.** Pasting a YouTube link
  may fail the same way it can on a computer (YouTube sometimes
  demands a sign-in). Uploading a file from the phone always works.
- **Posting needs the same setup as desktop.** The app never posts
  without approval, and the platform credentials still go in a
  `.env` file — see [SETUP.md](SETUP.md). Nothing about posting
  changes on the phone.
- **Same ownership rule.** Every job still needs the ownership
  attestation ("I own this video or have rights to post clips from
  it"). The app is for your own videos only.
