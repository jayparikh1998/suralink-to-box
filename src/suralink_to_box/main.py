from dataclasses import dataclass
from io import BytesIO

from suralink_to_box.settings import get_settings
from suralink_to_box.suralink_client import SuralinkClient
from suralink_to_box.box_client import (
    find_box_file_in_folder,
    get_box_client,
    upload_bytes_to_box,
    upload_stream_to_box,
    upload_stream_to_box_version,
    ensure_box_subfolder,
    move_box_file,
    resolve_box_folder_path,
    resolve_box_folder_shared_link,
)
from suralink_to_box.sync_tracker import SyncTracker


PAGE_SIZE = 50
STRUCTURE_CATEGORIES_REQUESTS = "categories_requests"
STRUCTURE_CATEGORIES = "categories"
STRUCTURE_JUST_FILES = "just_files"
ARCHIVE_FOLDER_NAME = "_Archived from Suralink"


@dataclass
class SyncOverrides:
    suralink_engagement_id: str | None = None
    suralink_engagement_name: str | None = None
    suralink_customer_name: str | None = None
    suralink_customer_custom_id: str | None = None
    suralink_client_id: str | None = None
    suralink_active_engagements_only: bool | None = None
    box_target_folder_id: str | None = None
    box_target_folder_path: str | None = None
    box_target_folder_shared_link: str | None = None
    box_overwrite_existing: bool | None = None
    box_mirror_mode: bool | None = None
    box_structure_mode: str | None = None
    suralink_approved_only: bool | None = None


@dataclass
class SyncSummary:
    engagements_selected: int
    engagements_with_files: int
    total_files_found: int
    uploaded_successfully: int
    archived_missing: int
    skipped_existing_box: int
    skipped_tracked: int
    failed: int


def _pick_id(obj: dict, keys: list[str]) -> str | None:
    for k in keys:
        v = obj.get(k)
        if v is not None:
            return str(v)
    return None


def _resolve_box_destination_settings(settings, log=print) -> tuple[str, str]:
    """
    Normalize Box destination settings.

    `BOX_TARGET_FOLDER_ID` must be a real Box folder id. For convenience, if a
    user puts a folder name/path there instead, treat it as a path rooted at
    Box's root folder.
    """
    raw_folder_id = (getattr(settings, "box_target_folder_id", None) or "0").strip()
    raw_folder_path = (getattr(settings, "box_target_folder_path", None) or "").strip()

    if raw_folder_path:
        return raw_folder_id or "0", raw_folder_path

    # Box folder ids are numeric strings. If a user entered a folder name like
    # "JayTestFolder", interpret it as a path instead of crashing on the API call.
    if raw_folder_id and not raw_folder_id.isdigit():
        log(
            "BOX_TARGET_FOLDER_ID does not look like a Box folder id. "
            f"Treating '{raw_folder_id}' as BOX_TARGET_FOLDER_PATH under root."
        )
        return "0", raw_folder_id

    return raw_folder_id or "0", raw_folder_path


def _pick_name(obj: dict | None) -> str:
    if not obj:
        return "(unnamed)"
    return str(
        obj.get("requestName")
        or obj.get("name")
        or obj.get("title")
        or "(unnamed)"
    )


def _normalize_structure_mode(raw_value: str | None) -> str:
    value = (raw_value or "").strip().lower()
    if value in {
        STRUCTURE_CATEGORIES_REQUESTS,
        STRUCTURE_CATEGORIES,
        STRUCTURE_JUST_FILES,
    }:
        return value
    return STRUCTURE_JUST_FILES


def _structure_mode_label(mode: str) -> str:
    labels = {
        STRUCTURE_CATEGORIES_REQUESTS: "Categories / Requests",
        STRUCTURE_CATEGORIES: "Categories",
        STRUCTURE_JUST_FILES: "Just Files",
    }
    return labels.get(mode, "Just Files")


def _box_safe_folder_name(raw_name: str | None, *, fallback: str) -> str:
    value = str(raw_name or "").strip()
    if not value:
        return fallback

    sanitized = (
        value.replace("\\", " - ")
        .replace("/", " - ")
        .replace(":", " - ")
        .replace("*", "")
        .replace("?", "")
        .replace('"', "'")
        .replace("<", "(")
        .replace(">", ")")
        .replace("|", "-")
    )
    sanitized = " ".join(sanitized.split()).strip(" .")
    return sanitized or fallback


def _pick_customer_name(obj: dict | None) -> str | None:
    if not obj:
        return None

    direct = obj.get("customerName") or obj.get("clientName")
    if direct:
        return str(direct)

    customer = obj.get("customer") or obj.get("client")
    if isinstance(customer, dict):
        nested = customer.get("name") or customer.get("title")
        if nested:
            return str(nested)

    return None


def _engagement_matches_customer_name(engagement: dict, customer_name: str) -> bool:
    candidate = (_pick_customer_name(engagement) or "").strip().lower()
    target = customer_name.strip().lower()
    return bool(candidate) and candidate == target


def _pick_customer_id(obj: dict | None) -> str | None:
    if not obj:
        return None

    direct = obj.get("customerId") or obj.get("clientId")
    if direct is not None:
        return str(direct)

    customer = obj.get("customer") or obj.get("client")
    if isinstance(customer, dict):
        nested = customer.get("id")
        if nested is not None:
            return str(nested)

    return None


def _pick_client_name(client_obj: dict | None) -> str | None:
    if not client_obj:
        return None
    name = client_obj.get("name") or client_obj.get("clientName") or client_obj.get("title")
    if name:
        return str(name)
    return None


def _pick_client_id(client_obj: dict | None) -> str | None:
    if not client_obj:
        return None
    raw = client_obj.get("id") or client_obj.get("clientId")
    if raw is not None:
        return str(raw)
    return None


def _resolve_client_folder_name(engagement: dict, configured_customer_name: str) -> str:
    detected = (_pick_customer_name(engagement) or "").strip()
    if detected:
        return _box_safe_folder_name(
            f"Suralink - {detected}",
            fallback="Suralink - unknown_client",
        )

    configured = configured_customer_name.strip()
    if configured:
        return _box_safe_folder_name(
            f"Suralink - {configured}",
            fallback="Suralink - unknown_client",
        )

    return "Suralink - unknown_client"


def _resolve_category_folder_name(request_obj: dict | None) -> str:
    if not isinstance(request_obj, dict):
        return "Unknown Category"
    return _box_safe_folder_name(
        request_obj.get("categoryName"),
        fallback="Unknown Category",
    )


def _resolve_request_folder_name(request_obj: dict | None, request_id: str) -> str:
    if not isinstance(request_obj, dict):
        return f"Request {request_id}"

    request_number = request_obj.get("requestNumber")
    request_name = _pick_name(request_obj)
    if request_number not in (None, ""):
        return _box_safe_folder_name(
            f"{request_number} {request_name}",
            fallback=f"Request {request_id}",
        )

    return _box_safe_folder_name(
        request_name,
        fallback=f"Request {request_id}",
    )


def _build_archived_file_name(file_name: str, suralink_file_id: str) -> str:
    stem, dot, extension = file_name.rpartition(".")
    suffix = f" [removed from Suralink {suralink_file_id}]"
    if dot:
        return f"{stem}{suffix}.{extension}"
    return f"{file_name}{suffix}"


def _build_comments_doc_name(client_name: str) -> str:
    safe_name = _box_safe_folder_name(
        f"Suralink Comments - {client_name}",
        fallback="Suralink Comments",
    )
    return f"{safe_name}.docx"


def _fetch_all_requests(client: SuralinkClient, engagement_id: str) -> list[dict]:
    all_requests: list[dict] = []
    offset = 0

    while True:
        batch = client.list_requests(engagement_id, limit=PAGE_SIZE, offset=offset)
        if not batch:
            break

        all_requests.extend(batch)

        if len(batch) < PAGE_SIZE:
            break

        offset += PAGE_SIZE

    return all_requests


def _fetch_all_files(client: SuralinkClient, engagement_id: str) -> list[dict]:
    all_files: list[dict] = []
    offset = 0

    while True:
        batch = client.list_engagement_files(engagement_id, limit=PAGE_SIZE, offset=offset)
        if not batch:
            break

        all_files.extend(batch)

        if len(batch) < PAGE_SIZE:
            break

        offset += PAGE_SIZE

    return all_files


def _normalize_status_text(value: object) -> str:
    return str(value).strip().lower()


def _extract_engagement_activity_values(engagement: dict | None) -> list[str]:
    if not isinstance(engagement, dict):
        return []

    values: list[str] = []
    seen: set[str] = set()
    relevant_tokens = ("active", "inactive", "archiv", "closed", "open", "status", "state")
    nested_value_keys = {"id", "name", "title", "label", "value", "code", "status", "state"}

    def add(raw: object) -> None:
        normalized = _normalize_status_text(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            values.append(normalized)

    for key, value in engagement.items():
        lowered_key = key.lower()
        if not any(token in lowered_key for token in relevant_tokens):
            continue

        if isinstance(value, dict):
            for nested_key in nested_value_keys:
                if nested_key in value:
                    add(value[nested_key])
        elif isinstance(value, list):
            for item in value:
                add(item)
        else:
            add(value)

    return values


def _is_active_engagement(engagement: dict) -> tuple[bool, list[str]]:
    raw_state = engagement.get("state")
    raw_status = engagement.get("status")

    for raw_value in (raw_state, raw_status):
        if isinstance(raw_value, bool):
            continue
        if isinstance(raw_value, int):
            # Observed from the Suralink engagement payload:
            # 1 = active, 2 = inactive, and archived/non-active states are
            # expected to be other non-1 values such as 3.
            if raw_value == 1:
                return True, [str(raw_value)]
            return False, [str(raw_value)]
        if isinstance(raw_value, str) and raw_value.strip().isdigit():
            numeric_value = int(raw_value.strip())
            if numeric_value == 1:
                return True, [str(numeric_value)]
            return False, [str(numeric_value)]

    observed_values = _extract_engagement_activity_values(engagement)
    active_markers = ("active", "open", "current", "true", "yes", "1")
    inactive_markers = ("inactive", "archived", "closed", "complete", "completed", "false", "no", "0", "2", "3")

    if any(any(marker in value for marker in inactive_markers) for value in observed_values):
        return False, observed_values
    if any(any(marker in value for marker in active_markers) for value in observed_values):
        return True, observed_values

    # Default open if nothing explicit is found, but preserve observed values for logging.
    return True, observed_values


def _engagement_activity_debug_snapshot(engagement: dict | None) -> dict[str, object]:
    if not isinstance(engagement, dict):
        return {}

    snapshot: dict[str, object] = {}
    for key, value in engagement.items():
        lowered_key = key.lower()
        if any(token in lowered_key for token in ("active", "inactive", "archiv", "closed", "open", "status", "state")):
            snapshot[key] = value
    return snapshot


def _extract_status_values(payload: dict | None) -> list[str]:
    if not isinstance(payload, dict):
        return []

    direct_keys = {
        "state",
        "status",
        "color",
        "statusColor",
        "approvalStatus",
        "approvalState",
        "requestState",
        "requestStatus",
        "reviewStatus",
        "isApproved",
        "isComplete",
        "isCompleted",
        "isReceived",
        "isFulfilled",
    }
    nested_value_keys = {"id", "name", "title", "label", "value", "code", "color"}

    values: list[str] = []
    seen: set[str] = set()

    def add(raw: object) -> None:
        normalized = _normalize_status_text(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            values.append(normalized)

    for key, value in payload.items():
        lowered_key = key.lower()
        key_looks_relevant = (
            key in direct_keys
            or any(
                token in lowered_key
                for token in (
                    "status",
                    "state",
                    "approval",
                    "color",
                    "complete",
                    "received",
                    "fulfilled",
                )
            )
        )

        if key_looks_relevant:
            if isinstance(value, dict):
                for nested_key in nested_value_keys:
                    if nested_key in value:
                        add(value[nested_key])
            elif isinstance(value, list):
                for item in value:
                    add(item)
            else:
                add(value)

    return values


def _is_approved_request(*, request_obj: dict | None, file_obj: dict | None) -> tuple[bool, list[str]]:
    raw_request_state = request_obj.get("state") if isinstance(request_obj, dict) else None
    if isinstance(raw_request_state, bool):
        raw_request_state = None

    if isinstance(raw_request_state, int):
        # Observed from the live Suralink request-item payload for the UI shown:
        # 1 = default/pending, 2 = flagged, 3 = green check, 4 = red X.
        return raw_request_state == 3, [str(raw_request_state)]

    if isinstance(raw_request_state, str) and raw_request_state.strip().isdigit():
        numeric_state = int(raw_request_state.strip())
        return numeric_state == 3, [str(numeric_state)]

    observed_values = _extract_status_values(request_obj) + [
        value for value in _extract_status_values(file_obj)
        if value not in _extract_status_values(request_obj)
    ]
    approved_markers = (
        "green",
        "approved",
        "complete",
        "completed",
    )
    is_approved = any(
        any(marker in value for marker in approved_markers)
        for value in observed_values
    )
    return is_approved, observed_values


def _status_debug_snapshot(payload: dict | None) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}

    snapshot: dict[str, object] = {}
    for key, value in payload.items():
        lowered_key = key.lower()
        if any(
            token in lowered_key
            for token in ("status", "state", "approval", "color", "complete", "received", "fulfilled")
        ):
            snapshot[key] = value
    return snapshot


def _settings_with_overrides(overrides: SyncOverrides | None):
    settings = get_settings()
    if not overrides:
        return settings

    update_data = {
        key: value
        for key, value in vars(overrides).items()
        if value is not None
    }
    return settings.model_copy(update=update_data)


def sync_to_box(
    *,
    overrides: SyncOverrides | None = None,
    log=print,
) -> SyncSummary:
    s = _settings_with_overrides(overrides)
    client = SuralinkClient(s)
    tracker = SyncTracker()

    try:
        log("\nSync Suralink engagements to Box with persistent file-id tracking")

        configured_engagement_id = (getattr(s, "suralink_engagement_id", None) or "").strip()
        configured_engagement_name = (getattr(s, "suralink_engagement_name", None) or "").strip()
        configured_customer_custom_id = (getattr(s, "suralink_customer_custom_id", None) or "").strip()
        configured_client_id = (getattr(s, "suralink_client_id", None) or "").strip()
        configured_customer_name = (getattr(s, "suralink_customer_name", None) or "").strip()
        active_engagements_only = bool(getattr(s, "suralink_active_engagements_only", False))
        overwrite_existing = bool(getattr(s, "box_overwrite_existing", False))
        mirror_mode = bool(getattr(s, "box_mirror_mode", False))
        structure_mode = _normalize_structure_mode(getattr(s, "box_structure_mode", None))
        approved_only = bool(getattr(s, "suralink_approved_only", False))

        if active_engagements_only:
            log("Active-engagement-only mode is enabled: inactive engagements will be filtered out before sync.")
        if overwrite_existing:
            log("Box overwrite mode is enabled: same-name files will be uploaded as new Box versions.")
        if mirror_mode:
            log(
                "Mirror mode is enabled: tracked Box files that are no longer listed in Suralink "
                f"will be moved into '{ARCHIVE_FOLDER_NAME}'."
            )
        log(f"Box structure mode: {_structure_mode_label(structure_mode)}")
        if approved_only:
            log("Approved-only mode is enabled: only Suralink files with a green/approved request state will sync.")

        selected_engagements: list[dict] = []

        # Priority: single engagement ID > single engagement name > client id > customer custom ID > customer name > fallback
        if configured_engagement_id:
            log("1) Use engagement from settings")
            selected_engagements = [{"id": configured_engagement_id, "name": f"engagement_{configured_engagement_id}"}]

            # Best effort: resolve friendly name.
            try:
                for engagement in client.list_engagements():
                    if _pick_id(engagement, ["id"]) == configured_engagement_id:
                        selected_engagements = [engagement]
                        break
            except Exception:
                pass

        elif configured_engagement_name:
            log("1) Resolve engagement by name from settings")
            candidate_engagements: list[dict] = []

            if configured_client_id:
                candidate_engagements = client.list_client_engagements(configured_client_id)
                log(
                    f"Loaded {len(candidate_engagements)} engagement(s) for clientId={configured_client_id} "
                    "before engagement-name filtering."
                )
            elif configured_customer_name:
                all_clients = client.list_all_clients()
                matching_clients = [
                    c for c in all_clients
                    if (_pick_client_name(c) or "").strip().lower() == configured_customer_name.lower()
                ]
                if matching_clients:
                    if len(matching_clients) > 1:
                        log(
                            f"Warning: found {len(matching_clients)} clients named "
                            f"'{configured_customer_name}'. Using the first match."
                        )
                    matched_client_id = _pick_client_id(matching_clients[0])
                    if matched_client_id:
                        candidate_engagements = client.list_client_engagements(matched_client_id)
                        log(
                            f"Loaded {len(candidate_engagements)} engagement(s) for customer "
                            f"'{configured_customer_name}' via clientId={matched_client_id}."
                        )

            if not candidate_engagements:
                candidate_engagements = client.list_all_engagements()
                log(
                    f"Falling back to all engagements for engagement-name lookup "
                    f"'{configured_engagement_name}'."
                )

            target_name = configured_engagement_name.lower()
            selected_engagements = [
                engagement
                for engagement in candidate_engagements
                if (_pick_name(engagement) or "").strip().lower() == target_name
            ]
            log(
                f"Found {len(selected_engagements)} engagement(s) named "
                f"'{configured_engagement_name}'."
            )

        elif configured_client_id:
            log("1) Use client ID from settings")
            selected_engagements = client.list_client_engagements(configured_client_id)
            log(f"Found {len(selected_engagements)} engagement(s) for clientId={configured_client_id}.")

        elif configured_customer_custom_id:
            log("1) Use customer custom ID from settings")
            selected_engagements = client.list_engagements_by_custom_id(configured_customer_custom_id)
            log(f"Found {len(selected_engagements)} engagement(s) for customId={configured_customer_custom_id}.")

        elif configured_customer_name:
            log("1) Resolve customer by name from /clients, then list customer engagements")
            all_clients = client.list_all_clients()
            matching_clients = [
                c for c in all_clients
                if (_pick_client_name(c) or "").strip().lower() == configured_customer_name.lower()
            ]

            if matching_clients:
                if len(matching_clients) > 1:
                    log(
                        f"Warning: found {len(matching_clients)} clients named "
                        f"'{configured_customer_name}'. Using the first match."
                    )
                matched_client = matching_clients[0]
                matched_client_id = _pick_client_id(matched_client)
                matched_custom_id = str(matched_client.get("customId") or "").strip()

                if matched_custom_id:
                    selected_engagements = client.list_engagements_by_custom_id(
                        matched_custom_id
                    )
                    log(
                        f"Found {len(selected_engagements)} engagement(s) for customer "
                        f"'{configured_customer_name}' (customId={matched_custom_id})."
                    )
                else:
                    log(
                        f"Client '{configured_customer_name}' was found but has no customId. "
                        "Falling back to paged engagement filtering by client id/name."
                    )
                    all_engagements = client.list_all_engagements()
                    selected_engagements = [
                        e
                        for e in all_engagements
                        if (
                            (matched_client_id and _pick_customer_id(e) == matched_client_id)
                            or _engagement_matches_customer_name(e, configured_customer_name)
                        )
                    ]
                    log(
                        f"Found {len(selected_engagements)} engagement(s) for customer "
                        f"'{configured_customer_name}' via client-id/name fallback."
                    )

            if not selected_engagements:
                log("Fallback: filter /engagements by customer name fields")
                all_engagements = client.list_all_engagements()
                selected_engagements = [
                    e for e in all_engagements if _engagement_matches_customer_name(e, configured_customer_name)
                ]
                log(
                    f"Found {len(selected_engagements)} engagement(s) for customer name "
                    f"'{configured_customer_name}'."
                )

        else:
            log("1) Fallback mode: find one engagement with files")
            engagements = client.list_engagements()
            for engagement in engagements:
                eid = _pick_id(engagement, ["id"])
                if not eid:
                    continue
                try:
                    if _fetch_all_files(client, eid):
                        selected_engagements = [engagement]
                        break
                except Exception:
                    continue

        if not selected_engagements:
            log("No matching engagements found.")
            return SyncSummary(
                engagements_selected=0,
                engagements_with_files=0,
                total_files_found=0,
                uploaded_successfully=0,
                archived_missing=0,
                skipped_existing_box=0,
                skipped_tracked=0,
                failed=0,
            )

        if active_engagements_only:
            filtered_engagements: list[dict] = []
            for engagement in selected_engagements:
                is_active, observed_values = _is_active_engagement(engagement)
                if is_active:
                    filtered_engagements.append(engagement)
                    continue

                engagement_name = _pick_name(engagement)
                observed_text = ", ".join(observed_values) if observed_values else "none found"
                log(
                    f"Skipping inactive engagement: {engagement_name}. "
                    f"Observed activity values: {observed_text}"
                )
                debug_fields = _engagement_activity_debug_snapshot(engagement)
                if debug_fields:
                    log(f"Engagement activity fields: {debug_fields}")

            log(
                f"Active engagement filter kept {len(filtered_engagements)} of "
                f"{len(selected_engagements)} engagement(s)."
            )
            selected_engagements = filtered_engagements

        if not selected_engagements:
            log("No active engagements matched the current filter.")
            return SyncSummary(
                engagements_selected=0,
                engagements_with_files=0,
                total_files_found=0,
                uploaded_successfully=0,
                archived_missing=0,
                skipped_existing_box=0,
                skipped_tracked=0,
                failed=0,
            )

        box_client = get_box_client(settings=s)
        try:
            current_box_user = box_client.users.get_user_me(fields=["id", "name", "login"])
            log(
                "Authenticated Box account: "
                f"{current_box_user.name} ({current_box_user.login}) "
                f"[id={current_box_user.id}]"
            )
        except Exception as e:
            log(f"Warning: could not inspect current Box account identity: {type(e).__name__}: {e}")

        if (getattr(s, "box_auth_method", None) or "").strip().lower() == "jwt":
            configured_as_user = (getattr(s, "box_as_user_id", None) or "").strip()
            if configured_as_user:
                log(f"JWT As-User is configured: {configured_as_user}")
            else:
                log(
                    "JWT is currently using the Box service account because BOX_AS_USER_ID is not set. "
                    "If you expect uploads in a managed user's Box account, that user must be authorized "
                    "for As-User access in the Box app configuration."
                )

        shared_link = (getattr(s, "box_target_folder_shared_link", None) or "").strip()
        root_folder_id, root_folder_path = _resolve_box_destination_settings(s, log=log)
        base_path_parts = []
        if shared_link:
            shared_folder = resolve_box_folder_shared_link(
                box_client,
                shared_link=shared_link,
            )
            root_folder_id = shared_folder.folder_id
            base_path_parts.append(shared_folder.folder_name)
            log(
                "Resolved Box shared-link folder: "
                f"{shared_folder.folder_name} (id={shared_folder.folder_id})"
            )

        destination_root = (
            resolve_box_folder_path(
                box_client,
                root_folder_id=root_folder_id,
                folder_path=root_folder_path,
            )
            if root_folder_path
            else None
        )
        if destination_root:
            base_path_parts.extend(
                part.strip()
                for part in root_folder_path.strip("/").split("/")
                if part.strip()
            )
            log(
                "Resolved Box destination root: "
                f"{' / '.join(base_path_parts)} (id={destination_root.folder_id})"
            )
        elif shared_link:
            log(f"Resolved Box destination root: {' / '.join(base_path_parts)} (id={root_folder_id})")
        else:
            log(f"Resolved Box destination root: Box root (id={root_folder_id})")

        comments_parent_folder_id = (
            destination_root.folder_id
            if destination_root
            else root_folder_id
        )
        comments_client_name = (
            _resolve_client_folder_name(selected_engagements[0], configured_customer_name)
            if selected_engagements
            else "Suralink - unknown_client"
        )
        comments_groups = []
        comments_export_error: Exception | None = None
        try:
            from suralink_to_box.comment_export import (
                build_suralink_comments_docx_from_groups,
                collect_suralink_comments_for_requests,
            )
        except Exception as e:
            build_suralink_comments_docx_from_groups = None
            collect_suralink_comments_for_requests = None
            comments_export_error = e

        total_engagements_with_files = 0
        total_files_found = 0
        uploaded_count = 0
        archived_missing_count = 0
        skipped_existing_box_count = 0
        skipped_tracked_count = 0
        failed_count = 0

        for engagement in selected_engagements:
            engagement_id = _pick_id(engagement, ["id"])
            engagement_name = _box_safe_folder_name(
                _pick_name(engagement),
                fallback=f"engagement_{engagement_id or 'unknown'}",
            )
            client_name = _resolve_client_folder_name(engagement, configured_customer_name)
            if not engagement_id:
                continue

            engagement_is_active, engagement_activity_values = _is_active_engagement(engagement)

            log(f"\nProcessing engagement: {engagement_name} (id={engagement_id})")
            if approved_only and not active_engagements_only and not engagement_is_active:
                observed_text = ", ".join(engagement_activity_values) if engagement_activity_values else "none found"
                log(
                    "Approved-only filter is bypassed for this inactive engagement. "
                    f"Observed activity values: {observed_text}"
                )

            try:
                files = _fetch_all_files(client, engagement_id)
            except Exception as e:
                log(f"Failed to list files for engagement {engagement_id}: {e}")
                continue

            tracked_entries = tracker.list_synced_files_for_engagement(engagement_id=engagement_id)
            current_file_ids = {
                str(file_obj.get("id"))
                for file_obj in files
                if file_obj.get("id") is not None
            }
            client_parent_folder_id = (
                destination_root.folder_id
                if destination_root
                else root_folder_id
            )
            client_folder: tuple[str, str] | None = None
            engagement_folder: tuple[str, str] | None = None
            archive_folder: tuple[str, str] | None = None
            engagement_base_path_parts = base_path_parts.copy()
            category_folder_cache: dict[str, tuple[str, str]] = {}
            request_folder_cache: dict[str, tuple[str, str]] = {}

            def ensure_engagement_destination() -> tuple[tuple[str, str], tuple[str, str]]:
                nonlocal client_folder, engagement_folder

                if client_folder is None:
                    created_client_folder = ensure_box_subfolder(
                        box_client,
                        parent_folder_id=client_parent_folder_id,
                        folder_name=client_name,
                    )
                    client_folder = (
                        created_client_folder.folder_id,
                        created_client_folder.folder_name,
                    )

                if engagement_folder is None:
                    created_engagement_folder = ensure_box_subfolder(
                        box_client,
                        parent_folder_id=client_folder[0],
                        folder_name=engagement_name,
                    )
                    engagement_folder = (
                        created_engagement_folder.folder_id,
                        created_engagement_folder.folder_name,
                    )
                    log(
                        "Using Box folder path: "
                        f"{' / '.join([*engagement_base_path_parts, client_folder[1], engagement_folder[1]])} "
                        f"(id={engagement_folder[0]})"
                    )

                return client_folder, engagement_folder

            if mirror_mode and tracked_entries:
                missing_tracked_entries = [
                    entry
                    for entry in tracked_entries
                    if entry.suralink_file_id not in current_file_ids
                ]
                if missing_tracked_entries:
                    _, ensured_engagement_folder = ensure_engagement_destination()
                    if archive_folder is None:
                        created_archive_folder = ensure_box_subfolder(
                            box_client,
                            parent_folder_id=ensured_engagement_folder[0],
                            folder_name=ARCHIVE_FOLDER_NAME,
                        )
                        archive_folder = (
                            created_archive_folder.folder_id,
                            created_archive_folder.folder_name,
                        )
                        log(
                            "Mirror archive folder: "
                            f"{' / '.join([*engagement_base_path_parts, client_name, engagement_name, archive_folder[1]])} "
                            f"(id={archive_folder[0]})"
                        )

                    for tracked_entry in missing_tracked_entries:
                        tracked_name = tracked_entry.file_name or f"file_{tracked_entry.suralink_file_id}"
                        if not tracked_entry.box_file_id:
                            tracker.unmark_synced(
                                suralink_file_id=tracked_entry.suralink_file_id,
                                engagement_id=engagement_id,
                            )
                            log(
                                "Mirror -> removed local tracker entry for a file that is no longer "
                                f"in Suralink but had no Box file id: {tracked_name}"
                            )
                            continue

                        try:
                            moved_file = move_box_file(
                                box_client,
                                file_id=tracked_entry.box_file_id,
                                parent_folder_id=archive_folder[0],
                                file_name=tracked_name,
                            )
                        except Exception as e:
                            error_text = str(e)
                            if "item_name_in_use" in error_text or "same name already exists" in error_text:
                                moved_file = move_box_file(
                                    box_client,
                                    file_id=tracked_entry.box_file_id,
                                    parent_folder_id=archive_folder[0],
                                    file_name=_build_archived_file_name(
                                        tracked_name,
                                        tracked_entry.suralink_file_id,
                                    ),
                                )
                            else:
                                failed_count += 1
                                log(
                                    "Mirror -> failed to archive missing Suralink file "
                                    f"{tracked_name}: {type(e).__name__}: {e}"
                                )
                                continue

                        tracker.unmark_synced(
                            suralink_file_id=tracked_entry.suralink_file_id,
                            engagement_id=engagement_id,
                        )
                        archived_missing_count += 1
                        log(
                            "Mirror -> archived Box file because it is no longer present in Suralink: "
                            f"{moved_file.file_name} (Box file id={moved_file.file_id})"
                        )

            if not files:
                log("No files found for this engagement. Skipping uploads.")
                continue

            total_engagements_with_files += 1
            total_files_found += len(files)

            try:
                requests = _fetch_all_requests(client, engagement_id)
            except Exception as e:
                log(f"Could not list requests for engagement {engagement_id}: {e}")
                requests = []

            request_map: dict[str, dict] = {}
            for request in requests:
                rid = _pick_id(request, ["id"])
                if rid:
                    request_map[rid] = request

            approved_comment_request_ids: set[str] = set()
            for file_obj in files:
                request_id = str(file_obj.get("requestId") or "")
                if not request_id:
                    continue
                request_obj = request_map.get(request_id)
                is_approved_for_comments, _ = _is_approved_request(
                    request_obj=request_obj,
                    file_obj=file_obj,
                )
                if is_approved_for_comments:
                    approved_comment_request_ids.add(request_id)

            if collect_suralink_comments_for_requests and approved_comment_request_ids:
                approved_comment_requests = [
                    request
                    for request in requests
                    if (_pick_id(request, ["id"]) or "") in approved_comment_request_ids
                ]
                try:
                    comments_group = collect_suralink_comments_for_requests(
                        client,
                        engagement=engagement,
                        requests=approved_comment_requests,
                    )
                except Exception as e:
                    comments_export_error = e
                    log(
                        "Warning: could not collect Suralink comments for approved requests "
                        f"in engagement {engagement_id}: {type(e).__name__}: {e}"
                    )
                else:
                    if comments_group:
                        comments_groups.append(comments_group)
                    log(
                        "Checked comments for "
                        f"{len(approved_comment_requests)} approved request(s) in this engagement."
                    )

            log(f"Found {len(files)} file(s) in engagement.")

            for index, file_obj in enumerate(files, start=1):
                file_id = str(file_obj["id"])
                request_id = str(file_obj["requestId"])
                file_name = file_obj.get("fileName", f"file_{file_id}.bin")

                req = request_map.get(request_id)
                request_name = _pick_name(req) if req else "(unknown request)"
                request_state = str(req.get("state")) if req else "(unknown)"

                log(f"\n[{index}/{len(files)}] Processing: {file_name}")
                log(
                    f"Request: {request_name} | Request ID: {request_id} | "
                    f"State: {request_state} | File ID: {file_id}"
                )

                if approved_only and engagement_is_active:
                    is_approved, observed_status_values = _is_approved_request(
                        request_obj=req,
                        file_obj=file_obj,
                    )
                    if not is_approved:
                        observed_text = ", ".join(observed_status_values) if observed_status_values else "none found"
                        log(
                            "Skipped -> file is not marked approved/green by the current detector. "
                            f"Observed status values: {observed_text}"
                        )
                        request_debug = _status_debug_snapshot(req)
                        file_debug = _status_debug_snapshot(file_obj)
                        if request_debug:
                            log(f"Request status fields: {request_debug}")
                        if file_debug:
                            log(f"File status fields: {file_debug}")
                        continue

                if tracker.is_synced(suralink_file_id=file_id, engagement_id=engagement_id):
                    skipped_tracked_count += 1
                    log("Skipped -> already tracked as synced")
                    continue

                ensured_client_folder, ensured_engagement_folder = ensure_engagement_destination()

                target_folder_id = ensured_engagement_folder[0]
                target_path_parts = [*engagement_base_path_parts, ensured_client_folder[1], ensured_engagement_folder[1]]
                if structure_mode in {STRUCTURE_CATEGORIES_REQUESTS, STRUCTURE_CATEGORIES}:
                    category_folder_name = _resolve_category_folder_name(req)
                    category_folder = category_folder_cache.get(category_folder_name)
                    if category_folder is None:
                        created_category_folder = ensure_box_subfolder(
                            box_client,
                            parent_folder_id=ensured_engagement_folder[0],
                            folder_name=category_folder_name,
                        )
                        category_folder = (
                            created_category_folder.folder_id,
                            created_category_folder.folder_name,
                        )
                        category_folder_cache[category_folder_name] = category_folder

                    target_folder_id = category_folder[0]
                    target_path_parts = [*target_path_parts, category_folder[1]]

                    if structure_mode == STRUCTURE_CATEGORIES_REQUESTS:
                        request_folder_name = _resolve_request_folder_name(req, request_id)
                        request_cache_key = f"{category_folder_name}::{request_folder_name}"
                        request_folder = request_folder_cache.get(request_cache_key)
                        if request_folder is None:
                            created_request_folder = ensure_box_subfolder(
                                box_client,
                                parent_folder_id=category_folder[0],
                                folder_name=request_folder_name,
                            )
                            request_folder = (
                                created_request_folder.folder_id,
                                created_request_folder.folder_name,
                            )
                            request_folder_cache[request_cache_key] = request_folder

                        target_folder_id = request_folder[0]
                        target_path_parts = [*target_path_parts, request_folder[1]]

                log(f"Target Box folder: {' / '.join(target_path_parts)} (id={target_folder_id})")

                try:
                    with client.stream_engagement_file(
                        audit_id=engagement_id,
                        request_id=request_id,
                        file_id=file_id,
                        fallback_filename=file_name,
                    ) as downloaded:
                        log("Streaming file from Suralink to Box")
                        log(f"Content-Type: {downloaded.content_type}")
                        if downloaded.content_length is not None:
                            log(f"Size (bytes): {downloaded.content_length}")
                        else:
                            log("Size (bytes): unknown")

                        existing_box_file = (
                            find_box_file_in_folder(
                                box_client,
                                folder_id=target_folder_id,
                                file_name=downloaded.filename,
                            )
                            if overwrite_existing
                            else None
                        )

                        if existing_box_file:
                            log(
                                "Existing Box file found with the same name. "
                                f"Uploading a new version to file id={existing_box_file.file_id}."
                            )
                            result = upload_stream_to_box_version(
                                box_client,
                                file_id=existing_box_file.file_id,
                                file_name=downloaded.filename,
                                stream=downloaded.stream,
                                content_type=downloaded.content_type,
                                content_length=downloaded.content_length,
                            )
                        else:
                            result = upload_stream_to_box(
                                box_client,
                                folder_id=target_folder_id,
                                file_name=downloaded.filename,
                                stream=downloaded.stream,
                                content_type=downloaded.content_type,
                                content_length=downloaded.content_length,
                            )

                    log("Upload complete")
                    log(f"Box File Name: {result.file_name}")
                    log(f"Box File ID: {result.file_id}")

                    tracker.mark_synced(
                        suralink_file_id=file_id,
                        engagement_id=engagement_id,
                        request_id=request_id,
                        file_name=downloaded.filename,
                        box_file_id=result.file_id,
                        box_folder_id=target_folder_id,
                    )

                    uploaded_count += 1

                except Exception as e:
                    error_text = str(e)
                    if "item_name_in_use" in error_text or "same name already exists" in error_text:
                        skipped_existing_box_count += 1
                        log("Skipped -> file already exists in Box")
                        log(f"Existing file name: {file_name}")
                        existing_box_file = find_box_file_in_folder(
                            box_client,
                            folder_id=target_folder_id,
                            file_name=file_name,
                        )
                        if existing_box_file:
                            log(
                                "Box confirms an existing file in the target folder: "
                                f"id={existing_box_file.file_id}, name={existing_box_file.file_name}"
                            )
                        log(
                            "Not marking this file as synced locally because the upload did not complete. "
                            "You can rerun after cleaning the Box target or enable overwrite."
                        )
                    else:
                        failed_count += 1
                        log(f"Failed -> {type(e).__name__}: {e}")

        if comments_export_error and not comments_groups:
            log(
                "Warning: could not transfer Suralink comments to Box: "
                f"{type(comments_export_error).__name__}: {comments_export_error}"
            )
        elif build_suralink_comments_docx_from_groups and comments_groups:
            try:
                comments_client_folder = ensure_box_subfolder(
                    box_client,
                    parent_folder_id=comments_parent_folder_id,
                    folder_name=comments_client_name,
                )
                comments_file_name = _build_comments_doc_name(comments_client_name)
                comments_export = build_suralink_comments_docx_from_groups(
                    comments_groups,
                    filename=comments_file_name,
                )
                existing_comments_doc = find_box_file_in_folder(
                    box_client,
                    folder_id=comments_client_folder.folder_id,
                    file_name=comments_export.filename,
                )
                if existing_comments_doc:
                    uploaded_comments_doc = upload_stream_to_box_version(
                        box_client,
                        file_id=existing_comments_doc.file_id,
                        file_name=comments_export.filename,
                        stream=BytesIO(comments_export.docx_bytes),
                        content_type=(
                            "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document"
                        ),
                        content_length=len(comments_export.docx_bytes),
                    )
                    log(
                        "Uploaded Suralink comments Word document as a new Box version: "
                        f"{uploaded_comments_doc.file_name} "
                        f"(file id={uploaded_comments_doc.file_id})"
                    )
                else:
                    uploaded_comments_doc = upload_bytes_to_box(
                        box_client,
                        folder_id=comments_client_folder.folder_id,
                        file_name=comments_export.filename,
                        content=comments_export.docx_bytes,
                    )
                    log(
                        "Uploaded Suralink comments Word document: "
                        f"{uploaded_comments_doc.file_name} "
                        f"(file id={uploaded_comments_doc.file_id})"
                    )
                log(
                    "Suralink comments included for approved files only: "
                    f"{comments_export.comment_count} comment(s), "
                    f"{comments_export.request_count} request(s), "
                    f"{comments_export.engagement_count} engagement(s)."
                )
            except Exception as e:
                log(f"Warning: could not transfer Suralink comments to Box: {type(e).__name__}: {e}")
        else:
            log("No Suralink comments found for approved files in the selected source.")

        log("\nSync summary")
        log(f"Engagements selected: {len(selected_engagements)}")
        log(f"Engagements with files: {total_engagements_with_files}")
        log(f"Total files found: {total_files_found}")
        log(f"Uploaded successfully: {uploaded_count}")
        log(f"Archived from Box because missing in Suralink: {archived_missing_count}")
        log(f"Skipped (already existed in Box this run): {skipped_existing_box_count}")
        log(f"Skipped (already tracked from previous runs): {skipped_tracked_count}")
        log(f"Failed: {failed_count}")
        return SyncSummary(
            engagements_selected=len(selected_engagements),
            engagements_with_files=total_engagements_with_files,
            total_files_found=total_files_found,
            uploaded_successfully=uploaded_count,
            archived_missing=archived_missing_count,
            skipped_existing_box=skipped_existing_box_count,
            skipped_tracked=skipped_tracked_count,
            failed=failed_count,
        )

    finally:
        client.close()
        tracker.close()


def main() -> None:
    sync_to_box()


if __name__ == "__main__":
    main()
