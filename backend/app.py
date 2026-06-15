"""
VideoRipper — Flask backend.
Auth is handled client-side by hub-nav.js; server routes are unauthenticated.
"""
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
DATA_DIR = BASE_DIR / 'data'
VIDEOS_DIR = DATA_DIR / 'videos'
MANIFEST_PATH = DATA_DIR / 'manifest.json'

VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

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


# ── background pipeline ─────────────────────────────────────────────────────

def _run_pipeline(job_id: str, source_url: str) -> None:
    """
    Full rip pipeline: fetch → detect → download → Rev submit.
    Runs in a background thread; updates manifest at each step.
    """
    try:
        # Step 1: fetch page (skip for direct BrightCove player URLs — no useful metadata there)
        _update_job(job_id, {'pipeline_step': 'fetching_page'})
        if 'players.brightcove.net' in source_url:
            html, title, thumbnail = '', source_url, ''
        else:
            html, title, thumbnail = ripper.fetch_page(source_url)
        _update_job(job_id, {'title': title, 'thumbnail_url': thumbnail})

        # Step 2: detect platform
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
            'pipeline_step': 'submitting_to_rev',
        })

        # Step 4: Rev AI — submit video URL directly, no upload needed
        app_base = os.environ.get('APP_BASE_URL', 'https://vidripper.oxfordhub.app')
        video_url = f'{app_base}/api/jobs/{job_id}/video'
        order_id = rev_client.submit_job(video_url, metadata=job_id)
        _update_job(job_id, {
            'rev_order_id': order_id,
            'rev_status': 'in_progress',
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

    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    job = {
        'id': job_id,
        'source_url': url,
        'platform': 'unknown',
        'video_id': '',
        'title': '',
        'thumbnail_url': '',
        'local_video': '',
        'rev_order_id': '',
        'rev_status': 'pending',
        'rev_transcript_url': '',
        'transcript_text': '',
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
        abort(404)
    return jsonify(job)


@app.route('/api/jobs/<job_id>/transcript')
def get_transcript(job_id):
    job = _get_job(job_id)
    if not job:
        abort(404)

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


@app.route('/api/jobs/<job_id>/video')
def serve_video(job_id):
    video_path = VIDEOS_DIR / f'{job_id}.mp4'
    if not video_path.exists():
        abort(404)
    return send_from_directory(str(VIDEOS_DIR), f'{job_id}.mp4', mimetype='video/mp4')


@app.route('/api/jobs/<job_id>', methods=['DELETE'])
def delete_job(job_id):
    job = _get_job(job_id)
    if not job:
        abort(404)

    # Remove video file if present
    video_path = VIDEOS_DIR / f'{job_id}.mp4'
    if video_path.exists():
        try:
            video_path.unlink()
        except OSError:
            pass

    _delete_job(job_id)
    return jsonify({'deleted': True, 'id': job_id})


if __name__ == '__main__':
    app.run(debug=True, port=5001)
