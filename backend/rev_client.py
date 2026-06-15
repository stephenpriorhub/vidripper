"""
Rev AI speech-to-text client (api.rev.ai).

Flow:
  1. submit_job(video_url)     -> job_id
  2. get_job_status(job_id)    -> 'in_progress' | 'complete' | 'failed'
  3. get_transcript(job_id)    -> plain text
"""
import os
import requests

BASE_URL = 'https://api.rev.ai/speechtotext/v1'


def _headers(accept='application/json') -> dict:
    token = os.environ.get('REVAI_ACCESS_TOKEN', '')
    return {
        'Authorization': f'Bearer {token}',
        'Accept': accept,
    }


def submit_job(video_url: str, metadata: str = '') -> str:
    """
    Submit a transcription job by URL.
    Rev AI fetches the video directly — no upload needed.
    Returns the job_id.
    """
    payload = {'media_url': video_url}
    if metadata:
        payload['metadata'] = metadata

    resp = requests.post(
        f'{BASE_URL}/jobs',
        headers={**_headers(), 'Content-Type': 'application/json'},
        json=payload,
        timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(f'Rev AI job failed ({resp.status_code}): {resp.text[:300]}')
    return resp.json()['id']


def get_job_status(job_id: str) -> str:
    """
    Poll job status.
    Returns one of: 'in_progress' | 'complete' | 'failed'.
    """
    resp = requests.get(
        f'{BASE_URL}/jobs/{job_id}',
        headers=_headers(),
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f'Rev AI status failed ({resp.status_code}): {resp.text[:200]}')
    status = resp.json().get('status', '')
    if status == 'transcribed':
        return 'complete'
    if status == 'failed':
        return 'failed'
    return 'in_progress'


def get_transcript(job_id: str) -> str:
    """
    Retrieve plain-text transcript for a completed job.
    """
    resp = requests.get(
        f'{BASE_URL}/jobs/{job_id}/transcript',
        headers=_headers(accept='text/plain'),
        timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(f'Rev AI transcript failed ({resp.status_code}): {resp.text[:200]}')
    return resp.text
