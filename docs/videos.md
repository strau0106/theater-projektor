# Preparing videos

Upload clips that were converted with `projektor-prepare`. The web UI marks them with a green
"prepared" tag; other clips show warnings if they are likely to misbehave on the Pi.

## Recommended format

- MP4, H.264 High profile, level 4.1 or lower, at most 1920x1080, 8-bit yuv420p
- constant frame rate (24, 25 or 30; 50/60 fps sources are halved)
- closed GOP with a keyframe every 0.5 s
- no audio track, `+faststart`
- metadata marker `comment=projektor-prepared v1 gop=0.5 fps=25`

## Why

- **Seek, loop and start_at cost.** The player can only start decoding at a keyframe. Camera or
  editor exports often have a keyframe every 5-10 s, so every loop, in-point, pause-resume or
  `start_at` has to decode up to that many seconds first, which shows as a stall or a late start.
  A keyframe every 0.5 s keeps that under half a second.
- **Pi 4** has hardware H.264 decode up to 1080p30 (level 4.1/4.2). 4K, 10-bit, 4:2:2 or high frame
  rates fall back to software or fail.
- **Pi 5** has no H.264 hardware decoder but does hardware HEVC (Main, up to 4K). 1080p30 H.264 is
  still fine in software; for Pi 5 only use `--codec hevc`.
- CFR avoids audio-less drift and uneven frame pacing.

## Install ffmpeg (on your computer, not on the Pi)

    sudo apt install ffmpeg      # Debian/Ubuntu
    brew install ffmpeg          # macOS

`ffmpeg` and `ffprobe` must be on `PATH`. `projektor-prepare` is installed with the app
(`pip install -e app`).

## Usage

    projektor-prepare clip.mov                       # writes clip.mp4 next to the input
    projektor-prepare *.mov -o prepared/             # output directory
    projektor-prepare *.mov -o prepared/ --upload http://projektor.local --password-file pw.txt
    projektor-prepare clip.mov --codec hevc          # Pi 5
    projektor-prepare clip.mov --dry-run             # print the ffmpeg command only

Files that already carry the marker are skipped (and still uploaded with `--upload`); use `--force`
to re-encode. If the output is the input file name, `.prepared.mp4` is used. See
`projektor-prepare --help` for `--fps`, `--height`, `--gop-s`, `--crf`, `--preset`, `--keep-audio`.

## ffprobe on the Pi

Not required. Keyframe interval and the marker of MP4/MOV files are read by a built-in parser.
Without `ffprobe`, other containers (MKV, WebM) get no keyframe-interval warning, and without both
GStreamer Discoverer and `ffprobe` probing is unavailable. Playback is never affected.
