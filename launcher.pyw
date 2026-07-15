#!/usr/bin/env python3
"""
ChatterboxTTS Windows Launcher
First-run: installs PyTorch, transformers, and other dependencies (~2-3 GB)
Subsequent runs: launches ChatterboxTTS GUI immediately
Uses console output for progress feedback.
"""
import sys
import os
import subprocess
from pathlib import Path


INSTALL_DIR = Path(__file__).resolve().parent
BUNDLED_PYTHON = INSTALL_DIR / "python" / "python.exe"
BUNDLED_PYTHONW = INSTALL_DIR / "python" / "pythonw.exe"
LAUNCH_GUI = INSTALL_DIR / "chatterbox_gui.py"
REQUIREMENTS_TXT = INSTALL_DIR / "requirements.txt"
SETUP_MARKER = INSTALL_DIR / ".setup_complete"
NOCONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def has_nvidia_gpu():
    """Check if NVIDIA GPU is available using nvidia-smi."""
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0 and result.stdout.strip() != ''
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def verify_torch():
    """Verify PyTorch imports and actually works (not just imports)."""
    try:
        # Don't just import — actually create a tensor to trigger DLL loads
        result = subprocess.run(
            [str(BUNDLED_PYTHON), "-c", "import torch; x = torch.zeros(1); print('ok')"],
            capture_output=True, text=True, timeout=60
        )
        return result.returncode == 0 and 'ok' in result.stdout
    except Exception:
        return False


def setup_ffmpeg():
    """Ensure ffmpeg is on PATH; use imageio-ffmpeg binary if not found."""
    import shutil
    if shutil.which('ffmpeg'):
        return
    try:
        result = subprocess.run(
            [str(BUNDLED_PYTHON), "-c",
             "import imageio_ffmpeg, os; print(os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe()))"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            ffmpeg_dir = result.stdout.strip()
            if ffmpeg_dir:
                os.environ['PATH'] = ffmpeg_dir + os.pathsep + os.environ.get('PATH', '')
    except Exception:
        pass


def run_cmd(cmd, label=""):
    """Run a command with streaming output."""
    if label:
        print(f"  {label}...")
        sys.stdout.flush()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        creationflags=NOCONSOLE if sys.platform == "win32" else 0
    )
    for line in proc.stdout:
        line = line.strip()
        if line:
            print(f"    {line[:120]}")
    proc.wait()
    return proc


def run_setup():
    """Run first-time setup with console output."""
    print()
    print("=" * 60)
    print("  ChatterboxTTS First-Time Setup")
    print("=" * 60)
    print()
    print("This will download PyTorch, language models, and other")
    print("dependencies (~2-3 GB). It may take 10-20 minutes depending")
    print("on your internet speed.")
    print()

    print("[1/3] Upgrading pip and installing uv...")
    r = run_cmd(
        [str(BUNDLED_PYTHON), "-m", "pip", "install", "--upgrade", "pip"],
        "Upgrading pip"
    )
    if r.returncode != 0:
        print("  Warning: pip upgrade failed, continuing...")

    r = run_cmd(
        [str(BUNDLED_PYTHON), "-m", "pip", "install", "uv"],
        "Installing uv"
    )
    if r.returncode != 0:
        print("  FAILED: could not install uv. Check internet connection.")
        input("\nPress Enter to exit...")
        return False
    UV_EXE = BUNDLED_PYTHON.parent / "Scripts" / "uv.exe"
    if not UV_EXE.exists():
        print(f"  FAILED: uv.exe not found at {UV_EXE} after install.")
        input("\nPress Enter to exit...")
        return False

    print("[2/3] Installing PyTorch, ChatterboxFlash, and dependencies...")

    # Always uninstall first to bypass pip caching issues with wrong version
    run_cmd(
        [str(BUNDLED_PYTHON), "-m", "pip", "uninstall", "torch", "torchvision",
         "torchaudio", "-y"],
        "Clearing old PyTorch"
    )

    # chatterbox-tts (a dependency of chatterbox-flash) hard-pins torch==2.6.0,
    # which conflicts with the torch==2.7.1 pin in requirements.txt (needed for
    # RTX 50-series/Blackwell GPU support - older torch builds have no compiled
    # kernels for that architecture at all). Plain pip silently picks whichever
    # pin it resolves last; uv's --overrides forces our version to win instead,
    # in one combined resolution covering torch + chatterbox-flash + everything
    # else in requirements.txt together (not sequential pip calls, which is what
    # let this conflict slip through silently in the first place).
    overrides_path = INSTALL_DIR / "overrides.txt"
    overrides_path.write_text("torch==2.7.1\ntorchaudio==2.7.1\n")

    uv_cmd = [
        str(UV_EXE), "pip", "install", "--python", str(BUNDLED_PYTHON),
        "-r", str(REQUIREMENTS_TXT), "chatterbox-flash",
        "--overrides", str(overrides_path),
    ]
    if has_nvidia_gpu():
        print("  NVIDIA GPU detected - installing with CUDA 12.8 support...")
        uv_cmd += ["--extra-index-url", "https://download.pytorch.org/whl/cu128"]
    else:
        print("  No GPU detected - installing CPU-only...")
    r = run_cmd(uv_cmd, "Installing PyTorch + ChatterboxFlash")

    if r.returncode != 0:
        print("  FAILED: Installation failed. Check internet connection.")
        input("\nPress Enter to exit...")
        return False

    print("  Verifying PyTorch...")
    if not verify_torch():
        print("  FAILED: PyTorch is broken. Check your internet connection and try again.")
        input("\nPress Enter to exit...")
        return False
    print("  PyTorch OK")

    print("[3/3] Setup complete.")

    SETUP_MARKER.write_text("ok")
    print()
    print("=" * 60)
    print("  Setup complete! Launching ChatterboxTTS...")
    print("=" * 60)
    print()
    return True


def main():
    if not LAUNCH_GUI.exists():
        print(f"ERROR: chatterbox_gui.py not found at: {LAUNCH_GUI}")
        input("\nPress Enter to exit...")
        return 1

    if not BUNDLED_PYTHON.exists():
        print()
        print("=" * 60)
        print("  ChatterboxTTS Install Error")
        print("=" * 60)
        print()
        print("python.exe not found in the install directory.")
        print("Please reinstall ChatterboxTTS.")
        print()
        input("Press Enter to exit...")
        return 1

    # Check if PyTorch is broken even if setup supposedly completed
    if SETUP_MARKER.exists() and not verify_torch():
        print()
        print("=" * 60)
        print("  PyTorch Repair")
        print("=" * 60)
        print()
        print("PyTorch is not working correctly - running repair...")
        SETUP_MARKER.unlink(missing_ok=True)
        run_cmd(
            [str(BUNDLED_PYTHON), "-m", "pip", "uninstall", "torch", "torchvision",
             "torchaudio", "-y"],
            "Removing broken torch"
        )
        success = run_setup()
        if not success:
            return 1
    elif not SETUP_MARKER.exists():
        success = run_setup()
        if not success:
            return 1

    os.chdir(str(INSTALL_DIR))

    # Setup ffmpeg before launching GUI
    setup_ffmpeg()

    # Launch GUI in subprocess with torch pre-imported
    # Import torch BEFORE PyQt5 to avoid DLL initialization conflict on Windows
    # ChatterboxFlash's classes come from properly pip/uv-installed packages in
    # site-packages, not a local src/ tree - no extra sys.path entry needed.
    bootstrap = (
        f"import sys; sys.path.insert(0, r'{INSTALL_DIR}'); "
        f"from dotenv import load_dotenv; load_dotenv(); "
        f"import torch; "
        f"import runpy; runpy.run_path(r'{LAUNCH_GUI}', run_name='__main__')"
    )
    subprocess.run(
        [str(BUNDLED_PYTHON), "-c", bootstrap],
        cwd=str(INSTALL_DIR),
        env=os.environ.copy()
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
