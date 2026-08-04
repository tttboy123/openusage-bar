#!/bin/zsh
set -euo pipefail

ROOT=${0:A:h:h}
SOURCE=${1:-"$ROOT/docs/assets/brand/openusage-bar-icon.png"}
OUTPUT=${2:-"$ROOT/swift_app/Resources/OpenUsageBar.icns"}
TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/openusage-app-icon.XXXXXX")
ICONSET="$TEMP_ROOT/AppIcon.iconset"
mkdir "$ICONSET"
trap 'find "$TEMP_ROOT" -depth -delete' EXIT

[[ -f "$SOURCE" ]] || { print -u2 "icon source unavailable"; exit 1; }
[[ $(sips -g pixelWidth "$SOURCE" | awk '/pixelWidth/ {print $2}') == 1024 ]] || {
  print -u2 "icon source must be 1024 pixels wide"
  exit 1
}
[[ $(sips -g pixelHeight "$SOURCE" | awk '/pixelHeight/ {print $2}') == 1024 ]] || {
  print -u2 "icon source must be 1024 pixels high"
  exit 1
}

while read -r size name; do
  sips -z "$size" "$size" "$SOURCE" --out "$ICONSET/$name" >/dev/null
done <<'SIZES'
16 icon_16x16.png
32 icon_16x16@2x.png
32 icon_32x32.png
64 icon_32x32@2x.png
128 icon_128x128.png
256 icon_128x128@2x.png
256 icon_256x256.png
512 icon_256x256@2x.png
512 icon_512x512.png
1024 icon_512x512@2x.png
SIZES

mkdir -p "${OUTPUT:h}"
iconutil -c icns "$ICONSET" -o "$OUTPUT"
print "generated $OUTPUT"
