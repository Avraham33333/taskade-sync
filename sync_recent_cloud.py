"""
Taskade -> Google Drive sync (recent days only, cloud-safe version).
Reads all secrets from environment variables.
Designed to run on GitHub Actions.
"""

import sys
import time
import os
import re
import json
from datetime import datetime, timedelta

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
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


def upsert_file(drive, filename, content_bytes, existing_files):
    media = MediaInMemoryUpload(content_bytes, mimetype="text/markdown")
    file_id = existing_files.get(filename)
    if file_id:
        drive.files().update(fileId=file_id, media_body=media).execute()
        return "updated"
    else:
        meta = {"name": filename, "parents": [GDRIVE_FOLDER_ID]}
        result = drive.files().create(
            body=meta, media_body=media, fields="id"
        ).execute()
        existing_files[filename] = result["id"]
        return "created"


def list_existing_files(drive):
    files = {}
    page_token = None
    while True:
        res = drive.files().list(
            q=f"'{GDRIVE_FOLDER_ID}' in parents and trashed = false",
            fields="nextPageToken, files(id, name)",
            spaces="drive",
            pageSize=1000,
            pageToken=page_token,
        ).execute()
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

    synced = 0
    failed = 0

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
                drive, filename, md.encode("utf-8"), existing_files
            )
            print(f"  [{action}] {filename}")
            synced += 1

        except Exception as e:
            print(f"  [FAIL] {proj_name}: {e}")
            failed += 1

    print(f"\nDone. Synced: {synced}. Failed: {failed}.")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()