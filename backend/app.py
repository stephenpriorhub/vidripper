"""
VideoRipper — Flask backend.
Auth is handled client-side by hub-nav.js; server routes are unauthenticated.
"""
import io
import json
import os
import uuid
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, abort, Response

import ripper
import rev_client

# ── paths ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / 'frontend'
# DATA_DIR: use /data (Railway volume mount point) when it exists, else fall
# back to the repo-relative data/ dir for local dev.
_volume = Path('/data')
DATA_DIR = _volume if _volume.is_dir() else BASE_DIR / 'data'
VIDEOS_DIR = DATA_DIR / 'videos'
SCREENSHOTS_DIR = DATA_DIR / 'screenshots'
MANIFEST_PATH = DATA_DIR / 'manifest.json'

VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Flask app ───────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=str(FRONTEND_DIR))

_manifest_lock = threading.Lock()


# ── manifest helpers ────────────────────────────────────────────────────────

def _load_manifest() -> list:
    if not MANIFEST_PATH.exists():
        return []
    with open(MANIFEST_PATH) as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def _save_manifest(jobs: list) -> None:
    with open(MANIFEST_PATH, 'w') as f:
        json.dump(jobs, f, indent=2)


def _get_job(job_id: str) -> dict | None:
    jobs = _load_manifest()
    return next((j for j in jobs if j['id'] == job_id), None)


def _update_job(job_id: str, updates: dict) -> dict | None:
    with _manifest_lock:
        jobs = _load_manifest()
        for job in jobs:
            if job['id'] == job_id:
                job.update(updates)
                _save_manifest(jobs)
                return job
    return None


def _append_job(job: dict) -> None:
    with _manifest_lock:
        jobs = _load_manifest()
        jobs.append(job)
        _save_manifest(jobs)


def _delete_job(job_id: str) -> bool:
    with _manifest_lock:
        jobs = _load_manifest()
        new_jobs = [j for j in jobs if j['id'] != job_id]
        if len(new_jobs) == len(jobs):
            return False
        _save_manifest(new_jobs)
    return True


def _find_duplicate(source_url: str) -> dict | None:
    """Return the first completed job with the same source_url, or None."""
    jobs = _load_manifest()
    for job in jobs:
        if (job.get('source_url') == source_url
                and job.get('rev_status') == 'complete'
                and job.get('transcript_text')):
            return job
    return None


# ── docx generation ─────────────────────────────────────────────────────────

def _build_docx(title: str, transcript_text: str) -> bytes:
    """Return .docx bytes for the given transcript."""
    try:
        from docx import Document
        doc = Document()
        doc.add_heading(title or 'Transcript', level=1)
        # Split into paragraphs on double newline, add each as a paragraph
        paragraphs = [p.strip() for p in transcript_text.split('\n\n') if p.strip()]
        if not paragraphs:
            paragraphs = [transcript_text]
        for para in paragraphs:
            doc.add_paragraph(para)
        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()
    except ImportError:
        raise RuntimeError('python-docx is not installed')


# ── background pipeline ─────────────────────────────────────────────────────

def _take_screenshot(job_id: str, source_url: str) -> None:
    """Take a Playwright screenshot of source_url; save to SCREENSHOTS_DIR/{job_id}.png."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox'],
            )
            context = browser.new_context(
                user_agent=(
                    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/120.0.0.0 Safari/537.36'
                ),
                viewport={'width': 1280, 'height': 800},
            )
            pw_cookies = ripper._load_cookies_for_playwright(source_url)
            if pw_cookies:
                context.add_cookies(pw_cookies)
            page = context.new_page()
            try:
                from playwright_stealth import stealth_sync
                stealth_sync(page)
            except ImportError:
                pass
            try:
                page.goto(source_url, wait_until='domcontentloaded', timeout=30000)
                page.wait_for_load_state('networkidle', timeout=8000)
            except Exception:
                pass
            dest = str(SCREENSHOTS_DIR / f'{job_id}.png')
            page.screenshot(path=dest, full_page=False)
            browser.close()
        _update_job(job_id, {'has_screenshot': True})
    except Exception as exc:
        # Non-fatal — screenshot failure should not block transcription
        _update_job(job_id, {'screenshot_error': str(exc)[:200]})


def _run_pipeline(job_id: str, source_url: str) -> None:
    """
    Full rip pipeline: fetch → detect → download → screenshot → Rev submit.
    Runs in a background thread; updates manifest at each step.
    """
    try:
        # Step 1: fetch page (skip for direct player URLs — no useful metadata there)
        _update_job(job_id, {'pipeline_step': 'fetching_page'})
        if 'players.brightcove.net' in source_url:
            html, title, thumbnail = '', source_url, ''
        elif 'fast.wistia.com/medias/' in source_url or 'wistia.com/medias/' in source_url:
            # Direct Wistia media URL — extract ID from URL, skip page fetch
            import re as _re
            _m = _re.search(r'wistia\.com/medias/([a-z0-9]+)', source_url)
            html, title, thumbnail = '', source_url, ''
            if _m:
                _update_job(job_id, {
                    'platform': 'wistia',
                    'video_id': _m.group(1),
                    'pipeline_step': 'downloading_video',
                })
                dest_path = str(VIDEOS_DIR / f'{job_id}.mp4')
                ripper.download_video('wistia', _m.group(1), source_url, dest_path, {})
                _update_job(job_id, {
                    'local_video': f'/data/videos/{job_id}.mp4',
                    'pipeline_step': 'taking_screenshot',
                })
                job = _get_job(job_id)
                _take_screenshot(job_id, job.get('page_url') or source_url)
                _update_job(job_id, {'pipeline_step': 'submitting_to_rev'})
                app_base = os.environ.get('APP_BASE_URL', 'https://vidripper.oxfordhub.app')
                video_url = f'{app_base}/api/jobs/{job_id}/video'
                now = datetime.now(timezone.utc).isoformat()
                order_id = rev_client.submit_job(video_url, metadata=job_id)
                _update_job(job_id, {
                    'rev_order_id': order_id,
                    'rev_status': 'in_progress',
                    'rev_submitted_at': now,
                    'pipeline_step': 'done',
                })
                return
        else:
            html, title, thumbnail = ripper.fetch_page(source_url)
        _update_job(job_id, {'title': title, 'thumbnail_url': thumbnail})

        # Step 2: detect platform — if unknown, retry with headless browser
        platform, video_id, extra = ripper.detect_platform(html, source_url)
        if platform == 'unknown':
            _update_job(job_id, {'pipeline_step': 'fetching_page_rendered'})
            html, title_r, thumbnail_r, bc_attrs = ripper.fetch_page_rendered(source_url)
            if title_r and title_r != source_url:
                _update_job(job_id, {'title': title_r, 'thumbnail_url': thumbnail_r})
            # If Playwright found BrightCove attrs directly in the DOM, use them
            if bc_attrs and bc_attrs.get('video_id') and bc_attrs.get('account_id'):
                platform = 'brightcove'
                video_id = bc_attrs['video_id']
                extra = {
                    'bc_account_id': bc_attrs['account_id'],
                    'bc_player_id': bc_attrs.get('player_id', ''),
                }
            else:
                platform, video_id, extra = ripper.detect_platform(html, source_url)
        _update_job(job_id, {
            'platform': platform,
            'video_id': video_id,
            'pipeline_step': 'downloading_video',
        })

        # Step 3: download
        dest_path = str(VIDEOS_DIR / f'{job_id}.mp4')
        ripper.download_video(platform, video_id, source_url, dest_path, extra)
        _update_job(job_id, {
            'local_video': f'/data/videos/{job_id}.mp4',
            'pipeline_step': 'taking_screenshot',
        })

        # Step 4: screenshot (non-blocking on failure)
        job = _get_job(job_id)
        _take_screenshot(job_id, job.get('page_url') or source_url)
        _update_job(job_id, {'pipeline_step': 'submitting_to_rev'})

        # Step 5: Rev AI — submit video URL directly, no upload needed
        app_base = os.environ.get('APP_BASE_URL', 'https://vidripper.oxfordhub.app')
        video_url = f'{app_base}/api/jobs/{job_id}/video'
        now = datetime.now(timezone.utc).isoformat()
        order_id = rev_client.submit_job(video_url, metadata=job_id)
        _update_job(job_id, {
            'rev_order_id': order_id,
            'rev_status': 'in_progress',
            'rev_submitted_at': now,
            'pipeline_step': 'done',
        })

    except Exception as exc:
        _update_job(job_id, {
            'rev_status': 'failed',
            'pipeline_step': 'failed',
            'error': str(exc)[:500],
        })


# ── routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(str(FRONTEND_DIR), 'index.html')


@app.route('/api/ping')
def ping():
    return jsonify({'status': 'ok', 'service': 'vidripper'})


@app.route('/api/rip', methods=['POST'])
def rip():
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'url is required'}), 400
    if not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'url must start with http:// or https://'}), 400

    # ── Duplicate check ──────────────────────────────────────────────────────
    existing = _find_duplicate(url)
    if existing:
        return jsonify({'duplicate': True, 'existing_job': existing}), 200

    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    page_url = (data.get('page_url') or '').strip() or None

    job = {
        'id': job_id,
        'source_url': url,
        'page_url': page_url,
        'platform': 'unknown',
        'video_id': '',
        'title': '',
        'thumbnail_url': '',
        'local_video': '',
        'rev_order_id': '',
        'rev_status': 'pending',
        'rev_submitted_at': '',
        'rev_transcript_url': '',
        'transcript_text': '',
        'has_screenshot': False,
        'pipeline_step': 'queued',
        'error': '',
        'created_at': now,
    }
    _append_job(job)

    thread = threading.Thread(target=_run_pipeline, args=(job_id, url), daemon=True)
    thread.start()

    return jsonify(job), 202


@app.route('/api/jobs')
def list_jobs():
    jobs = _load_manifest()
    # Most recent first
    jobs.sort(key=lambda j: j.get('created_at', ''), reverse=True)
    return jsonify(jobs)


@app.route('/api/jobs/<job_id>')
def get_job(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found', 'id': job_id}), 404
    return jsonify(job)


@app.route('/api/jobs/<job_id>/transcript')
def get_transcript(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found', 'id': job_id}), 404

    # Return cached transcript if present
    if job.get('transcript_text'):
        return jsonify({'transcript': job['transcript_text'], 'source': 'cache'})

    order_id = job.get('rev_order_id')
    if not order_id:
        return jsonify({'error': 'No Rev order associated with this job'}), 400

    try:
        status = rev_client.get_job_status(order_id)
        _update_job(job_id, {'rev_status': status})

        if status != 'complete':
            return jsonify({'status': status, 'message': 'Transcript not ready yet'}), 202

        text = rev_client.get_transcript(order_id)
        _update_job(job_id, {'transcript_text': text, 'rev_status': 'complete'})
        return jsonify({'transcript': text, 'source': 'revai'})

    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/jobs/<job_id>/transcript.docx')
def get_transcript_docx(job_id):
    """Download transcript as a formatted Word document."""
    job = _get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found', 'id': job_id}), 404

    text = job.get('transcript_text', '')
    if not text:
        return jsonify({'error': 'Transcript not available yet'}), 400

    try:
        docx_bytes = _build_docx(job.get('title', ''), text)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500

    safe_title = ''.join(c for c in (job.get('title') or job_id)[:40] if c.isalnum() or c in ' -_').strip() or job_id
    filename = f'{safe_title}.docx'

    return Response(
        docx_bytes,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Length': str(len(docx_bytes)),
        },
    )


@app.route('/api/jobs/<job_id>/screenshot')
def get_screenshot(job_id):
    """Download the page screenshot PNG."""
    screenshot_path = SCREENSHOTS_DIR / f'{job_id}.png'
    if not screenshot_path.exists():
        return jsonify({'error': 'Screenshot not found', 'id': job_id}), 404
    return send_from_directory(str(SCREENSHOTS_DIR), f'{job_id}.png', mimetype='image/png')


@app.route('/api/jobs/<job_id>/video')
def serve_video(job_id):
    video_path = VIDEOS_DIR / f'{job_id}.mp4'
    if not video_path.exists():
        return jsonify({'error': 'Job not found', 'id': job_id}), 404
    return send_from_directory(str(VIDEOS_DIR), f'{job_id}.mp4', mimetype='video/mp4')


@app.route('/api/jobs/<job_id>', methods=['DELETE'])
def delete_job(job_id):
    job = _get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found', 'id': job_id}), 404

    # Remove video file if present
    video_path = VIDEOS_DIR / f'{job_id}.mp4'
    if video_path.exists():
        try:
            video_path.unlink()
        except OSError:
            pass

    # Remove screenshot if present
    screenshot_path = SCREENSHOTS_DIR / f'{job_id}.png'
    if screenshot_path.exists():
        try:
            screenshot_path.unlink()
        except OSError:
            pass

    _delete_job(job_id)
    return jsonify({'deleted': True, 'id': job_id})



@app.route('/api/jobs/<job_id>/analyze-proxy', methods=['POST'])
def analyze_proxy(job_id):
    """Proxy transcript to Promo Analyzer to avoid CORS."""
    import requests as _requests
    job = _get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    
    text = job.get('transcript_text', '')
    if not text:
        return jsonify({'error': 'Transcript not available yet'}), 400
    
    title = job.get('title', '') or job_id
    try:
        docx_bytes = _build_docx(title, text)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    
    safe_title = ''.join(c for c in title[:40] if c.isalnum() or c in ' -_').strip() or job_id
    filename = f'{safe_title}.docx'
    
    analyzer_url = 'https://analyzer.oxfordhub.app/api/analyze'
    try:
        resp = _requests.post(
            analyzer_url,
            files={'file': (filename, docx_bytes, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},
            timeout=120,
            stream=True,
        )
    except Exception as exc:
        return jsonify({'error': f'Failed to reach analyzer: {exc}'}), 502
    
    # Read full response, extract [META]{reviewId}[/META]
    full_text = resp.text
    import re as _re
    meta_match = _re.search(r'\[META\](.*?)\[/META\]', full_text, _re.DOTALL)
    review_id = None
    if meta_match:
        try:
            meta = json.loads(meta_match.group(1))
            review_id = meta.get('reviewId')
        except Exception:
            pass
    
    if not review_id:
        return jsonify({'error': 'Analyzer did not return a review ID', 'raw': full_text[:500]}), 500
    
    _update_job(job_id, {'promo_review_id': review_id})
    return jsonify({'review_id': review_id})

@app.route('/api/jobs/<job_id>/set-review', methods=['POST'])
def set_review(job_id):
    """Persist the Promo Analyzer review ID on a job."""
    job = _get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found', 'id': job_id}), 404
    data = request.get_json(silent=True) or {}
    review_id = (data.get('promo_review_id') or '').strip()
    if not review_id:
        return jsonify({'error': 'promo_review_id is required'}), 400
    updated = _update_job(job_id, {'promo_review_id': review_id})
    return jsonify({'ok': True, 'promo_review_id': review_id})


COOKIES_DIR = DATA_DIR / 'cookies'


@app.route('/api/admin/upload-cookies', methods=['POST'])
def upload_cookies():
    """Upload a Netscape cookies.txt file for a domain."""
    f = request.files.get('file')
    domain = (request.form.get('domain') or '').strip()
    if not f or not domain:
        return jsonify({'error': 'file and domain are required'}), 400
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    dest = COOKIES_DIR / f'{domain}.txt'
    f.save(str(dest))
    return jsonify({'saved': str(dest), 'domain': domain})


if __name__ == '__main__':
    app.run(debug=True, port=5001)
