"""
Google Drive archival helper for VidRipper.

Videos older than the retention window are uploaded to a Shared Drive folder so
the local Railway volume can reclaim the space, then streamed back on demand so
playback keeps working after the local .mp4 is deleted.

Auth reuses the existing OxfordHub service account (GCP project
primeval-rain-501214-e3) via GOOGLE_SERVICE_ACCOUNT_JSON. Files MUST land in a
Shared Drive (GDRIVE_ARCHIVE_FOLDER_ID) — a service account has no personal
Drive quota, so uploading into a plain My Drive folder would fail.

All google-api imports are lazy (inside functions) so `import gdrive` never
fails when the libraries aren't installed (e.g. local dev).
"""
import json
import os

SCOPES = ['https://www.googleapis.com/auth/drive']


def is_configured() -> bool:
    """True when both the service-account credential and target folder are set."""
    return bool(
        os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
        and os.environ.get('GDRIVE_ARCHIVE_FOLDER_ID')
    )


def _credentials():
    from google.oauth2 import service_account
    info = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def upload_video(local_path: str, filename: str) -> dict:
    """Resumable-upload a local file into the configured Shared Drive folder.
    Returns {'file_id', 'web_view_link'}. Raises on failure."""
    import googleapiclient.discovery
    from googleapiclient.http import MediaFileUpload

    creds = _credentials()
    svc = googleapiclient.discovery.build(
        'drive', 'v3', credentials=creds, cache_discovery=False
    )
    meta = {
        'name': filename,
        'parents': [os.environ['GDRIVE_ARCHIVE_FOLDER_ID']],
    }
    media = MediaFileUpload(local_path, mimetype='video/mp4', resumable=True)
    f = svc.files().create(
        body=meta,
        media_body=media,
        fields='id,webViewLink',
        supportsAllDrives=True,  # required for Shared Drives
    ).execute()
    return {'file_id': f['id'], 'web_view_link': f.get('webViewLink', '')}


def open_stream(file_id: str, range_header: str | None = None):
    """Open a streaming GET against the Drive media endpoint, forwarding the
    client's Range header so the browser player can seek. Returns the live
    `requests` Response (caller streams .iter_content and forwards status +
    Content-Range/Length/Type). Drive honours Range on media downloads."""
    from google.auth.transport.requests import AuthorizedSession
    sess = AuthorizedSession(_credentials())
    url = (
        f'https://www.googleapis.com/drive/v3/files/{file_id}'
        '?alt=media&supportsAllDrives=true'
    )
    headers = {}
    if range_header:
        headers['Range'] = range_header
    return sess.get(url, headers=headers, stream=True)


def delete_file(file_id: str) -> None:
    """Delete a file from Drive (used when a job is deleted). Best-effort."""
    import googleapiclient.discovery
    creds = _credentials()
    svc = googleapiclient.discovery.build(
        'drive', 'v3', credentials=creds, cache_discovery=False
    )
    svc.files().delete(fileId=file_id, supportsAllDrives=True).execute()
