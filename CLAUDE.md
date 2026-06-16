# VideoRipper — Agent Instructions

## What This App Does
Flask + single HTML app that lets users paste a page URL, extracts and downloads embedded video via yt-dlp, submits to Rev AI (automated speech-to-text) for transcription, and displays a job dashboard with thumbnails and transcript access.

## Stack
- **Backend:** Python/Flask, waitress WSGI
- **Frontend:** Single HTML file (no build step)
- **Deployment:** Railway (Nixpacks), domain `vidripper.oxfordhub.app`
- **Auth:** hub-nav.js client-side (same pattern as oxford-pl-dashboard)

## Local Path
`~/GitHub/vidripper/`

## GitHub
`git@github.com:stephenpriorhub/vidripper.git`

## File Structure
```
vidripper/
├── backend/
│   ├── app.py           # Flask routes
│   ├── ripper.py        # Platform detection + yt-dlp wrapper
│   ├── rev_client.py    # Rev AI API client
│   └── serve.py         # Waitress WSGI entry point
├── frontend/
│   └── index.html       # Single-page app
├── data/
│   ├── videos/          # Downloaded .mp4 files
│   ├── screenshots/     # Page screenshots (PNG)
│   ├── cookies/         # Netscape cookies.txt files for gated pages ({domain}.txt)
│   └── manifest.json    # Job records (JSON array)
├── requirements.txt
├── railway.toml
└── CLAUDE.md
```

## Environment Variables (set in Railway)
- `REVAI_ACCESS_TOKEN` — Rev AI Bearer token for speech-to-text API
- `APP_BASE_URL` — Public URL of this app (default: `https://vidripper.oxfordhub.app`), used to construct video download URLs for Rev AI
- `PORT` — Set automatically by Railway (default 8080)

## Supported Platforms
- YouTube
- Wistia
- BrightCove (generic yt-dlp extractor on original page URL)
- Vidalytics

## Data Model
Each job in manifest.json has:
- id, source_url, page_url (original page for screenshot), platform, video_id, title, thumbnail_url
- local_video, rev_order_id, rev_status, rev_submitted_at, rev_transcript_url, transcript_text
- has_screenshot, screenshot_error, promo_review_id
- pipeline_step (queued/fetching_page/fetching_page_rendered/downloading_video/taking_screenshot/submitting_to_rev/done/failed)
- error, created_at

## Rev AI API Notes
- Base: `https://api.rev.ai/speechtotext/v1`
- Auth header: `Authorization: Bearer {REVAI_ACCESS_TOKEN}`
- Flow: POST `/jobs` with `source_url` pointing to the video file → poll `GET /jobs/{id}` for status → `GET /jobs/{id}/transcript` (Accept: text/plain) when complete
- Status values: `in_progress` → `transcribed` (mapped to `complete` in rev_client.py) → `failed`

## hub-nav.js Integration
- `<style>html{visibility:hidden}</style>` is the FIRST element in `<head>`
- `<script src="https://oxfordhub.app/hub-nav.js" data-project-id="vidripper" id="hub-nav">` is the FIRST script in `<body>`
- project-id must match the slug registered in OxfordHub admin

## Bookmarklet
The app generates a `javascript:` bookmarklet that users drag to their bookmarks bar. When clicked on any page:
- Detects Wistia embeds via `[class*="wistia_async_"]` DOM element (string methods only — no regex to avoid bookmarklet URL corruption)
- Detects BrightCove via `[data-video-id][data-account]` DOM attributes
- Falls back to scanning iframe `src` attributes for wistia/brightcove domains
- Passes `?url=<detected_or_page_url>&page_url=<original_page_url>` to VidRipper
- Used for gated pages where server-side fetch cannot see the video embed

## API Routes
| Route | Method | Purpose |
|-------|--------|---------|
| `/` | GET | Serve frontend |
| `/api/ping` | GET | Health check |
| `/api/rip` | POST | Start rip pipeline (body: `{url, page_url?}`) |
| `/api/jobs` | GET | List all jobs |
| `/api/jobs/<id>` | GET | Get single job |
| `/api/jobs/<id>` | DELETE | Delete job + files |
| `/api/jobs/<id>/transcript` | GET | Get/sync transcript from Rev AI |
| `/api/jobs/<id>/transcript.docx` | GET | Download transcript as Word doc |
| `/api/jobs/<id>/screenshot` | GET | Serve page screenshot PNG |
| `/api/jobs/<id>/video` | GET | Serve downloaded MP4 |
| `/api/jobs/<id>/analyze-proxy` | POST | Proxy .docx to analyzer.oxfordhub.app |
| `/api/jobs/<id>/set-review` | POST | Save promo_review_id on job |
| `/api/admin/upload-cookies` | POST | Upload cookies.txt for gated domains |

## Railway Notes
- Builder: Nixpacks
- Start command: `python backend/serve.py`
- Health check: `/api/ping`
- Videos stored in `/data/videos/` — consider Railway volume for persistence across deploys
- manifest.json also needs volume mount or DB-backed storage for production persistence

## Known Limitations
- manifest.json and video files are ephemeral on Railway without a volume mount
- yt-dlp version pinned in requirements.txt — update periodically as sites change extractors
- Rev AI transcript polling is client-side (30s interval) — no server-side webhook yet
