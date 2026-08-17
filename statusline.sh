#!/bin/bash
# statusline для Claude Code: показывает баланс radio-taiso.
# Конфигурируется install.sh в ~/.claude/settings.json (refreshInterval 2).
# Fail-open: любая ошибка — печатаем пусто, statusline не ломаем.
TAISO_PY="${TAISO_DIR:-$HOME/.radio-taiso}/bin/taiso.py"
[ -f "$TAISO_PY" ] || exit 0
/usr/bin/python3 "$TAISO_PY" status --line 2>/dev/null || true
