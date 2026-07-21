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
    Boot-time safety net: ensure a Playwright Chromium binary is present.

    The browser is installed into the image at build time (see railway.toml), so
    this normally finds it and returns. It only reinstalls if the binary is
    missing — e.g. a Playwright version bump changed the expected revision.
    Best-effort — failure must not block app startup.

    Checks whichever path Playwright will actually use: PLAYWRIGHT_BROWSERS_PATH
    if set, otherwise the default cache (~/.cache/ms-playwright). Recognizes both
    the full-browser (`chromium-*`) and headless-shell (`chromium_headless_shell-*`)
    directory names that newer Playwright versions create.
    """
    browsers_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or str(
        Path.home() / '.cache' / 'ms-playwright'
    )
    marker_dir = Path(browsers_path)
    has_chromium = marker_dir.is_dir() and (
        any(marker_dir.glob('chromium-*'))
        or any(marker_dir.glob('chromium_headless_shell-*'))
    )
    if has_chromium:
        print(f'[serve] Chromium already present at {browsers_path}', flush=True)
        return
    print(f'[serve] Chromium missing at {browsers_path}; installing ...', flush=True)
    try:
        subprocess.run(
            [sys.executable, '-m', 'playwright', 'install', 'chromium'],
            check=False,
            timeout=300,
        )
    except Exception as exc:
        print(f'[serve] Chromium install failed (screenshots unavailable): {exc}', flush=True)


_ensure_chromium()

from app import app, start_archiver

# Kick off the daily Google Drive archival sweep (no-ops until Drive is configured).
start_archiver()

port = int(os.environ.get('PORT', 8080))
print(f'[serve] Starting waitress on 0.0.0.0:{port}', flush=True)

from waitress import serve
serve(app, host='0.0.0.0', port=port, threads=4)
