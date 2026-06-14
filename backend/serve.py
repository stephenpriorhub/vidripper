"""
Entry point for Railway — uses waitress WSGI server.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import app

port = int(os.environ.get('PORT', 8080))
print(f'[serve] Starting waitress on 0.0.0.0:{port}', flush=True)

from waitress import serve
serve(app, host='0.0.0.0', port=port, threads=4)
