"""
Platform detection and yt-dlp video download wrapper.
"""
import json
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
        r'vidalytics_embed_([A-Za-z0-9_-]+)',
        r'fast\.vidalytics\.com/embeds/[A-Za-z0-9_-]+/([A-Za-z0-9_-]+)',
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


def resolve_vidalytics_stream(account_id: str, embed_id: str) -> str:
    """
    Resolve a Vidalytics embed to its playable HLS master manifest URL.

    Vidalytics does NOT serve a scrapable video page at
    fast.vidalytics.com/embeds/{account}/{embed}/ — that path is Cloudflare-
    protected and returns 403 to yt-dlp's generic extractor (this was the root
    cause of the "[generic] Unable to download webpage: HTTP Error 403" failure).

    The real media lives at a DIFFERENT id than the embed id:
        https://fast.vidalytics.com/video/{account}/{videoId}/{a}/{b}__FFMPEG/stream.m3u8
    The {videoId} and full stream path are baked into the embed's loader.min.js,
    which IS publicly fetchable (returns 200). An embed can reference several
    videos (main VSL + short intro/CTA loops); we pick the one with the longest
    duration, which is reliably the main video.

    The resolved stream.m3u8 CDN URL needs NO special headers (200 with a bare
    request) and yt-dlp's generic extractor parses it into an adaptive-bitrate
    HLS ladder (up to 1080p) with no further work.

    Returns the master manifest URL, or '' if it can't be resolved.
    """
    loader_url = (
        f'https://fast.vidalytics.com/embeds/{account_id}/{embed_id}/loader.min.js'
    )
    try:
        resp = requests.get(loader_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception:
        return ''

    # loader.min.js escapes slashes as \/ — unescape so URLs match cleanly.
    src = resp.text.replace('\\/', '/')

    # Every playable rendition is referenced by a full stream.m3u8 path keyed by
    # the media videoId (distinct from the embed id).
    stream_re = re.compile(
        r'fast\.vidalytics\.com/video/'
        + re.escape(account_id)
        + r'/([A-Za-z0-9_-]+)/\d+/\d+__FFMPEG/stream\.m3u8'
    )
    streams: dict[str, str] = {}
    for m in stream_re.finditer(src):
        streams.setdefault(m.group(1), 'https://' + m.group(0))
    if not streams:
        return ''

    # Pick the main video: the one whose nearby config carries the longest
    # "duration". Short intro/CTA loops are only a few seconds.
    best_id, best_dur = None, -1.0
    for dm in re.finditer(r'"duration"\s*:\s*([\d.]+)', src):
        try:
            dur = float(dm.group(1))
        except ValueError:
            continue
        ctx = src[max(0, dm.start() - 400):dm.start() + 100]
        for vid in re.findall(
            r'/video/' + re.escape(account_id) + r'/([A-Za-z0-9_-]+)/', ctx
        ):
            if vid in streams and dur > best_dur:
                best_dur, best_id = dur, vid

    if best_id is None:
        # No duration hint — fall back to the first stream found.
        best_id = next(iter(streams))
    return streams[best_id]


def resolve_vidalytics_poster(account_id: str, embed_id: str) -> str:
    """
    Resolve a Vidalytics embed to its pre-play poster/thumbnail image URL.

    On many VSL promos the marketing HEADLINE is baked into this thumbnail
    image (not the page text), and the promo page itself is Cloudflare-gated so
    it can't be screenshotted server-side. The poster, however, lives on the
    Vidalytics CDN and is reachable. Vidalytics stores it in the embed config
    inside loader.min.js at `ui.thumbnail.default.source`.

    Returns the image URL, or '' if it can't be resolved.
    """
    loader_url = (
        f'https://fast.vidalytics.com/embeds/{account_id}/{embed_id}/loader.min.js'
    )
    try:
        resp = requests.get(loader_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception:
        return ''

    src = resp.text.replace('\\/', '/')  # loader escapes slashes as \/

    # Primary: the default (desktop) thumbnail source in the ui.thumbnail block.
    m = re.search(
        r'"thumbnail"\s*:\s*\{.*?"default"\s*:\s*\{\s*"source"\s*:\s*"([^"]+)"',
        src,
        re.DOTALL,
    )
    if not m:
        # Looser: any "default":{"source":"<image>"} pointing at an image file.
        m = re.search(
            r'"default"\s*:\s*\{\s*"source"\s*:\s*"(https?://[^"]+?\.(?:png|jpe?g|webp)[^"]*)"',
            src,
            re.IGNORECASE,
        )
    if not m:
        # Last resort: first image URL on the Vidalytics CDN in the config.
        m = re.search(
            r'"(https?://[^"]*vidalytics\.com/[^"]+?\.(?:png|jpe?g|webp)[^"]*)"',
            src,
            re.IGNORECASE,
        )
    return m.group(1) if m else ''


def download_image(url: str, dest_path: str) -> bool:
    """Download an image URL to dest_path. Best-effort — returns True on success."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.content
        if not data:
            return False
        with open(dest_path, 'wb') as f:
            f.write(data)
        return True
    except Exception:
        return False


def extract_poster_frame(video_path: str, dest_path: str) -> bool:
    """Grab a still frame from a downloaded video via ffmpeg as a poster image.

    Fallback for when a platform-native thumbnail URL isn't available: VSLs
    almost always open on a branded headline card, so an early frame usually
    carries the headline. Seeks ~1s in to skip any fade-in; falls back to the
    very first frame. Best-effort — returns True only if a non-empty PNG lands.
    """
    for seek in ('1', '0'):
        try:
            subprocess.run(
                ['ffmpeg', '-y', '-ss', seek, '-i', video_path,
                 '-frames:v', '1', '-q:v', '2', dest_path],
                capture_output=True, timeout=60,
            )
        except Exception:
            continue
        try:
            if Path(dest_path).is_file() and Path(dest_path).stat().st_size > 0:
                return True
        except Exception:
            pass
    return False


def _load_cookies_for_playwright(url: str) -> list:
    """
    Load a Netscape cookies.txt file and convert to Playwright cookie dicts.
    Tries domain-specific file first (e.g. investorplace.txt), then cookies.txt.
    """
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.lstrip('www.')
    # strip subdomains to get root domain, e.g. secure.investorplace.com → investorplace.com
    parts = domain.split('.')
    root = '.'.join(parts[-2:]) if len(parts) >= 2 else domain

    candidates = [
        COOKIES_DIR / f'{root}.txt',
        COOKIES_DIR / f'{domain}.txt',
        COOKIES_DIR / 'cookies.txt',
    ]
    cookies_path = next((p for p in candidates if p.exists()), None)
    if not cookies_path:
        return []

    cookies = []
    with open(cookies_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) < 7:
                continue
            domain_val, _, path, secure, expires, name, value = parts[:7]
            cookies.append({
                'name': name,
                'value': value,
                'domain': domain_val,
                'path': path,
                'secure': secure.upper() == 'TRUE',
                'sameSite': 'None',
            })
    return cookies


def fetch_page_rendered(url: str) -> tuple[str, str, str, dict]:
    """
    Render a page in headless Chromium via Playwright.
    Returns (html, title, og_image_url, brightcove_attrs).
    brightcove_attrs may contain {video_id, account_id, player_id} if found directly in DOM.
    Loads cookies from data/cookies/ so gated pages render correctly.
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
        pw_cookies = _load_cookies_for_playwright(url)
        if pw_cookies:
            context.add_cookies(pw_cookies)
        page = context.new_page()

        # Apply stealth patches to avoid bot/WAF detection
        try:
            from playwright_stealth import stealth_sync
            stealth_sync(page)
        except ImportError:
            pass

        try:
            page.goto(url, wait_until='domcontentloaded', timeout=30000)
        except Exception:
            pass  # timeout is fine — grab whatever rendered

        # Give JS frameworks extra time to bootstrap and make API calls
        try:
            page.wait_for_load_state('networkidle', timeout=10000)
        except Exception:
            pass

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
                if platform == 'vidalytics':
                    acc_match = re.search(
                        r'fast\.vidalytics\.com/embeds/([A-Za-z0-9_-]+)/' + re.escape(match.group(1)),
                        html,
                    )
                    if acc_match:
                        extra['vidalytics_account_id'] = acc_match.group(1)
                        # Resolve the real HLS manifest now (via loader.min.js) and
                        # cache it so probe + download reuse the same URL. The embed
                        # page itself is Cloudflare-protected (403), so we must NOT
                        # hand that URL to yt-dlp.
                        stream_url = resolve_vidalytics_stream(
                            acc_match.group(1), match.group(1)
                        )
                        if stream_url:
                            extra['vidalytics_stream_url'] = stream_url
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
        account_id = extra.get('vidalytics_account_id', '')
        # A resolved stream.m3u8 may already be cached on `extra` (set during
        # detection) — prefer it so we don't hit the network twice.
        stream_url = extra.get('vidalytics_stream_url', '')
        if not stream_url and account_id and video_id:
            stream_url = resolve_vidalytics_stream(account_id, video_id)
        if stream_url:
            return stream_url
        # Last resort: the Cloudflare-protected embed page. yt-dlp will very
        # likely 403 here, but it's better than pasting nothing.
        if account_id:
            return f'https://fast.vidalytics.com/embeds/{account_id}/{video_id}/'
        return source_url  # fallback to original page
    else:
        # Unknown platform — pass source_url directly so yt-dlp's generic extractor
        # can attempt extraction. Works for: foxbusiness.com, foxnews.com, and any
        # other site with a direct MP4 embed that yt-dlp's generic extractor handles.
        return source_url


_volume = Path('/data')
COOKIES_DIR = (_volume if _volume.is_dir() else Path(__file__).resolve().parent.parent / 'data') / 'cookies'


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


# ── publisher / title helpers ────────────────────────────────────────────────

# Known publisher domains → friendly display names.
PUBLISHER_MAP = {
    'brownstoneresearch.com': 'Brownstone',
    'investorplace.com': 'InvestorPlace',
    'paradigmpressgroup.com': 'Paradigm',
    'monumenttradersalliance.com': 'MTA',
    'stansberryresearch.com': 'Stansberry',
    'oxfordclub.com': 'Oxford Club',
    'agorafinancial.com': 'Agora',
    'banyanhill.com': 'Banyan Hill',
    'legacyresearch.com': 'Legacy Research',
    'rogueeconomics.com': 'Rogue Economics',
    'jeffclarktrader.com': 'Jeff Clark',
    'dailyreckoning.com': 'Daily Reckoning',
}

# Subdomain prefixes to strip when deriving a publisher label from a domain.
_DOMAIN_PREFIXES = ('www.', 'secure.', 'mb.', 'pro.', 'view.', 'go.', 'app.')


def publisher_from_url(url: str) -> str:
    """
    Derive a friendly publisher name from a URL's domain.
    Maps known domains; otherwise strips common prefixes and Title-cases the root.
    """
    domain = (urlparse(url).netloc or '').lower()
    for prefix in _DOMAIN_PREFIXES:
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    # Collapse to the registrable root (last two labels) for matching.
    parts = domain.split('.')
    root = '.'.join(parts[-2:]) if len(parts) >= 2 else domain
    if root in PUBLISHER_MAP:
        return PUBLISHER_MAP[root]
    if domain in PUBLISHER_MAP:
        return PUBLISHER_MAP[domain]
    # Fallback: Title-case the root label (the bit before the TLD).
    label = root.split('.')[0] if root else domain
    return label.replace('-', ' ').replace('_', ' ').title() if label else 'Video'


def probe_video_info(platform: str, video_id: str, source_url: str, extra: dict = None) -> dict:
    """
    Run `yt-dlp --dump-single-json` to obtain video metadata without downloading.
    Returns a dict (possibly empty) with keys like title / alt_title / display_id /
    thumbnail. Never raises — metadata is best-effort.
    """
    yt_url = _build_yt_dlp_url(platform, video_id, source_url, extra)
    cmd = [
        'yt-dlp',
        '--no-playlist',
        '--skip-download',
        '--dump-single-json',
        '--no-warnings',
    ]
    cookies = _cookies_path(platform)
    if cookies:
        cmd += ['--cookies', cookies]
    cmd.append(yt_url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        info = json.loads(result.stdout)
        if isinstance(info, dict) and info.get('entries'):
            # Playlist-shaped result — use the first entry.
            entries = [e for e in info['entries'] if isinstance(e, dict)]
            if entries:
                info = entries[0]
        return info if isinstance(info, dict) else {}
    except Exception:
        return {}


def video_title_from_info(info: dict) -> str:
    """
    Extract the user-created video title from a yt-dlp info dict.
    Prefers `title`, then `alt_title`, then `display_id`. Ignores titles that are
    just the numeric/ID display_id or empty.
    """
    if not info:
        return ''
    for key in ('title', 'alt_title'):
        val = (info.get(key) or '').strip()
        if val and val.lower() not in ('na', 'none'):
            return val
    return (info.get('display_id') or '').strip()


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
        # Remux to a real MP4 container. HLS sources (Brightcove) deliver MPEG-TS
        # segments; without this, a single combined stream is saved as raw .ts
        # under a .mp4 name and won't open in players. Requires ffmpeg.
        '--remux-video', 'mp4',
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
            # Long VSLs (e.g. ~58-min Vidalytics promos) take several minutes to
            # download over HLS + remux to MP4. 20 min ceiling so they complete.
            timeout=1200,
        )
        if result.returncode != 0:
            err = result.stderr[:500]
            if 'Unsupported URL' in err:
                if platform == 'unknown':
                    raise RuntimeError(
                        'Could not detect video platform. yt-dlp also could not extract a video '
                        'from this URL. Try the bookmarklet on this page, or paste the direct '
                        'video URL.'
                    )
                raise RuntimeError(
                    'Could not extract video — the page requires login or is not publicly accessible. '
                    'For InvestorPlace BrightCove videos: open Chrome DevTools → Network tab → '
                    'reload the page → filter by "brightcove.net" → copy the players.brightcove.net '
                    'URL and paste it directly into VidRipper. '
                    f'(yt-dlp: {err[:200]})'
                )
            raise RuntimeError(f'yt-dlp failed: {err}')

        # Find the downloaded file — prefer the remuxed .mp4 if an intermediate
        # (e.g. .ts) is also left behind.
        files = list(Path(tmp_dir).glob('video.*'))
        if not files:
            raise RuntimeError('yt-dlp produced no output file')

        mp4s = [f for f in files if f.suffix.lower() == '.mp4']
        downloaded = mp4s[0] if mp4s else files[0]
        shutil.move(str(downloaded), dest_path)
        return dest_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
