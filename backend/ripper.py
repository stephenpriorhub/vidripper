"""
Platform detection and yt-dlp video download wrapper.
"""
import re
import subprocess
import tempfile
import shutil
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

PATTERNS = {
    'youtube': [
        r'youtube\.com/embed/([a-zA-Z0-9_-]{11})',
        r'youtu\.be/([a-zA-Z0-9_-]{11})',
        r'[?&]v=([a-zA-Z0-9_-]{11})',
    ],
    'wistia': [
        r'wistia_async_([a-z0-9]+)',
        r"var\s+videoid\s*=\s*['\"]([a-z0-9]+)['\"]",
        r'fast\.wistia\.com/embed/medias/([a-z0-9]+)',
        r'wistia\.com/medias/([a-z0-9]+)',
    ],
    'brightcove': [
        r'data-video-id=["\'](\d+)["\']',
        r'"videoId"\s*:\s*"(\d+)"',
    ],
    'vidalytics': [
        r'vidalytics\.com/embed/([A-Za-z0-9_-]+)',
        r'vidalytics_embed[^"\']*["\']([A-Za-z0-9_-]{8,})["\']',
    ],
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    )
}


def fetch_page(url: str) -> tuple[str, str, str]:
    """
    Fetch a page and return (html, page_title, og_image_url).
    """
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    html = resp.text

    soup = BeautifulSoup(html, 'html.parser')

    # Page title — prefer og:title, fall back to <title>
    og_title = soup.find('meta', property='og:title')
    title = (og_title['content'] if og_title and og_title.get('content')
             else (soup.title.string.strip() if soup.title else url))

    # Thumbnail — og:image
    og_image = soup.find('meta', property='og:image')
    thumbnail = og_image['content'] if og_image and og_image.get('content') else ''

    return html, title, thumbnail


def detect_platform(html: str) -> tuple[str, str]:
    """
    Scan page HTML for embedded video IDs.
    Returns (platform, video_id) or ('unknown', '').
    """
    for platform, patterns in PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                return platform, match.group(1)
    return 'unknown', ''


def _build_yt_dlp_url(platform: str, video_id: str, source_url: str) -> str:
    if platform == 'youtube':
        return f'https://www.youtube.com/watch?v={video_id}'
    elif platform == 'wistia':
        return f'https://fast.wistia.com/medias/{video_id}'
    elif platform == 'brightcove':
        return source_url  # generic extractor on original page
    elif platform == 'vidalytics':
        return f'https://vidalytics.com/embed/{video_id}'
    else:
        return source_url


def download_video(platform: str, video_id: str, source_url: str, dest_path: str) -> str:
    """
    Download video using yt-dlp to dest_path (full .mp4 path).
    Returns the actual output path.
    """
    yt_url = _build_yt_dlp_url(platform, video_id, source_url)

    # Use a temp dir so yt-dlp can write its own filename, then we rename
    tmp_dir = tempfile.mkdtemp()
    try:
        result = subprocess.run(
            [
                'yt-dlp',
                '--no-playlist',
                '--format', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                '--merge-output-format', 'mp4',
                '--output', str(Path(tmp_dir) / 'video.%(ext)s'),
                '--no-warnings',
                yt_url,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f'yt-dlp failed: {result.stderr[:500]}')

        # Find the downloaded file
        files = list(Path(tmp_dir).glob('video.*'))
        if not files:
            raise RuntimeError('yt-dlp produced no output file')

        downloaded = files[0]
        shutil.move(str(downloaded), dest_path)
        return dest_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
