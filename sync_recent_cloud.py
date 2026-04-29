"""
Taskade -> Google Drive sync (recent days only, cloud-safe version).
Reads all secrets from environment variables.
Designed to run on GitHub Actions.

Logging policy:
- No project names or filenames are ever printed.
- Only counts and (on failure) exception type names are logged.
- Safe for public repository.
"""

import sys
import time
import os
import re
import json
from collections import Counter
from datetime import datetime, timedelta

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

# ============================================================
# Read secrets from environment variables
# ============================================================
TASKADE_TOKEN = os.environ.get("TASKADE_TOKEN")
TASKADE_SUBSPACE_ID = os.environ.get("TASKADE_SUBSPACE_ID")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")
GOOGLE_TOKEN_JSON = os.environ.get("GOOGLE_TOKEN_JSON")

TASKADE_API = "https://www.taskade.com/api/v1"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

RECENT_DAYS = 5

# Retry config for transient Drive errors
DRIVE_RETRY_STATUSES = (429, 500, 502, 503, 504)
DRIVE_RETRIES = 4
DRIVE_RETRY_BASE = 1.0


def drive_call(fn):
    """Execute a Drive API call with exponential backoff on transient errors."""
    for attempt in range(DRIVE_RETRIES + 1):
        try:
            return fn()
        except HttpError as e:
            status = getattr(e, "status_code", None)
            if status is None:
                resp = getattr(e, "resp", None)
                status = getattr(resp, "status", None)
            if status in DRIVE_RETRY_STATUSES and attempt < DRIVE_RETRIES:
                time.sleep(DRIVE_RETRY_BASE * (2 ** attempt))
                continue
            raise


# ============================================================
# Taskade API
# ============================================================
taskade_headers = {
    "Authorization": f"Bearer {TASKADE_TOKEN}",
    "Content-Type": "application/json",
}


def taskade_get(endpoint, params=None):
    url = f"{TASKADE_API}{endpoint}"
    for attempt in range(8):
        try:
            r = requests.get(
                url, headers=taskade_headers, params=params, timeout=30
            )
        except requests.exceptions.RequestException:
            time.sleep(min(2 ** attempt, 60))
            continue
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 5))
            time.sleep(wait)
            continue
        if r.status_code in (500, 502, 503, 504):
            time.sleep(min(2 ** attempt, 60))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Failed after retries: {endpoint}")


def items_of(response):
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        if "items" in response:
            return response["items"]
        if "item" in response:
            return [response["item"]]
    return []


def project_to_markdown(project, tasks):
    name = project.get("name") or project.get("title") or "Untitled"
    lines = [f"# {name}", ""]
    for task in tasks:
        text = task.get("text") or task.get("content") or ""
        completed = bool(task.get("completed", False))
        prefix = "- [x] " if completed else "- [ ] "
        lines.append(prefix + str(text).strip())
    return "\n".join(lines)


# ============================================================
# Date parsing
# ============================================================
DATE_PATTERN = re.compile(r"^(\d{2})/(\d{2})/(\d{4})")
DATE_FILENAME_PATTERN = re.compile(r"^(\d{2}_\d{2}_\d{4})")


def parse_project_date(name):
    m = DATE_PATTERN.match(name)
    if not m:
        return None
    try:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return datetime(year, month, day).date()
    except ValueError:
        return None


# ============================================================
# Google Drive (OAuth)
# ============================================================
def get_credentials():
    """Build credentials from token JSON stored in env var."""
    token_data = json.loads(GOOGLE_TOKEN_JSON)
    creds = Credentials.from_authorized_user_info(token_data, SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError(
                "Credentials invalid and cannot refresh. "
                "Re-run sync_recent.py locally to regenerate token.json."
            )

    return creds


def index_by_date_prefix(existing_files):
    """Group existing files by their leading DD_MM_YYYY prefix.
    Returns a dict mapping prefix -> list of (filename, file_id)."""
    by_prefix = {}
    for filename, file_id in existing_files.items():
        m = DATE_FILENAME_PATTERN.match(filename)
        if m:
            prefix = m.group(1)
            by_prefix.setdefault(prefix, []).append((filename, file_id))
    return by_prefix


def upsert_file(drive, filename, content_bytes, existing_files, by_prefix):
    """Upsert a file, identifying existing files by date prefix (DD_MM_YYYY)
    rather than exact filename. This way, renaming a Taskade project
    (e.g., adding ' - cheat day' mid-day) updates and renames the existing
    Drive file instead of creating a duplicate. If multiple files with the
    same date prefix already exist, the canonical one is kept and updated;
    the rest are moved to trash."""
    media = MediaInMemoryUpload(content_bytes, mimetype="text/markdown")

    m = DATE_FILENAME_PATTERN.match(filename)
    prefix = m.group(1) if m else None
    candidates = by_prefix.get(prefix, []) if prefix else []

    if not candidates:
        meta = {"name": filename, "parents": [GDRIVE_FOLDER_ID]}
        result = drive_call(
            lambda: drive.files()
            .create(body=meta, media_body=media, fields="id")
            .execute()
        )
        new_id = result["id"]
        existing_files[filename] = new_id
        if prefix:
            by_prefix.setdefault(prefix, []).append((filename, new_id))
        return "created"

    chosen_filename, chosen_id = next(
        ((n, i) for n, i in candidates if n == filename), candidates[0]
    )

    for other_name, other_id in candidates:
        if other_id == chosen_id:
            continue
        drive_call(
            lambda fid=other_id: drive.files()
            .update(fileId=fid, body={"trashed": True})
            .execute()
        )
        existing_files.pop(other_name, None)

    if chosen_filename != filename:
        drive_call(
            lambda: drive.files()
            .update(
                fileId=chosen_id,
                body={"name": filename},
                media_body=media,
            )
            .execute()
        )
        existing_files.pop(chosen_filename, None)
    else:
        drive_call(
            lambda: drive.files()
            .update(fileId=chosen_id, media_body=media)
            .execute()
        )

    existing_files[filename] = chosen_id
    by_prefix[prefix] = [(filename, chosen_id)]
    return "updated"


def list_existing_files(drive):
    files = {}
    page_token = None
    while True:
        current_token = page_token
        res = drive_call(
            lambda: drive.files()
            .list(
                q=f"'{GDRIVE_FOLDER_ID}' in parents and trashed = false",
                fields="nextPageToken, files(id, name)",
                spaces="drive",
                pageSize=1000,
                pageToken=current_token,
            )
            .execute()
        )
        for f in res.get("files", []):
            files[f["name"]] = f["id"]
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    return files


# ============================================================
# Main
# ============================================================
def safe_filename(name):
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if c in bad else c for c in name).strip()
    return cleaned or "untitled"


def main():
    missing = []
    if not TASKADE_TOKEN:
        missing.append("TASKADE_TOKEN")
    if not TASKADE_SUBSPACE_ID:
        missing.append("TASKADE_SUBSPACE_ID")
    if not GDRIVE_FOLDER_ID:
        missing.append("GDRIVE_FOLDER_ID")
    if not GOOGLE_TOKEN_JSON:
        missing.append("GOOGLE_TOKEN_JSON")
    if missing:
        print(f"ERROR: Missing environment variables: {', '.join(missing)}")
        sys.exit(1)

    creds = get_credentials()
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    existing_files = list_existing_files(drive)
    by_prefix = index_by_date_prefix(existing_files)

    today = datetime.now().date()
    cutoff = today - timedelta(days=RECENT_DAYS - 1)

    print(f"Syncing projects from {cutoff} to {today}...")

    all_projects = items_of(
        taskade_get(f"/folders/{TASKADE_SUBSPACE_ID}/projects")
    )

    recent = []
    for proj in all_projects:
        name = proj.get("name") or proj.get("title") or ""
        proj_date = parse_project_date(name)
        if proj_date and cutoff <= proj_date <= today:
            recent.append((proj_date, proj))

    recent.sort(key=lambda x: x[0], reverse=True)

    print(f"  Found {len(recent)} project(s) in date range.")

    created = 0
    updated = 0
    failed = 0
    error_types = Counter()

    for proj_date, proj in recent:
        proj_id = proj["id"]
        proj_name = proj.get("name") or proj.get("title") or proj_id
        filename = safe_filename(proj_name) + ".md"

        try:
            project_data_resp = taskade_get(f"/projects/{proj_id}")
            project_data_list = items_of(project_data_resp)
            project_data = project_data_list[0] if project_data_list else proj

            tasks_resp = taskade_get(
                f"/projects/{proj_id}/tasks", params={"limit": 500}
            )
            tasks = items_of(tasks_resp)

            md = project_to_markdown(project_data, tasks)
            action = upsert_file(
                drive, filename, md.encode("utf-8"), existing_files, by_prefix
            )
            if action == "created":
                created += 1
            else:
                updated += 1

        except Exception as e:
            failed += 1
            error_types[type(e).__name__] += 1

    print(
        f"\nDone. Created: {created}. Updated: {updated}. Failed: {failed}."
    )
    if error_types:
        summary = ", ".join(
            f"{name}={count}" for name, count in error_types.most_common()
        )
        print(f"Error types: {summary}")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
