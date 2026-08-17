#!/bin/bash
# radio-taiso uninstall. Дизайн: 03_design.md W7.
# Порядок: лог события → settings.json (surgical, бэкап, откат) → каталог.
set -euo pipefail

TAISO_DIR="${TAISO_DIR:-$HOME/.radio-taiso}"
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
PY=/usr/bin/python3

echo "== radio-taiso uninstall =="

# --- 1. Событие в лог ДО снятия (данные для kill criteria)
if [ -f "$TAISO_DIR/taiso.db" ]; then
  TAISO_DIR="$TAISO_DIR" "$PY" - <<'PYEOF' || true
import os, sqlite3, time
d = os.environ["TAISO_DIR"]
con = sqlite3.connect(d + "/taiso.db")
con.execute("INSERT INTO events (ts_utc, type, payload) VALUES (?, 'uninstall', '{}')",
            (int(time.time()),))
con.commit()
PYEOF
fi

# --- 2. settings.json: surgical удаление только своих записей
if [ -f "$SETTINGS" ]; then
  BACKUP="$SETTINGS.bak-$(date +%Y%m%d-%H%M%S)"
  cp "$SETTINGS" "$BACKUP"
  echo "Бэкап: $BACKUP"
  SETTINGS="$SETTINGS" "$PY" <<'PYEOF'
import json, os, sys

path = os.environ["SETTINGS"]
settings = json.load(open(path))

hooks = settings.get("hooks") or {}
for event in list(hooks):
    matchers = hooks[event]
    for m in matchers:
        m["hooks"] = [h for h in (m.get("hooks") or [])
                      if h.get("_owner") != "radio-taiso"]
    hooks[event] = [m for m in matchers if m.get("hooks")]
    if not hooks[event]:
        del hooks[event]
if not hooks and "hooks" in settings:
    del settings["hooks"]

if (settings.get("statusLine") or {}).get("_owner") == "radio-taiso":
    del settings["statusLine"]

tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(settings, f, ensure_ascii=False, indent=2)
json.load(open(tmp))
os.replace(tmp, path)
print("settings.json: записи radio-taiso удалены, чужие hooks не тронуты")
PYEOF
fi

# --- 3. PATH и LaunchAgent
rm -f /usr/local/bin/taiso 2>/dev/null || true
launchctl unload "$HOME/Library/LaunchAgents/cy.radio-taiso.menubar.plist" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/cy.radio-taiso.menubar.plist" 2>/dev/null || true

# --- 4. Каталог: статистику по умолчанию оставляем
if [ -t 0 ]; then
  read -r -p "Удалить данные и статистику ($TAISO_DIR)? [y/N] " ANS
  case "${ANS:-N}" in
    [yY]*) rm -rf "$TAISO_DIR"; echo "Каталог удалён." ;;
    *) rm -rf "$TAISO_DIR/bin"; echo "Код удалён, база и статистика оставлены в $TAISO_DIR." ;;
  esac
else
  rm -rf "$TAISO_DIR/bin"
  echo "Код удалён, база оставлена (неинтерактивный режим)."
fi

echo "== Снято. Claude Code работает без ограничений. =="
