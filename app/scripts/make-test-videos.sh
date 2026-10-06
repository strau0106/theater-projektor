#!/usr/bin/env bash
# Generate short H.264 1080p30 test clips with burnt-in name + timecode and a sample
# show.json into app/devdata/ (dev mode data dir).
#
# Usage: app/scripts/make-test-videos.sh [--force] [DATA_DIR]
set -euo pipefail

FORCE=0
if [[ "${1:-}" == "--force" ]]; then FORCE=1; shift; fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${1:-$HERE/../devdata}"
MEDIA="$DATA_DIR/media"
mkdir -p "$MEDIA" "$DATA_DIR/run"

command -v ffmpeg >/dev/null || { echo "ffmpeg not found (apt install ffmpeg)" >&2; exit 1; }

FONT="$(fc-match -f '%{file}' 'DejaVu Sans Mono:bold' 2>/dev/null || true)"
FONTOPT=""
[[ -n "$FONT" && -f "$FONT" ]] && FONTOPT="fontfile=$FONT:"

# make_clip NAME SECONDS COLOR [audio]
make_clip() {
    local name="$1" secs="$2" color="$3" audio="${4:-}"
    local out="$MEDIA/$name.mp4"
    if [[ -f "$out" && $FORCE -eq 0 ]]; then
        echo "exists: $out"
        return
    fi
    local label
    label="$(echo "$name" | tr '[:lower:]_' '[:upper:] ')"
    local vf="drawbox=x=0:y=0:w=iw:h=ih:color=${color}@0.35:t=fill,"
    vf+="drawtext=${FONTOPT}text='${label}':fontsize=160:fontcolor=white:x=(w-tw)/2:y=h*0.18:borderw=6,"
    vf+="drawtext=${FONTOPT}text='%{pts\\:hms}':fontsize=120:fontcolor=yellow:x=(w-tw)/2:y=h*0.62:borderw=6,"
    vf+="drawtext=${FONTOPT}text='frame %{frame_num}':fontsize=60:fontcolor=white:x=(w-tw)/2:y=h*0.80:borderw=4"
    local args=(-hide_banner -loglevel error -y
        -f lavfi -i "testsrc2=size=1920x1080:rate=30:duration=${secs}")
    if [[ -n "$audio" ]]; then
        args+=(-f lavfi -i "sine=frequency=440:duration=${secs}" -c:a aac -b:a 96k)
    fi
    args+=(-vf "$vf" -c:v libx264 -preset veryfast -profile:v high -level 4.0
        -pix_fmt yuv420p -g 30 -b:v 6M -movflags +faststart -t "$secs" "$out")
    echo "creating: $out"
    ffmpeg "${args[@]}"
}

make_clip clip_a 20 red
make_clip clip_b 12 blue
make_clip clip_c 10 green audio   # has an audio track (must be ignored by the player)

SHOW="$DATA_DIR/show.json"
if [[ -f "$SHOW" && $FORCE -eq 0 ]]; then
    echo "exists: $SHOW"
else
    cat > "$SHOW" <<'EOF'
{
  "version": 1,
  "videos": {
    "a": {"file": "clip_a.mp4", "title": "Clip A (pause points 5 s / 10 s)", "in_point": 0.0,
          "out_point": null, "pause_points": [5.0, 10.0], "fade_in": 1.0, "fade_out": 2.0,
          "end": "black"},
    "b": {"file": "clip_b.mp4", "title": "Clip B (hold last frame)", "in_point": 0.0,
          "out_point": null, "pause_points": [], "fade_in": 0.5, "fade_out": 1.5, "end": "hold"},
    "c": {"file": "clip_c.mp4", "title": "Clip C (loop 2-8 s)", "in_point": 2.0,
          "out_point": 8.0, "pause_points": [], "fade_in": 0.0, "fade_out": 1.0, "end": "loop"}
  },
  "cues": [
    {"id": "1", "name": "Opening", "video": "a",
     "fade_in": null, "fade_out": null, "pause_points": null, "end": null, "start_at": null},
    {"id": "2", "name": "Hold", "video": "b",
     "fade_in": null, "fade_out": null, "pause_points": null, "end": null, "start_at": null},
    {"id": "3", "name": "Loop", "video": "c",
     "fade_in": null, "fade_out": null, "pause_points": null, "end": null, "start_at": null},
    {"id": "4", "name": "A from 12 s, hold", "video": "a",
     "fade_in": 0.0, "fade_out": 3.0, "pause_points": [], "end": "hold", "start_at": 12.0}
  ]
}
EOF
    echo "created: $SHOW"
fi
