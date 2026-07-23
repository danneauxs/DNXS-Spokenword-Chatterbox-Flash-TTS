#!/bin/bash
# ChatterboxTTS installation script.
# Creates local venv, installs only requirements.txt, and leaves models to
# download on first run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
VENV_DIR="venv"
VENV_PATH="$SCRIPT_DIR/$VENV_DIR"

echo "ChatterboxTTS installation"
echo "==========================="

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is required but not installed."
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "Python $PYTHON_VERSION detected"

if [[ "${VIRTUAL_ENV:-}" != "$VENV_PATH" ]] && declare -F deactivate >/dev/null 2>&1; then
    deactivate
fi

if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating virtual environment at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

echo "Activating virtual environment: $VENV_PATH"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

if ! python -m pip --version >/dev/null 2>&1; then
    echo "Bootstrapping pip inside virtual environment"
    python -m ensurepip --upgrade
fi

python -m pip install --upgrade pip

PIP_ARGS=()
if command -v nvidia-smi >/dev/null 2>&1; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1 || true)
    if [[ -n "${GPU_NAME:-}" ]]; then
        echo "NVIDIA GPU detected: $GPU_NAME"
        PIP_ARGS+=(--extra-index-url https://download.pytorch.org/whl/cu128)
    fi
fi

echo "Installing requirements.txt"
python -m pip install -r requirements.txt "${PIP_ARGS[@]}"

if [[ ! -f .env && -f .env.template ]]; then
    cp .env.template .env
    echo "Created .env from .env.template"
fi

python - <<'PY'
import sys

checks = [
    ("torch", "import torch"),
    ("Flash model package", "from chatterbox_flash.tts import ChatterboxFlashTTS"),
    ("GUI", "import chatterbox_gui"),
    ("main interface", "import interface"),
    ("JSON generator", "from utils.generate_from_json import main"),
    ("repair tools", "import wrapper.chunk_tool"),
]

for label, code in checks:
    try:
        exec(code, {})
        print(f"OK: {label}")
    except Exception as exc:
        print(f"FAILED: {label}: {exc}")
        sys.exit(1)
PY

echo
echo "Installation complete."
echo "Start app with: ./0.sh"
echo "First run will download required models automatically"
echo ""
echo "⚠️  HuggingFace Token Required"
echo "   The Turbo model requires authentication to download."
echo "   1. Get your token at: https://huggingface.co/settings/tokens"
echo "   2. Copy .env.template to .env:"
echo "      cp .env.template .env"
echo "   3. Edit .env and replace 'your_huggingface_token_here' with your token"
echo ""
echo "   Or set the environment variable directly:"
echo "      export HF_TOKEN=your_token_here"
