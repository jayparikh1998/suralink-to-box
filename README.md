# suralink-to-box

Sync files from Suralink into Box.

## Developer Setup

This repo is ready to share with other developers for local testing.

Each developer should use their own:
- Suralink token
- Box developer token or Box JWT app config
- local `.env` file

Do not share or commit:
- `.env`
- Box JWT config JSON files
- developer tokens
- downloaded files
- `sync_state.db`

## Install

From the repo root:

```bash
uv sync
```

## Configure

Copy the example environment file:

```bash
cp .env.example .env
```

Then fill in your own values.

### Option 1: Box developer token

Use this while testing or while waiting for Box admin approval for JWT:

```env
SURALINK_BASE_URL=https://your-suralink-base-url
SURALINK_TOKEN=your-suralink-token

BOX_AUTH_METHOD=developer_token
BOX_DEVELOPER_TOKEN=your-box-developer-token
BOX_TARGET_FOLDER_ID=0
```

### Option 2: Box JWT auth

Use this once your Box JWT app is approved:

```env
SURALINK_BASE_URL=https://your-suralink-base-url
SURALINK_TOKEN=your-suralink-token

BOX_AUTH_METHOD=jwt
BOX_JWT_CONFIG_PATH=/absolute/path/to/box-jwt-config.json
BOX_TARGET_FOLDER_ID=0
```

Optional:

```env
BOX_AS_USER_ID=
```

Notes:
- `BOX_TARGET_FOLDER_ID=0` means the Box root folder.
- With JWT auth, root usually means the app service account unless `BOX_AS_USER_ID` is set.
- Store JWT config files outside the repo.

## Choose What To Sync

Set one of these in `.env`:

```env
SURALINK_ENGAGEMENT_ID=12345
```

or

```env
SURALINK_CUSTOMER_NAME=Acme Corp
```

or

```env
SURALINK_CUSTOMER_CUSTOM_ID=acme-001
```

## Run The App

### CLI mode

```bash
uv run python -m suralink_to_box.main
```

### Streamlit mode

If this repo includes a Streamlit UI, run the file that imports `streamlit as st`, for example:

```bash
uv run streamlit run app.py
```

or

```bash
uv run streamlit run src/suralink_to_box/app.py
```

## What The App Does

The sync flow:
- reads config from `.env`
- finds one or more Suralink engagements
- downloads files locally into `downloads/`
- uploads them into Box
- records synced file IDs in `sync_state.db` to avoid duplicates on later runs

## Sharing With Other Developers

Recommended workflow:
1. Share the repo.
2. Have each developer run `uv sync`.
3. Have each developer copy `.env.example` to `.env`.
4. Each developer adds their own credentials locally.
5. Each developer runs the app locally.

## Future Direction

This repo is currently optimized for developer-local usage.

If this becomes a multi-user web application later, plan to move toward:
- centralized backend auth
- server-side secret management
- database-backed sync state
- user roles and permissions
- background job processing
