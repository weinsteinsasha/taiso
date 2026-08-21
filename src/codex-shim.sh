#!/bin/bash
# radio-taiso Codex adapter v1 (обёртка вокруг настоящего codex).
# Ставится командой: taiso enable-codex (симлинк раньше настоящего codex в PATH).
# Механика: блок на старте новой сессии + тикер активности, пока codex работает.
# Mid-session блокировки нет — у Codex CLI нет hooks-API уровня Claude Code.
TAISO_DIR="${TAISO_DIR:-$HOME/.radio-taiso}"
TAISO="$TAISO_DIR/bin/taiso"
# защита от рекурсии (symlink/двойной PATH): второй вход — сразу к настоящему codex
if [ -n "${TAISO_SHIM_ACTIVE:-}" ]; then
  REAL=""
  while IFS= read -r cand; do
    [ "$cand" -ef "$0" ] && continue
    REAL="$cand"; break
  done < <(which -a codex 2>/dev/null)
  [ -n "$REAL" ] && exec "$REAL" "$@"
  exit 127
fi
export TAISO_SHIM_ACTIVE=1

# найти настоящий codex (первый в PATH, который не мы — сравнение по inode)
REAL=""
while IFS= read -r cand; do
  [ "$cand" -ef "$0" ] && continue
  REAL="$cand"; break
done < <(which -a codex 2>/dev/null)
[ -z "$REAL" ] && { echo "codex не найден в PATH"; exit 127; }

# шлагбаум на старте (fail-open: если taiso сломан — работаем)
if [ -x "$TAISO" ]; then
  if ! "$TAISO" gate; then
    "$TAISO" go >/dev/null 2>&1 || true
    # ждём завершения зарядки (окно пишет в базу; gate начнёт отвечать 0)
    while ! "$TAISO" gate >/dev/null 2>&1; do sleep 5; done
    echo "⛩ Готово — спина скажет спасибо. Запускаю codex."
  fi
  # тикер активности, пока codex жив
  ( while kill -0 $$ 2>/dev/null; do "$TAISO" ping-activity >/dev/null 2>&1; sleep 60; done ) &
  TICKER=$!
  trap 'kill $TICKER 2>/dev/null' EXIT
fi

exec "$REAL" "$@"
