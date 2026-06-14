# VideoRipper — Agent Instructions

## What This App Does
Flask + single HTML app that lets users paste a page URL, extracts and downloads embedded video via yt-dlp, submits to Rev.com for transcription, and displays a job dashboard with thumbnails and transcript access.

## Stack
- **Backend:** Python/Flask, waitress WSGI
- **Frontend:** Single HTML file (no build step)
- **Deployment:** Railway (Nixpacks), domain `vidripper.oxfordhub.app`
- **Auth:** hub-nav.js client-side (same pattern as oxford-pl-dashboard)

## Local Path
`~/Documents/github/vidripper/`

## GitHub
`git@github.com:stephenpriorhub/vidripper.git`

## File Structure
```
vidripper/
├── backend/
│   ├── app.py           # Flask routes
│   ├── ripper.py        # Platform detection + yt-dlp wrapper
│   ├── rev_client.py    # Rev.com API v2 client
│   └── serve.py         # Waitress WSGI entry point
├── frontend/
│   └── index.html       # Single-page app
├── data/
│   ├── videos/          # Downloaded .mp4 files
│   └── manifest.json    # Job records (JSON array)
├── requirements.txt
├── railway.toml
└── CLAUDE.md
```

## Environment Variables (set in Railway)
- `REV_API_KEY` — Rev.com API v2 key
- `PORT` — set automatically by Railway (default 8080)

## Supported Platforms
- YouTube
- Wistia
- BrightCove (generic yt-dlp extractor on original page URL)
- Vidalytics

## Data Model
Each job in manifest.json has:
- id, source_url, platform, video_id, title, thumbnail_url
- local_video, rev_order_id, rev_status, transcript_text
- pipeline_step (queued/fetching_page/downloading_video/submitting_to_rev/done/failed)
- error, created_at

## Rev.com API Notes
- Base: `https://www.rev.com/api/v2`
- Auth header: `Authorization: Rev {key}`
- Flow: upload file to /media → get media_url → POST /orders → poll /orders/{id} → GET /orders/{id}/transcript

## hub-nav.js Integration
- `<style>html{visibility:hidden}</style>` is the FIRST element in `<head>`
- `<script src="https://oxfordhub.app/hub-nav.js" data-project-id="vidripper" id="hub-nav">` is the FIRST script in `<body>`
- project-id must match the slug registered in OxfordHub admin

## Railway Notes
- Builder: Nixpacks
- Start command: `python backend/serve.py`
- Health check: `/api/ping`
- Videos stored in `/data/videos/` — consider Railway volume for persistence across deploys
- manifest.json also needs volume mount or DB-backed storage for production persistence

## Known Limitations
- manifest.json and video files are ephemeral on Railway without a volume mount
- yt-dlp version pinned in requirements.txt — update periodically as sites change extractors
- Rev.com transcript polling is client-side (30s interval) — no server-side webhook yet
