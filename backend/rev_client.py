"""
Rev.com API v1 client.

Flow:
  1. submit_input(video_url, filename) -> input_id  (Rev fetches video from our URL)
  2. submit_order(input_id, filename)  -> order_number
  3. get_order_status(order_number)    -> 'pending' | 'in_progress' | 'complete' | 'failed'
  4. get_transcript(order_number)      -> plain text  (from transcript attachment)
"""
import os
import requests

BASE_URL = 'https://api.rev.com/api/v1'


def _headers() -> dict:
    client_key = os.environ.get('REV_CLIENT_KEY', '')
    user_key = os.environ.get('REV_USER_KEY', '')
    return {
        'Authorization': f'Rev {client_key}:{user_key}',
        'Accept': 'application/json',
    }


def submit_input(video_url: str, filename: str = 'video.mp4') -> str:
    """
    Tell Rev to fetch a video from video_url.
    Returns the input_id (URI) to reference in the order.
    """
    resp = requests.post(
        f'{BASE_URL}/inputs',
        headers={**_headers(), 'Content-Type': 'application/json'},
        json={'url': video_url, 'filename': filename},
        timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(f'Rev input failed ({resp.status_code}): {resp.text[:300]}')
    # Rev returns the input URI in the Location header
    location = resp.headers.get('Location') or resp.headers.get('location', '')
    if not location:
        raise RuntimeError(f'Rev input: no Location header. Response: {resp.text[:200]}')
    # location is like /api/v1/inputs/abc123 — extract just the URI portion
    return location


def submit_order(input_uri: str, filename: str = 'video.mp4') -> str:
    """
    Place an automated transcription order for a previously submitted input.
    Returns the Rev order_number.
    """
    payload = {
        'automated_transcription': {
            'inputs': [{'uri': input_uri}],
        }
    }
    resp = requests.post(
        f'{BASE_URL}/orders',
        headers={**_headers(), 'Content-Type': 'application/json'},
        json=payload,
        timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(f'Rev order failed ({resp.status_code}): {resp.text[:300]}')
    location = resp.headers.get('Location') or resp.headers.get('location', '')
    # Location is like /api/v1/orders/TCxxxxxxxxxx
    order_number = location.rstrip('/').split('/')[-1] if location else ''
    if not order_number:
        data = resp.json() if resp.content else {}
        order_number = data.get('order_number', '')
    if not order_number:
        raise RuntimeError(f'Rev order: could not get order number. Response: {resp.text[:200]}')
    return order_number


def get_order_status(order_number: str) -> str:
    """
    Poll order status.
    Returns one of: 'pending', 'in_progress', 'complete', 'failed'.
    """
    resp = requests.get(
        f'{BASE_URL}/orders/{order_number}',
        headers=_headers(),
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f'Rev status failed ({resp.status_code}): {resp.text[:200]}')
    status = (resp.json().get('status') or '').lower()
    STATUS_MAP = {
        'in_progress': 'in_progress',
        'finding_reviewers': 'in_progress',
        'transcribed': 'in_progress',
        'complete': 'complete',
        'completed': 'complete',
        'cancelled': 'failed',
        'failed': 'failed',
    }
    return STATUS_MAP.get(status, 'pending')


def get_transcript(order_number: str) -> str:
    """
    Retrieve plain-text transcript for a completed order.
    Finds the transcript attachment and fetches its text content.
    """
    resp = requests.get(
        f'{BASE_URL}/orders/{order_number}',
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    order = resp.json()

    # Find the transcript attachment
    for attachment in order.get('attachments', []):
        if attachment.get('kind') == 'transcript':
            for link in attachment.get('links', []):
                if link.get('rel') == 'content':
                    href = link['href']
                    # href may be relative like /api/v1/attachments/xxx/content
                    if href.startswith('/'):
                        href = f'https://api.rev.com{href}'
                    txt_resp = requests.get(
                        href,
                        headers={**_headers(), 'Accept': 'text/plain'},
                        timeout=60,
                    )
                    txt_resp.raise_for_status()
                    return txt_resp.text

    raise RuntimeError(f'No transcript attachment found for order {order_number}')
