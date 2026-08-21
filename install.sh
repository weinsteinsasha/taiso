#!/bin/bash
# radio-taiso installer. Дизайн: 03_design.md W6.
# Единственный код, которому позволено трогать ~/.claude/settings.json.
# Правила: таймстампованный бэкап, merge через python json (не sed),
# post-merge assert «чужие hooks целы», автооткат, идемпотентность.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
TAISO_DIR="${TAISO_DIR:-$HOME/.radio-taiso}"
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
PY=/usr/bin/python3

echo "== radio-taiso install =="

# --- 1. Зависимости
command -v "$PY" >/dev/null || { echo "Нет /usr/bin/python3"; exit 1; }
xcrun -f swiftc >/dev/null 2>&1 || { echo "Нет Swift (Xcode CLT): xcode-select --install"; exit 1; }

# --- 2. Каталог, права
mkdir -p "$TAISO_DIR/bin"
chmod 700 "$TAISO_DIR"

# --- 3. Код на место
cp "$REPO_DIR/src/taiso.py" "$TAISO_DIR/bin/taiso.py"
chmod 755 "$TAISO_DIR/bin/taiso.py"
cp "$REPO_DIR/statusline.sh" "$TAISO_DIR/bin/statusline.sh"
chmod 755 "$TAISO_DIR/bin/statusline.sh"
cp "$REPO_DIR/uninstall.sh" "$TAISO_DIR/bin/uninstall.sh"
chmod 755 "$TAISO_DIR/bin/uninstall.sh"
cp "$REPO_DIR/src/codex-shim.sh" "$TAISO_DIR/bin/codex-shim.sh" 2>/dev/null || true
chmod 755 "$TAISO_DIR/bin/codex-shim.sh" 2>/dev/null || true

# CLI `taiso` в PATH (для Клода и терминала)
cat > "$TAISO_DIR/bin/taiso" <<EOF
#!/bin/bash
exec /usr/bin/python3 "$TAISO_DIR/bin/taiso.py" "\$@"
EOF
chmod 755 "$TAISO_DIR/bin/taiso"
SYMLINK_DONE=""
for SYMLINK_DIR in /opt/homebrew/bin /usr/local/bin; do
  if [ -d "$SYMLINK_DIR" ] && [ -w "$SYMLINK_DIR" ]; then
    ln -sf "$TAISO_DIR/bin/taiso" "$SYMLINK_DIR/taiso"
    echo "CLI: $SYMLINK_DIR/taiso"
    SYMLINK_DONE=1
    break
  fi
done
[ -n "$SYMLINK_DONE" ] || echo "ВНИМАНИЕ: PATH-каталоги недоступны — CLI по полному пути $TAISO_DIR/bin/taiso"

# --- 4. Компиляция окна
echo "Компилирую окно зарядки..."
xcrun swiftc -O -o "$TAISO_DIR/bin/TaisoWindow" "$REPO_DIR/src/TaisoWindow.swift"
codesign -s - -f "$TAISO_DIR/bin/TaisoWindow" 2>/dev/null || true
chmod 755 "$TAISO_DIR/bin/TaisoWindow"

# --- 5. База и конфиг
TAISO_DIR="$TAISO_DIR" "$PY" "$TAISO_DIR/bin/taiso.py" init
chmod 600 "$TAISO_DIR/config.json" 2>/dev/null || true

# --- 6. Видео (единственный сетевой акт; yt-dlp без --exec, фиксированный -o)
VIDEO="$TAISO_DIR/radio-taiso.mp4"
if [ ! -s "$VIDEO" ]; then
  command -v yt-dlp >/dev/null || brew install yt-dlp
  URL=$("$PY" -c "import json;print(json.load(open('$TAISO_DIR/config.json'))['video_url'])")
  echo "Скачиваю видео Radio Taiso..."
  # 1080p с merge через ffmpeg (если есть), иначе лучший прогрессивный mp4
  if command -v ffmpeg >/dev/null; then
    yt-dlp -f "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best" \
      --merge-output-format mp4 -o "$VIDEO" "$URL" || true
  else
    yt-dlp -f "best[ext=mp4][height<=1080]/best[ext=mp4]/best" -o "$VIDEO" "$URL" || true
  fi
  if [ -s "$VIDEO" ]; then
    SIZE=$(stat -f%z "$VIDEO")
    if [ "$SIZE" -lt 1000000 ] || [ "$SIZE" -gt 500000000 ]; then
      echo "Видео подозрительного размера ($SIZE байт) — удаляю, таймер-режим"; rm -f "$VIDEO"
    elif command -v ffprobe >/dev/null; then
      DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$VIDEO" 2>/dev/null | cut -d. -f1 || echo 0)
      [ "${DUR:-0}" -ge 60 ] && [ "${DUR:-0}" -le 900 ] || { echo "Длительность вне вилки ($DUR с) — удаляю"; rm -f "$VIDEO"; }
    fi
  fi
  if [ -s "$VIDEO" ]; then
    chmod 600 "$VIDEO"
    # зачёт = фактическая длина ролика (иначе окно обрубает музыку на пороге зачёта)
    VDUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$VIDEO" 2>/dev/null | cut -d. -f1 || echo "")
    "$PY" - "$TAISO_DIR" "$VIDEO" "${VDUR:-0}" <<'PYEOF'
import json, sys
d, v, dur = sys.argv[1], sys.argv[2], int(sys.argv[3] or 0)
p = d + "/config.json"
cfg = json.load(open(p))
cfg["video_path"] = v
if 30 <= dur <= 3600:
    cfg["exercise_seconds"] = dur
json.dump(cfg, open(p, "w"), ensure_ascii=False, indent=2)
PYEOF
  fi
fi
[ -s "$VIDEO" ] && echo "Видео: ок" || echo "Видео: нет (таймер-режим; положи mp4 в $VIDEO и пропиши video_path)"

# --- 7. Merge hooks + statusline в settings.json
BACKUP="$SETTINGS.bak-$(date +%Y%m%d-%H%M%S)"
[ -f "$SETTINGS" ] && cp "$SETTINGS" "$BACKUP" && echo "Бэкап: $BACKUP"

TAISO_DIR="$TAISO_DIR" SETTINGS="$SETTINGS" "$PY" <<'PYEOF'
import json, os, sys, copy

settings_path = os.environ["SETTINGS"]
taiso_dir = os.environ["TAISO_DIR"]
py = "/usr/bin/python3"
script = os.path.join(taiso_dir, "bin", "taiso.py")

try:
    with open(settings_path) as f:
        settings = json.load(f)
except (OSError, ValueError):
    settings = {}
original = copy.deepcopy(settings)

def foreign_hooks(s):
    """Все hook-записи, кроме наших (маркер _owner)."""
    out = []
    for event, matchers in (s.get("hooks") or {}).items():
        for m in matchers:
            for h in (m.get("hooks") or []):
                if h.get("_owner") != "radio-taiso":
                    out.append((event, json.dumps(h, sort_keys=True)))
    return sorted(out)

before = foreign_hooks(settings)

hooks = settings.setdefault("hooks", {})
def install(event, cmd):
    matchers = hooks.setdefault(event, [])
    # идемпотентность: убрать наши старые записи
    for m in matchers:
        m["hooks"] = [h for h in (m.get("hooks") or [])
                      if h.get("_owner") != "radio-taiso"]
    entry = {"type": "command", "command": cmd, "timeout": 5,
             "_owner": "radio-taiso"}
    # свой matcher-блок в конец (не мешаем mail-digest/gstack)
    matchers.append({"matcher": "*", "hooks": [entry]} if event == "PreToolUse"
                    else {"hooks": [entry]})
    hooks[event] = [m for m in matchers if m.get("hooks")]

install("UserPromptSubmit", f"{py} {script} hook-prompt")
install("PreToolUse", f"{py} {script} hook-tool")

if "statusLine" not in settings:  # чужую statusline не трогаем
    settings["statusLine"] = {
        "type": "command",
        "command": os.path.join(taiso_dir, "bin", "statusline.sh"),
        "refreshInterval": 2, "_owner": "radio-taiso"}

after = foreign_hooks(settings)
if before != after:
    print("АВАРИЯ: merge задел чужие hooks — откат"); sys.exit(2)

tmp = settings_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(settings, f, ensure_ascii=False, indent=2)
json.load(open(tmp))  # финальная валидация
os.replace(tmp, settings_path)
print("settings.json: hooks + statusline установлены")
PYEOF
RC=$?
if [ $RC -ne 0 ]; then
  [ -f "$BACKUP" ] && cp "$BACKUP" "$SETTINGS" && echo "Откат из бэкапа выполнен."
  exit $RC
fi

# --- 7.4 Codex: если стоит — включаем адаптер автоматически (opt-out: taiso disable-codex)
if command -v codex >/dev/null 2>&1; then
  TAISO_DIR="$TAISO_DIR" "$PY" "$TAISO_DIR/bin/taiso.py" enable-codex && \
    echo "Codex: адаптер включён автоматически (отключить: taiso disable-codex)"
fi

# --- 7.5 Menubar: LaunchAgent (автозапуск при логине) + запуск сейчас
LA_DIR="$HOME/Library/LaunchAgents"
LA_PLIST="$LA_DIR/cy.radio-taiso.menubar.plist"
if [ -z "${TAISO_NO_MENUBAR:-}" ] && [ "$TAISO_DIR" = "$HOME/.radio-taiso" ]; then
  mkdir -p "$LA_DIR"
  cat > "$LA_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>cy.radio-taiso.menubar</string>
  <key>ProgramArguments</key><array>
    <string>$TAISO_DIR/bin/TaisoWindow</string>
    <string>--menubar</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF
  pkill -f "TaisoWindow --menubar" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/cy.radio-taiso.menubar" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$LA_PLIST" 2>/dev/null || launchctl load "$LA_PLIST" 2>/dev/null || true
  sleep 2
  if ! pgrep -f "TaisoWindow --menubar" >/dev/null; then
    # launchd не поднял — стартуем напрямую, автозапуск при логине всё равно прописан
    (nohup "$TAISO_DIR/bin/TaisoWindow" --menubar >/dev/null 2>&1 &)
    sleep 2
  fi
  if pgrep -f "TaisoWindow --menubar" >/dev/null; then
    echo "Menubar: запущен ⛩ (ищи у часов; на MacBook с чёлкой иконки могут прятаться — сверни лишние)"
  else
    echo "ВНИМАНИЕ: menubar не стартовал. Диагностика: taiso doctor"
  fi
fi

# --- 8. Smoke-тест: фиктивный hook-вызов
SMOKE=$(echo '{"session_id":"install-smoke"}' | TAISO_DIR="$TAISO_DIR" "$PY" "$TAISO_DIR/bin/taiso.py" hook-prompt)
echo "$SMOKE" | grep -q "⛩" && echo "Smoke-тест: ок" || { echo "Smoke-тест ПРОВАЛЕН"; exit 1; }

# --- 9. Анкета-онбординг (FR-11) — пропускается при TAISO_NO_ONBOARD=1
if [ -t 0 ] && [ -z "${TAISO_NO_ONBOARD:-}" ]; then
  echo; echo "— Анкета (4 вопроса, настроит твой ритм) —"
  read -r -p "1. Сколько часов подряд обычно сидишь с агентами в день? " A1
  read -r -p "2. Делаешь ли утреннюю зарядку/пробежку? (да/нет) " A2
  read -r -p "3. Есть ли уже сигналы тела — спина/шея? (да/нет) " A3
  INTERVAL=40
  case "$A3" in [нН]*|[nN]*) INTERVAL=60;; esac
  read -r -p "4. Интервал между зарядками, минут [$INTERVAL]: " A4
  INTERVAL="${A4:-$INTERVAL}"
  TAISO_DIR="$TAISO_DIR" "$PY" - "$A1" "$A2" "$A3" "$INTERVAL" <<'PYEOF'
import json, os, sqlite3, sys, time
d = os.environ["TAISO_DIR"]
a1, a2, a3, interval = sys.argv[1:5]
con = sqlite3.connect(d + "/taiso.db")
for q, a in [("hours_daily", a1), ("morning_exercise", a2), ("body_signals", a3),
             ("interval_min", interval)]:
    con.execute("INSERT INTO profile (question, answer, ts_utc) VALUES (?,?,?)",
                (q, a, int(time.time())))
con.commit()
p = d + "/config.json"
cfg = json.load(open(p))
try:
    cfg["work_minutes_per_exercise"] = max(5, min(480, int(interval)))
except ValueError:
    pass
json.dump(cfg, open(p, "w"), ensure_ascii=False, indent=2)
print("Профиль сохранён, интервал: %s мин" % cfg["work_minutes_per_exercise"])
PYEOF
fi

echo; echo "== Готово. Radio-taiso активен для всех сессий Claude Code. =="
echo "Проверка: taiso status · Зарядка: taiso go · Снятие: ./uninstall.sh"
