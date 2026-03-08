# AGENTS.md

## Purpose
This project syncs files from Suralink engagements into Box and tracks synced file IDs locally to prevent duplicate uploads.

Primary entrypoint: `src/suralink_to_box/main.py`

## Project Map
- `src/suralink_to_box/main.py`: end-to-end sync orchestration.
- `src/suralink_to_box/suralink_client.py`: Suralink API calls, pagination, file downloads.
- `src/suralink_to_box/box_client.py`: Box auth, folder lookup/create, uploads.
- `src/suralink_to_box/sync_tracker.py`: SQLite sync state (`sync_state.db`).
- `src/suralink_to_box/settings.py`: env-backed configuration.
- `src/suralink_to_box/file_utils.py`: local byte/file writing helper.

## Environment Configuration
Use `.env` in repo root.

Required Suralink:
- `SURALINK_BASE_URL`
- `SURALINK_TOKEN`

Engagement selection priority:
1. `SURALINK_ENGAGEMENT_ID`
2. `SURALINK_CUSTOMER_CUSTOM_ID`
3. `SURALINK_CUSTOMER_NAME`
4. fallback to first engagement with files

Required Box:
- `BOX_AUTH_METHOD` (`developer_token` or `jwt`)
- if `developer_token`: `BOX_DEVELOPER_TOKEN`
- if `jwt`: `BOX_JWT_CONFIG_PATH`

Useful optional:
- `BOX_TARGET_FOLDER_ID` (default `0`)
- `HTTP_TIMEOUT_SECONDS` (default `60.0`)
- `HTTP_MAX_RETRIES` (default `3`)
- `LOG_LEVEL` (default `INFO`)

## Setup And Run
```powershell
uv sync
uv run python -m suralink_to_box.main
```

## Safe Change Rules
- Keep pagination behavior intact for engagements/clients/files/requests.
- Preserve sync idempotency via `SyncTracker` checks.
- Do not hardcode credentials or endpoints.
- Keep retries bounded.
- Prefer explicit, debuggable error messages for API failures.

## Validation Checklist
1. Run with `SURALINK_ENGAGEMENT_ID` set to a small test engagement.
2. Check summary output (uploaded/skipped/failed).
3. Verify `sync_state.db` contains expected rows.
4. Re-run and confirm tracked skips increase.

## Operational Notes
- `settings.py` has duplicate `box_jwt_config_path`; functional but should be cleaned up separately.
- `README.md` is currently minimal; this file serves as practical run guidance.
- Avoid committing `.env`, `sync_state.db`, and downloaded files.
