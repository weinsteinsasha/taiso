#!/bin/bash
# radio-taiso one-liner installer:
# /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/weinsteinsasha/taiso/main/get.sh)"
set -euo pipefail

[ "$(uname)" = "Darwin" ] || { echo "Только macOS."; exit 1; }
command -v git >/dev/null || { echo "Нужен git (xcode-select --install)"; exit 1; }

SRC=$(mktemp -d)
trap 'rm -rf "$SRC"' EXIT
echo "⛩ Скачиваю radio-taiso..."
git clone --depth 1 --quiet https://github.com/weinsteinsasha/taiso.git "$SRC"
cd "$SRC"
./install.sh
exit $?
