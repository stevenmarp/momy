#!/bin/sh
# MoMy installer for Linux and macOS. https://momy.stevenmarp.xyz
# Installs uv (if missing), then MoMy as a uv tool, then a menu entry on Linux.
set -eu
SRC="${MOMY_SOURCE:-git+https://github.com/stevenmarp/momy}"

echo "MoMy: memasang..."
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
fi
uv tool install --force --python 3.12 "$SRC"
BIN="$(uv tool dir --bin)"

if [ "$(uname)" = Linux ]; then
  mkdir -p "$HOME/.local/share/applications"
  cat > "$HOME/.local/share/applications/momy.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=MoMy
Comment=Minutes of Meeting, yours
Exec="$BIN/momy-ui"
Icon=audio-input-microphone
Terminal=false
Categories=Office;
DESKTOP
fi

echo
echo "Selesai. Buka MoMy dari menu aplikasi, atau jalankan: $BIN/momy-ui"
if ! command -v ollama >/dev/null 2>&1; then
  echo
  echo "Supaya notulen ditulis gratis dan offline: pasang Ollama dari https://ollama.com,"
  echo "lalu jalankan: ollama pull gemma3:4b"
  echo "Atau isi alamat API dan API key layanan AI lewat tombol Pengaturan AI di aplikasi."
fi
