# suralink-to-box

Sync files from Suralink into Box.

## Setup

1. Install dependencies:

```bash
uv sync
```

2. Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

3. For Box JWT auth, create a Box Platform App with Server Authentication (JWT), download the JSON config file from Box, and store it outside git.

4. Run the program:

```bash
uv run python -m suralink_to_box.main
```

## Box JWT auth

Set these values in `.env`:

```env
BOX_AUTH_METHOD=jwt
BOX_JWT_CONFIG_PATH=/absolute/path/to/box-jwt-config.json
BOX_TARGET_FOLDER_ID=0
```

Optional:

```env
BOX_AS_USER_ID=
```

Notes:
- JWT auth uses the Box app's service account by default.
- If you need to access enterprise content as a managed user, set `BOX_AS_USER_ID`.
- Do not commit `.env` files or Box JWT config JSON files.
