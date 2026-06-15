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
        r'data-video-id=["\'](\d{7,})["\']',
        r'"videoId"\s*:\s*"(\d{7,})"',
        r'videoId=(\d{7,})',
    ],
    'vidalytics': [
        r'vidalytics\.com/embed/([A-Za-z0-9_-]+)',
        r'vidalytics_embed[^"\']*["\']([A-Za-z0-9_-]{8,})["\']',
    ],
}

# BrightCove also needs account ID + player ID to build the player URL
BC_ACCOUNT_PATTERNS = [
    r'data-account=["\'](\d+)["\']',
    r'accountId["\s:]+["\'](\d+)["\']',
    r'players\.brightcove\.net/(\d+)/',
]
BC_PLAYER_PATTERNS = [
    r'data-player=["\']([A-Za-z0-9_-]+)["\']',
    r'players\.brightcove\.net/\d+/([A-Za-z0-9_-]+)_default',
]

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    )
}


def fetch_page_rendered(url: str) -> tuple[str, str, str, dict]:
    """
    Render a page in headless Chromium via Playwright.
    Returns (html, title, og_image_url, brightcove_attrs).
    brightcove_attrs may contain {video_id, account_id, player_id} if found directly in DOM.
    """
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
        page = context.new_page()
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=30000)
        except Exception:
            pass  # timeout is fine — grab whatever rendered

        # Wait for BrightCove video element (video-js tag or any element with data-video-id + data-account)
        bc_attrs = {}
        try:
            page.wait_for_selector('[data-video-id][data-account]', timeout=20000)
            bc_attrs = page.evaluate("""() => {
                const el = document.querySelector('[data-video-id][data-account]')
                        || document.querySelector('video-js[data-video-id]');
                if (!el) return {};
                return {
                    video_id: el.getAttribute('data-video-id') || '',
                    account_id: el.getAttribute('data-account') || '',
                    player_id: el.getAttribute('data-player') || '',
                };
            }""")
        except Exception:
            try:
                page.wait_for_selector('[data-video-id]', timeout=5000)
            except Exception:
                pass

        html = page.content()
        title = page.title() or url
        thumbnail = page.evaluate(
            "document.querySelector('meta[property=\"og:image\"]')?.content || ''"
        )
        browser.close()
    return html, title, thumbnail, bc_attrs


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
             else ((soup.title.string if soup.title else None) or '').strip() or url)

    # Thumbnail — og:image
    og_image = soup.find('meta', property='og:image')
    thumbnail = og_image['content'] if og_image and og_image.get('content') else ''

    return html, title, thumbnail


def detect_platform(html: str, source_url: str = '') -> tuple[str, str, dict]:
    """
    Scan page HTML (and source URL) for embedded video IDs.
    Returns (platform, video_id, extra) where extra holds platform-specific metadata.
    Raises ValueError for pages where the platform is detected but video ID requires
    browser rendering (e.g. Angular SPAs) — caller should surface a helpful message.
    """
    # Direct BrightCove player URL pasted as input — handle immediately
    bc_direct = re.match(
        r'https?://players\.brightcove\.net/(\d+)/([A-Za-z0-9_-]+)_default.*[?&]videoId=(\d+)',
        source_url or '',
    )
    if bc_direct:
        return 'brightcove', bc_direct.group(3), {
            'bc_account_id': bc_direct.group(1),
            'bc_player_id': bc_direct.group(2),
            'bc_direct_url': source_url,
        }

    for platform, patterns in PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                extra = {}
                if platform == 'brightcove':
                    for p in BC_ACCOUNT_PATTERNS:
                        m = re.search(p, html, re.IGNORECASE)
                        if m:
                            extra['bc_account_id'] = m.group(1)
                            break
                    for p in BC_PLAYER_PATTERNS:
                        m = re.search(p, html, re.IGNORECASE)
                        if m:
                            extra['bc_player_id'] = m.group(1)
                            break
                    # If we found a video ID but no account ID, the page likely needs
                    # JS rendering to expose the full BrightCove attributes — return
                    # unknown so the caller can retry with a headless browser.
                    if not extra.get('bc_account_id'):
                        return 'unknown', '', {}
                return platform, match.group(1), extra

    return 'unknown', '', {}


def _build_yt_dlp_url(platform: str, video_id: str, source_url: str, extra: dict = None) -> str:
    extra = extra or {}
    if platform == 'youtube':
        return f'https://www.youtube.com/watch?v={video_id}'
    elif platform == 'wistia':
        return f'https://fast.wistia.com/medias/{video_id}'
    elif platform == 'brightcove':
        if extra.get('bc_direct_url'):
            return extra['bc_direct_url']
        account = extra.get('bc_account_id', '')
        player = extra.get('bc_player_id', '')
        if account and video_id:
            if player:
                # Named player: append _default suffix as BrightCove requires
                player_path = f'{player}_default'
            else:
                # No player ID found — use BrightCove's bare default path
                player_path = 'default'
            return (f'https://players.brightcove.net/{account}/{player_path}'
                    f'/index.html?videoId={video_id}')
        return source_url
    elif platform == 'vidalytics':
        return f'https://vidalytics.com/embed/{video_id}'
    else:
        return source_url


COOKIES_DIR = Path(__file__).resolve().parent.parent / 'data' / 'cookies'


def _cookies_path(platform: str) -> str | None:
    """Return path to cookies.txt for this platform if it exists."""
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    specific = COOKIES_DIR / f'{platform}.txt'
    generic = COOKIES_DIR / 'cookies.txt'
    if specific.exists():
        return str(specific)
    if generic.exists():
        return str(generic)
    return None


def download_video(platform: str, video_id: str, source_url: str, dest_path: str, extra: dict = None) -> str:
    """
    Download video using yt-dlp to dest_path (full .mp4 path).
    Returns the actual output path.
    """
    yt_url = _build_yt_dlp_url(platform, video_id, source_url, extra)

    cmd = [
        'yt-dlp',
        '--no-playlist',
        '--format', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
        '--merge-output-format', 'mp4',
        '--output', '',  # filled below after tmp_dir is created
        '--no-warnings',
    ]

    cookies = _cookies_path(platform)
    if cookies:
        cmd += ['--cookies', cookies]

    # Use a temp dir so yt-dlp can write its own filename, then we rename
    tmp_dir = tempfile.mkdtemp()
    cmd[cmd.index('--output') + 1] = str(Path(tmp_dir) / 'video.%(ext)s')
    cmd.append(yt_url)

    try:
        result = subprocess.run(
            cmd,
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
