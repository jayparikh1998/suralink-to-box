from __future__ import annotations

import io
import json
import tempfile
from dataclasses import dataclass
from io import BufferedIOBase
from pathlib import Path
from textwrap import wrap

from suralink_to_box.settings import Settings, get_settings

from box_sdk_gen import BoxClient, BoxDeveloperTokenAuth, BoxJWTAuth, JWTConfig
from box_sdk_gen import UploadFileAttributes, UploadFileAttributesParentField
from box_sdk_gen.schemas.folder_full import FolderFull


@dataclass
class BoxUploadResult:
    file_id: str
    file_name: str


@dataclass
class BoxFolderResult:
    folder_id: str
    folder_name: str


def _normalize_box_private_key(private_key: str) -> str:
    key = private_key.strip().replace("\r\n", "\n").replace("\r", "\n")
    if "-----BEGIN" in key and "-----END" in key:
        return key

    body = "".join(line.strip() for line in key.splitlines() if line.strip())
    if not body:
        return key

    wrapped = "\n".join(wrap(body, 64))
    return (
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\n"
        f"{wrapped}\n"
        "-----END ENCRYPTED PRIVATE KEY-----\n"
    )


def _load_jwt_config(config_path: str) -> JWTConfig:
    path = Path(config_path)
    config_payload = json.loads(path.read_text(encoding="utf-8"))

    app_auth = (
        config_payload.get("boxAppSettings", {})
        .get("appAuth", {})
    )
    private_key = app_auth.get("privateKey")
    if not isinstance(private_key, str):
        return JWTConfig.from_config_file(config_file_path=config_path)

    normalized_key = _normalize_box_private_key(private_key)
    if normalized_key == private_key:
        return JWTConfig.from_config_file(config_file_path=config_path)

    app_auth["privateKey"] = normalized_key
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        ) as tmp_file:
            json.dump(config_payload, tmp_file)
            temp_path = tmp_file.name

        return JWTConfig.from_config_file(config_file_path=temp_path)
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


def get_box_client(settings: Settings | None = None) -> BoxClient:
    """
    Creates a Box client based on .env settings.
    - developer_token: easiest for testing
    - jwt: server-to-server
    """
    s = settings or get_settings()

    method = (getattr(s, "box_auth_method", None) or "").strip().lower()
    if not method:
        raise ValueError("Missing BOX_AUTH_METHOD in .env (developer_token or jwt).")

    as_user_id = (getattr(s, "box_as_user_id", None) or "").strip()

    if method == "developer_token":
        token = (getattr(s, "box_developer_token", None) or "").strip()
        if not token:
            raise ValueError("Missing BOX_DEVELOPER_TOKEN in .env.")
        auth = BoxDeveloperTokenAuth(token=token)
        client = BoxClient(auth=auth)
        if as_user_id:
            client = client.with_as_user_header(user_id=as_user_id)
        return client

    if method == "jwt":
        path = (getattr(s, "box_jwt_config_path", None) or "").strip()
        if not path:
            raise ValueError("Missing BOX_JWT_CONFIG_PATH in .env.")
        jwt_config = _load_jwt_config(path)
        auth = BoxJWTAuth(config=jwt_config)
        client = BoxClient(auth=auth)
        if as_user_id:
            client = client.with_as_user_header(user_id=as_user_id)
        return client

    raise ValueError("BOX_AUTH_METHOD must be 'developer_token' or 'jwt'.")


def ensure_box_subfolder(
    client: BoxClient,
    *,
    parent_folder_id: str,
    folder_name: str,
) -> BoxFolderResult:
    """
    Find a child folder by name under the given parent folder.
    If it does not exist, create it.
    """
    items = client.folders.get_folder_items(
        folder_id=str(parent_folder_id),
        fields=["id", "name", "type"],
        limit=1000,
    )

    for entry in items.entries:
        if getattr(entry, "type", None) == "folder" and getattr(entry, "name", None) == folder_name:
            return BoxFolderResult(folder_id=str(entry.id), folder_name=str(entry.name))

    created: FolderFull = client.folders.create_folder(
        name=folder_name,
        parent={"id": str(parent_folder_id)},
    )
    return BoxFolderResult(folder_id=str(created.id), folder_name=str(created.name))


def resolve_box_folder_path(
    client: BoxClient,
    *,
    root_folder_id: str,
    folder_path: str,
) -> BoxFolderResult:
    """
    Resolve a slash-delimited Box path from the given root folder, creating any
    missing intermediate folders along the way.
    """
    parts = [part.strip() for part in folder_path.split("/") if part.strip()]
    if not parts:
        return BoxFolderResult(folder_id=str(root_folder_id), folder_name="")

    current = BoxFolderResult(folder_id=str(root_folder_id), folder_name="")
    for part in parts:
        current = ensure_box_subfolder(
            client,
            parent_folder_id=current.folder_id,
            folder_name=part,
        )

    return current


def upload_bytes_to_box(
    client: BoxClient,
    *,
    folder_id: str,
    file_name: str,
    content: bytes,
) -> BoxUploadResult:
    """
    Upload bytes as a file into the target Box folder.
    """
    parent = UploadFileAttributesParentField(id=str(folder_id), type="folder")
    attrs = UploadFileAttributes(name=file_name, parent=parent)

    stream = io.BytesIO(content)
    return upload_stream_to_box(
        client,
        folder_id=folder_id,
        file_name=file_name,
        stream=stream,
    )


def upload_stream_to_box(
    client: BoxClient,
    *,
    folder_id: str,
    file_name: str,
    stream: BufferedIOBase,
    content_type: str | None = None,
) -> BoxUploadResult:
    """
    Upload a file-like binary stream into the target Box folder.
    """
    parent = UploadFileAttributesParentField(id=str(folder_id), type="folder")
    attrs = UploadFileAttributes(name=file_name, parent=parent)

    uploaded = client.uploads.upload_file(
        attributes=attrs,
        file=stream,
        file_content_type=content_type,
    ).entries[0]
    return BoxUploadResult(file_id=str(uploaded.id), file_name=str(uploaded.name))
