# Preparing videos and images

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
    projektor-prepare ~/Show/Medien                  # whole folder -> ~/Show/Medien/prepared/

Files that already carry the marker are skipped (and still uploaded with `--upload`); use `--force`
to re-encode. If the output is the input file name, `.prepared.mp4` is used.

Folders are searched recursively for videos and images (hidden files and `prepared/` folders are
skipped, so re-running on the same folder is safe). With a single folder and no `-o`, the results go
to `<folder>/prepared/`. Two inputs that would produce the same output name (e.g. `a.heic` and
`a.jpg` -> `a.jpg`) are reported as an error; rename one. See
`projektor-prepare --help` for `--fps`, `--height`, `--gop-s`, `--crf`, `--preset`, `--keep-audio`.

## Images

Still images (JPEG, PNG, WebP) are cues like videos: upload them in the Media tab, put them in
the cue list. An image fades in over its `fade_in` when its cue runs, stays on screen until the
next cue or `stop`, and fades out over its `fade_out` on `/proj/fadeout` (or the web Fade out
button). Both times are set per image and can be overridden per cue. In/out points, pause points,
`start_at` and the end mode do not apply to images.

The Pi decodes an image in software each time it is shown, so a large camera photo starts later.
It also applies the EXIF orientation and shrinks the picture to the display size itself, but
preparing images first makes cues start faster:

    projektor-prepare IMG_*.jpg poster.png -o prepared/ --upload http://projektor.local --password-file pw.txt
    projektor-prepare ~/Show/Bilder                  # all images (and videos) -> ~/Show/Bilder/prepared/

- EXIF orientation applied (portrait phone photos come out upright), metadata stripped
- shrunk to fit 1920x1080 (16:9 box of `--height`), never upscaled, aspect ratio kept
- output `<name>.jpg` (high quality JPEG); PNG inputs stay PNG (lossless, keeps transparency,
  transparent areas show black). HEIC, TIFF, BMP, GIF, AVIF, WebP inputs become JPEG (HEIC/AVIF need
  an ffmpeg build that can decode them)
- an existing output is skipped unless `--force`

The web UI warns about images larger than 1920x1080 and about formats the projector can't show.

## ffprobe on the Pi

Not required. Keyframe interval and the marker of MP4/MOV files are read by a built-in parser.
Without `ffprobe`, other containers (MKV, WebM) get no keyframe-interval warning, and without both
GStreamer Discoverer and `ffprobe` probing is unavailable. Playback is never affected.
