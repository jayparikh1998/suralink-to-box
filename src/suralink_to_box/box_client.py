from __future__ import annotations

import io
import json
import tempfile
from dataclasses import dataclass
from io import BufferedIOBase
from pathlib import Path
from textwrap import wrap

import httpx

from suralink_to_box.settings import Settings, get_settings

from box_sdk_gen import BoxClient, BoxDeveloperTokenAuth, BoxJWTAuth, JWTConfig
from box_sdk_gen import UploadFileAttributes, UploadFileAttributesParentField
from box_sdk_gen.managers.files import UpdateFileByIdParent
from box_sdk_gen.managers.uploads import UploadFileVersionAttributes
from box_sdk_gen.schemas.folder_full import FolderFull


@dataclass
class BoxUploadResult:
    file_id: str
    file_name: str


@dataclass
class BoxFolderResult:
    folder_id: str
    folder_name: str


@dataclass
class BoxFileResult:
    file_id: str
    file_name: str


@dataclass
class BoxFolderPathOption:
    folder_id: str
    relative_path: str


class _SizedStream(BufferedIOBase):
    def __init__(self, base_stream: BufferedIOBase, content_length: int):
        self._base_stream = base_stream
        self.len = content_length

    def read(self, size: int = -1) -> bytes:
        return self._base_stream.read(size)

    def tell(self) -> int:
        return self._base_stream.tell()

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._base_stream.seek(offset, whence)

    def readable(self) -> bool:
        return True


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


def _resolve_jwt_config_path(config_path: str) -> Path:
    path = Path(config_path).expanduser()
    if path.is_absolute():
        return path

    candidates = [
        Path.cwd() / path,
        Path(__file__).resolve().parents[2] / path,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    return candidates[0]


def _load_jwt_config(config_path: str) -> JWTConfig:
    path = _resolve_jwt_config_path(config_path)
    if not path.exists():
        raise ValueError(
            "BOX_JWT_CONFIG_PATH does not point to an existing file. "
            f"Resolved path: {path}"
        )
    config_payload = json.loads(path.read_text(encoding="utf-8"))

    app_auth = (
        config_payload.get("boxAppSettings", {})
        .get("appAuth", {})
    )
    private_key = app_auth.get("privateKey")
    if not isinstance(private_key, str):
        return JWTConfig.from_config_file(config_file_path=str(path))

    normalized_key = _normalize_box_private_key(private_key)
    if normalized_key == private_key:
        return JWTConfig.from_config_file(config_file_path=str(path))

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


def resolve_box_folder_shared_link(
    client: BoxClient,
    *,
    shared_link: str,
    shared_link_password: str | None = None,
) -> BoxFolderResult:
    """
    Resolve a Box folder shared link into the folder id the API needs for
    uploads. The authenticated Box user or service account must be allowed to
    access the shared link.
    """
    cleaned_link = shared_link.strip()
    if not cleaned_link:
        raise ValueError("Paste a Box shared link before syncing.")

    boxapi = f"shared_link={cleaned_link}"
    cleaned_password = (shared_link_password or "").strip()
    if cleaned_password:
        boxapi += f"&shared_link_password={cleaned_password}"

    headers = {
        "Authorization": client.auth.retrieve_authorization_header(
            network_session=client.network_session
        ),
        "boxapi": boxapi,
        "Accept": "application/json",
        **getattr(client.network_session, "additional_headers", {}),
    }
    base_url = getattr(client.network_session.base_urls, "base_url", "https://api.box.com").rstrip("/")
    response = httpx.get(
        f"{base_url}/2.0/shared_items",
        params={"fields": "id,name,type"},
        headers=headers,
        follow_redirects=True,
        timeout=60.0,
    )

    response.raise_for_status()
    payload = response.json()
    if str(payload.get("type") or "").lower() != "folder":
        raise ValueError("The Box shared link must point to a folder.")

    return BoxFolderResult(
        folder_id=str(payload.get("id") or ""),
        folder_name=str(payload.get("name") or ""),
    )


def find_box_file_in_folder(
    client: BoxClient,
    *,
    folder_id: str,
    file_name: str,
) -> BoxFileResult | None:
    """
    Find a file by name directly under the given Box folder.
    """
    items = client.folders.get_folder_items(
        folder_id=str(folder_id),
        fields=["id", "name", "type"],
        limit=1000,
    )

    for entry in items.entries:
        if getattr(entry, "type", None) == "file" and getattr(entry, "name", None) == file_name:
            return BoxFileResult(file_id=str(entry.id), file_name=str(entry.name))

    return None


def list_box_folder_path_options(
    client: BoxClient,
    *,
    root_folder_id: str,
    folder_path: str,
) -> tuple[BoxFolderResult, list[BoxFolderPathOption]]:
    """
    Resolve the configured base folder and list the immediate child folders
    beneath it. The empty relative path represents the base folder itself.
    """
    base_folder = (
        resolve_box_folder_path(
            client,
            root_folder_id=root_folder_id,
            folder_path=folder_path,
        )
        if folder_path.strip()
        else BoxFolderResult(folder_id=str(root_folder_id), folder_name="")
    )

    items = client.folders.get_folder_items(
        folder_id=str(base_folder.folder_id),
        fields=["id", "name", "type"],
        limit=1000,
    )
    child_options = sorted(
        [
            BoxFolderPathOption(
                folder_id=str(entry.id),
                relative_path=str(entry.name),
            )
            for entry in items.entries
            if getattr(entry, "type", None) == "folder"
        ],
        key=lambda option: option.relative_path.lower(),
    )
    return base_folder, [
        BoxFolderPathOption(folder_id=base_folder.folder_id, relative_path=""),
        *child_options,
    ]


def list_box_descendant_folder_options(
    client: BoxClient,
    *,
    folder_id: str,
) -> list[BoxFolderPathOption]:
    """
    List all descendant folders beneath the given folder id, returning paths
    relative to that folder.
    """
    options: list[BoxFolderPathOption] = [
        BoxFolderPathOption(folder_id=str(folder_id), relative_path="")
    ]
    stack: list[tuple[str, str]] = [(str(folder_id), "")]
    seen_folder_ids = {str(folder_id)}

    while stack:
        current_folder_id, current_relative_path = stack.pop()
        items = client.folders.get_folder_items(
            folder_id=str(current_folder_id),
            fields=["id", "name", "type"],
            limit=1000,
        )

        child_folders = sorted(
            [
                entry for entry in items.entries
                if getattr(entry, "type", None) == "folder"
            ],
            key=lambda entry: str(getattr(entry, "name", "")).lower(),
        )

        for child_folder in child_folders:
            child_folder_id = str(child_folder.id)
            if child_folder_id in seen_folder_ids:
                continue

            child_name = str(child_folder.name)
            child_relative_path = (
                f"{current_relative_path} / {child_name}"
                if current_relative_path
                else child_name
            )
            options.append(
                BoxFolderPathOption(
                    folder_id=child_folder_id,
                    relative_path=child_relative_path,
                )
            )
            seen_folder_ids.add(child_folder_id)
            stack.append((child_folder_id, child_relative_path))

    return [options[0], *sorted(options[1:], key=lambda option: option.relative_path.lower())]


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
    content_length: int | None = None,
) -> BoxUploadResult:
    """
    Upload a file-like binary stream into the target Box folder.
    """
    parent = UploadFileAttributesParentField(id=str(folder_id), type="folder")
    attrs = UploadFileAttributes(name=file_name, parent=parent)

    upload_stream: BufferedIOBase
    if content_length is not None:
        upload_stream = _SizedStream(stream, content_length)
    else:
        upload_stream = io.BytesIO(stream.read())

    uploaded = client.uploads.upload_file(
        attributes=attrs,
        file=upload_stream,
        file_content_type=content_type,
    ).entries[0]
    return BoxUploadResult(file_id=str(uploaded.id), file_name=str(uploaded.name))


def upload_stream_to_box_version(
    client: BoxClient,
    *,
    file_id: str,
    file_name: str,
    stream: BufferedIOBase,
    content_type: str | None = None,
    content_length: int | None = None,
) -> BoxUploadResult:
    """
    Upload a new version for an existing Box file.
    """
    attrs = UploadFileVersionAttributes(name=file_name)

    upload_stream: BufferedIOBase
    if content_length is not None:
        upload_stream = _SizedStream(stream, content_length)
    else:
        upload_stream = io.BytesIO(stream.read())

    uploaded = client.uploads.upload_file_version(
        file_id=file_id,
        attributes=attrs,
        file=upload_stream,
        file_content_type=content_type,
    ).entries[0]
    return BoxUploadResult(file_id=str(uploaded.id), file_name=str(uploaded.name))


def move_box_file(
    client: BoxClient,
    *,
    file_id: str,
    parent_folder_id: str,
    file_name: str | None = None,
) -> BoxFileResult:
    """
    Move an existing Box file into a different folder. Optionally rename it.
    """
    updated = client.files.update_file_by_id(
        file_id=file_id,
        parent=UpdateFileByIdParent(id=str(parent_folder_id)),
        name=file_name,
    )
    return BoxFileResult(file_id=str(updated.id), file_name=str(updated.name))
