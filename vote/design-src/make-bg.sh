#!/bin/sh
# Rebuild public/bg.jpg (1920x1080 cover crop) from the original artwork.
set -e
cd "$(dirname "$0")"
magick bg-original.png -filter Lanczos -resize 1920x -gravity center -crop 1920x1080+0+0 +repage \
  -unsharp 0x1.2+0.6+0 -strip -interlace none -sampling-factor 4:2:0 -quality 76 ../public/bg.jpg
