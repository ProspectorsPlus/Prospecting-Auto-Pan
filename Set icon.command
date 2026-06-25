#!/bin/bash
# Builds the app icon from icon.png (save the cowboy image as icon.png in this
# folder first), then applies it to Prospectors Plus.app.
cd "$(dirname "$0")" || exit 1
SRC="icon.png"
APP="Prospectors Plus.app"
if [ ! -f "$SRC" ]; then
  osascript -e 'display alert "icon.png not found" message "Save the image as icon.png in this folder (same place as the app), then run this again."'
  exit 1
fi
mkdir -p "$APP/Contents/Resources" icon.iconset
for s in 16 32 64 128 256 512; do
  sips -z $s $s     "$SRC" --out "icon.iconset/icon_${s}x${s}.png"     >/dev/null 2>&1
  d=$((s*2)); sips -z $d $d "$SRC" --out "icon.iconset/icon_${s}x${s}@2x.png" >/dev/null 2>&1
done
iconutil -c icns icon.iconset -o "$APP/Contents/Resources/icon.icns" && echo "icon built"
rm -rf icon.iconset
touch "$APP"
# nudge Finder/Dock to refresh the icon
/usr/bin/killall Dock >/dev/null 2>&1
osascript -e 'display notification "Icon applied to Prospectors Plus" with title "Done"'
