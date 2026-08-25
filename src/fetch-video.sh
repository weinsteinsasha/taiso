#!/bin/bash
# Скачивание видео Radio Taiso (вызывается install.sh и `taiso video`).
# Обновляет yt-dlp перед скачиванием: YouTube отдаёт 403 старым версиям.
set -uo pipefail
TAISO_DIR="${TAISO_DIR:-$HOME/.radio-taiso}"
PY=/usr/bin/python3
VIDEO="$TAISO_DIR/radio-taiso.mp4"
URL=$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["video_url"])' "$TAISO_DIR/config.json")

command -v yt-dlp >/dev/null || brew install yt-dlp >/dev/null 2>&1
# обновление: сначала встроенный -U (pip/standalone), потом brew
yt-dlp -U >/dev/null 2>&1 || brew upgrade yt-dlp >/dev/null 2>&1 || "$PY" -m pip install -q -U yt-dlp >/dev/null 2>&1 || true

echo "Скачиваю видео Radio Taiso..."
rm -f "$VIDEO" "$VIDEO".part 2>/dev/null  # всегда с нуля: докачка после 403 клеит битый файл
COMMON=(--ignore-config --no-config-locations --no-update --no-continue -o "$VIDEO")
if command -v ffmpeg >/dev/null; then
  yt-dlp "${COMMON[@]}" -f "bestvideo[vcodec^=avc1][height<=1080]+bestaudio[ext=m4a]/best[vcodec^=avc1][ext=mp4]/best[ext=mp4]" \
    --merge-output-format mp4 -- "$URL" \
  || yt-dlp "${COMMON[@]}" -f "best[ext=mp4]/best" --extractor-args "youtube:player_client=ios" -- "$URL" || true
else
  yt-dlp "${COMMON[@]}" -f "best[vcodec^=avc1][ext=mp4][height<=1080]/best[ext=mp4]" -- "$URL" \
  || yt-dlp "${COMMON[@]}" -f "best[ext=mp4]/best" --extractor-args "youtube:player_client=ios" -- "$URL" || true
fi

if [ -s "$VIDEO" ]; then
  SIZE=$(stat -f%z "$VIDEO")
  if [ "$SIZE" -lt 1000000 ] || [ "$SIZE" -gt 500000000 ]; then
    echo "Видео подозрительного размера ($SIZE байт) — удаляю"; rm -f "$VIDEO"
  elif command -v ffprobe >/dev/null; then
    DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$VIDEO" 2>/dev/null | cut -d. -f1 || echo 0)
    [ "${DUR:-0}" -ge 60 ] && [ "${DUR:-0}" -le 900 ] || { echo "Длительность вне вилки ($DUR с) — удаляю"; rm -f "$VIDEO"; }
  fi
  # видео обязано реально декодироваться (битые склейки: звук есть, картинки нет)
  if [ -s "$VIDEO" ] && command -v ffmpeg >/dev/null; then
    ffmpeg -v error -xerror -ss 0 -t 5 -i "$VIDEO" -map 0:v:0 -f null - 2>/dev/null       || { echo "Видео не декодируется (битый файл) — удаляю"; rm -f "$VIDEO"; }
  fi
fi
if [ -s "$VIDEO" ]; then
  chmod 600 "$VIDEO"
  VDUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$VIDEO" 2>/dev/null | cut -d. -f1 || echo "")
  "$PY" - "$TAISO_DIR" "$VIDEO" "${VDUR:-0}" <<'PYEOF'
import json, sys
d, v, dur = sys.argv[1], sys.argv[2], int(sys.argv[3] or 0)
p = d + "/config.json"
cfg = json.load(open(p))
cfg["video_path"] = v
if 60 <= dur <= 900:
    cfg["exercise_seconds"] = max(60, dur - 30)  # минус аутро: в конце Аки прощается, а не упражняется
json.dump(cfg, open(p, "w"), ensure_ascii=False, indent=2)
PYEOF
  echo "Видео: ок"
  exit 0
else
  echo "Видео: не скачалось (таймер-режим). Повторить позже: taiso video"
  exit 1
fi
