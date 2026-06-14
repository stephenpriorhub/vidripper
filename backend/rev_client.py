"""
Rev.com API v2 client.

Flow:
  1. upload_media(file_path) -> media_url
  2. submit_order(media_url, filename) -> order_id
  3. get_order_status(order_id) -> status string
  4. get_transcript(order_id) -> transcript text (when status == 'complete')
"""
import os
import requests

BASE_URL = 'https://www.rev.com/api/v2'


def _headers() -> dict:
    key = os.environ.get('REV_API_KEY', '')
    return {
        'Authorization': f'Rev {key}',
        'Accept': 'application/json',
    }


def upload_media(file_path: str) -> str:
    """
    Upload a local file to Rev media endpoint.
    Returns the media_url Rev assigned.
    """
    url = f'{BASE_URL}/media'
    filename = os.path.basename(file_path)
    with open(file_path, 'rb') as fh:
        resp = requests.post(
            url,
            headers=_headers(),
            files={'file': (filename, fh, 'video/mp4')},
            timeout=300,
        )
    resp.raise_for_status()
    data = resp.json()
    # Rev returns {"value": "https://..."}
    media_url = data.get('value') or data.get('media_url') or data.get('uri')
    if not media_url:
        raise RuntimeError(f'Rev upload: unexpected response {data}')
    return media_url


def submit_order(media_url: str, filename: str = 'video.mp4') -> str:
    """
    Submit a transcription order.
    Returns the Rev order_id.
    """
    url = f'{BASE_URL}/orders'
    payload = {
        'media': [{'url': media_url, 'filename': filename}],
        'transcription_options': {'verbatim': False},
    }
    resp = requests.post(
        url,
        headers={**_headers(), 'Content-Type': 'application/json'},
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    order_id = data.get('order_number') or data.get('id') or data.get('order_id')
    if not order_id:
        raise RuntimeError(f'Rev order: unexpected response {data}')
    return str(order_id)


def get_order_status(order_id: str) -> str:
    """
    Poll order status.
    Returns one of: 'pending', 'in_progress', 'complete', 'failed', 'cancelled'.
    """
    url = f'{BASE_URL}/orders/{order_id}'
    resp = requests.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    status = (data.get('status') or '').lower()
    # Normalize Rev statuses to our internal set
    STATUS_MAP = {
        'finding_reviewers': 'in_progress',
        'in_progress': 'in_progress',
        'transcribed': 'in_progress',
        'complete': 'complete',
        'completed': 'complete',
        'cancelled': 'failed',
        'failed': 'failed',
    }
    return STATUS_MAP.get(status, 'pending')


def get_transcript(order_id: str) -> str:
    """
    Retrieve plain-text transcript for a completed order.
    """
    url = f'{BASE_URL}/orders/{order_id}/transcript'
    resp = requests.get(
        url,
        headers={**_headers(), 'Accept': 'text/plain'},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.text
