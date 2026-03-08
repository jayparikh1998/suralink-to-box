from __future__ import annotations

import io
from dataclasses import dataclass

from suralink_to_box.settings import get_settings

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


def get_box_client() -> BoxClient:
    """
    Creates a Box client based on .env settings.
    - developer_token: easiest for testing
    - jwt: server-to-server
    """
    s = get_settings()

    method = (getattr(s, "box_auth_method", None) or "").strip().lower()
    if not method:
        raise ValueError("Missing BOX_AUTH_METHOD in .env (developer_token or jwt).")

    if method == "developer_token":
        token = (getattr(s, "box_developer_token", None) or "").strip()
        if not token:
            raise ValueError("Missing BOX_DEVELOPER_TOKEN in .env.")
        auth = BoxDeveloperTokenAuth(token=token)
        return BoxClient(auth=auth)

    if method == "jwt":
        path = (getattr(s, "box_jwt_config_path", None) or "").strip()
        if not path:
            raise ValueError("Missing BOX_JWT_CONFIG_PATH in .env.")
        jwt_config = JWTConfig.from_config_file(config_file_path=path)
        auth = BoxJWTAuth(config=jwt_config)
        return BoxClient(auth=auth)

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
    uploaded = client.uploads.upload_file(attributes=attrs, file=stream).entries[0]
    return BoxUploadResult(file_id=str(uploaded.id), file_name=str(uploaded.name))