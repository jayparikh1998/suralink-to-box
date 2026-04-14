# Suralink to Box

Small Python app that syncs files from Suralink engagements into Box folders.

It can run in two main modes:
- Single engagement sync using `SURALINK_ENGAGEMENT_ID`
- Customer-wide sync using `SURALINK_CUSTOMER_NAME` or `SURALINK_CUSTOMER_CUSTOM_ID`

Uploaded files are tracked in a local SQLite database so the same Suralink file is not uploaded repeatedly across runs.

## Project Layout

- `src/suralink_to_box/main.py`: main sync flow
- `src/suralink_to_box/suralink_client.py`: Suralink API client
- `src/suralink_to_box/box_client.py`: Box auth, folder resolution, and upload helpers
- `src/suralink_to_box/sync_tracker.py`: persistent file sync tracking
- `sync_state.db`: local sync history

## Requirements

- Python 3.14+
- `uv`
- Valid Suralink API token
- Valid Box credentials

## Setup

Install dependencies:

```bash
uv sync
```

Create a `.env` file in the project root.

## Environment Variables

### Required Suralink settings

```env
SURALINK_BASE_URL=https://your-suralink-base-url
SURALINK_TOKEN=your-token
```

### Engagement selection

Use one of these approaches:

Single engagement:

```env
SURALINK_ENGAGEMENT_ID=12345
```

Customer-wide by name:

```env
SURALINK_CUSTOMER_NAME=Acme Client
```

Customer-wide by custom ID:

```env
SURALINK_CUSTOMER_CUSTOM_ID=ACME-001
```

Selection priority in the code is:
1. `SURALINK_ENGAGEMENT_ID`
2. `SURALINK_CUSTOMER_CUSTOM_ID`
3. `SURALINK_CUSTOMER_NAME`
4. Fallback: first engagement found that has files

### Box settings

Developer token mode:

```env
BOX_AUTH_METHOD=developer_token
BOX_DEVELOPER_TOKEN=your-box-token
```

JWT mode:

```env
BOX_AUTH_METHOD=jwt
BOX_JWT_CONFIG_PATH=box_config.json
```

Optional Box settings:

```env
BOX_TARGET_FOLDER_ID=0
BOX_TARGET_FOLDER_PATH=Clients/2026 Uploads
BOX_AS_USER_ID=
```

How destination works:
- `BOX_TARGET_FOLDER_ID` is the Box folder ID used as the starting parent folder
- `BOX_TARGET_FOLDER_PATH` is an optional slash-delimited path created under that parent
- The app then creates:
  `BOX_TARGET_FOLDER_PATH / <client_name> / <engagement_name>`

Example final path:

```text
Clients / 2026 Uploads / Acme Client / Audit 2026
```

If `BOX_TARGET_FOLDER_PATH` is blank, the app behaves like before and uses only `BOX_TARGET_FOLDER_ID`.

### Optional behavior settings

```env
LOG_LEVEL=INFO
HTTP_TIMEOUT_SECONDS=60
HTTP_MAX_RETRIES=3
```

## Running

Run the sync:

```bash
uv run python -m suralink_to_box.main
```

## What the App Does

For each selected engagement, the app:
- lists engagement files from Suralink
- streams each file from Suralink
- creates the target folder structure in Box if needed
- streams the file into Box
- records the sync in `sync_state.db`

## Sync Tracking

The app uses `sync_state.db` to track synced files by:
- Suralink file ID
- engagement ID

This prevents duplicate uploads across runs.

If a file already exists in Box with the same name and Box returns a conflict, the app marks it as synced and skips re-uploading it.

## Transfer Behavior

Files are not written to the local filesystem during sync.

The current flow is:
- open a streamed HTTP download from Suralink
- pass that stream directly into the Box upload request

So this avoids local file saves and avoids buffering the whole file in memory, but it is still not a true vendor-to-vendor server-side transfer. The app remains in the middle of the stream.

## Verification

Fast verification command:

```bash
uv run python -m compileall src
```

## Notes

- `README.md` documents current behavior, but actual runtime behavior is defined by the code in `src/suralink_to_box/`
- Do not commit secrets in `.env`
