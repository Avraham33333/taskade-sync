# taskade-sync

Automatically syncs the past 5 days of food tracking projects from a Taskade subspace into a Google Drive folder, so Claude (in claude.ai) can read them via the Google Drive connector.

## How it works

- A GitHub Action runs every minute.
- It calls Taskade API for projects whose names start with a date in the last 5 days (format: `DD/MM/YYYY`).
- Each project is converted to Markdown and uploaded to a designated Google Drive folder.
- Existing files in Drive are updated; new ones are created.

## Files

- `sync_recent_cloud.py` — the actual sync script. Reads all secrets from environment variables.
- `.github/workflows/sync.yml` — schedules the script to run automatically.

## Secrets (in GitHub repo Settings → Secrets and variables → Actions)

- `TASKADE_TOKEN` — Personal Access Token from Taskade (Settings → API).
- `TASKADE_SUBSPACE_ID` — ID of the Taskade subspace to sync (from URL: `taskade.com/spaces/.../subspaces/{ID}/projects`).
- `GDRIVE_FOLDER_ID` — ID of the destination Google Drive folder (from folder URL).
- `GOOGLE_TOKEN_JSON` — full contents of `token.json` produced by running `sync_recent.py` locally once for OAuth.

## Local setup (for initial OAuth and bootstrap)

1. Clone the repo.
2. Run `pip install requests google-auth google-auth-oauthlib google-api-python-client`.
3. Create `credentials.json` from a Google Cloud OAuth 2.0 Client (Desktop app type) and place it next to the script.
4. Run a local version of the script with the same logic (with hardcoded config) — it will open a browser for Google auth and produce `token.json`.
5. Paste the full contents of `token.json` as the `GOOGLE_TOKEN_JSON` secret.

## Bootstrap

For the initial full sync of all historical projects (not just last 5 days), use a separate `sync.py` that loops over all projects with retry-until-done logic.
