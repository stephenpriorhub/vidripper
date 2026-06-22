"""
Entry point for Railway — uses waitress WSGI server.
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _ensure_chromium() -> None:
    """
    Ensure the Playwright Chromium binary exists at PLAYWRIGHT_BROWSERS_PATH.

    On Railway, PLAYWRIGHT_BROWSERS_PATH points at the persistent /data volume,
    which is empty after a fresh deploy. The OS-level deps are baked into the
    image at build time; here we just (re)install the browser binary itself if
    it's missing. Best-effort — failure must not block app startup.
    """
    browsers_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    if not browsers_path:
        return  # local dev — rely on Playwright's default cache location
    marker_dir = Path(browsers_path)
    has_chromium = marker_dir.is_dir() and any(marker_dir.glob('chromium-*'))
    if has_chromium:
        print(f'[serve] Chromium already present at {browsers_path}', flush=True)
        return
    print(f'[serve] Installing Chromium to {browsers_path} ...', flush=True)
    try:
        subprocess.run(
            [sys.executable, '-m', 'playwright', 'install', 'chromium'],
            check=False,
            timeout=300,
        )
    except Exception as exc:
        print(f'[serve] Chromium install failed (screenshots unavailable): {exc}', flush=True)


_ensure_chromium()

from app import app

port = int(os.environ.get('PORT', 8080))
print(f'[serve] Starting waitress on 0.0.0.0:{port}', flush=True)

from waitress import serve
serve(app, host='0.0.0.0', port=port, threads=4)
