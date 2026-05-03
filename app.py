from __future__ import annotations

import streamlit as st

from suralink_to_box.box_client import (
    get_box_client,
    list_box_folder_path_options,
    resolve_box_folder_shared_link,
)
from suralink_to_box.main import SyncOverrides, sync_to_box
from suralink_to_box.settings import get_settings
from suralink_to_box.suralink_client import SuralinkClient

STRUCTURE_OPTIONS = {
    "Categories / Requests": "categories_requests",
    "Categories": "categories",
    "Just Files": "just_files",
}

st.set_page_config(
    page_title="Suralink to Box",
    layout="wide",
)

settings = get_settings()
configured_base_box_path = (settings.box_target_folder_path or "").strip()
configured_root_folder_id = (settings.box_target_folder_id or "0").strip() or "0"


def _pick_client_name(client_obj: dict) -> str:
    return str(
        client_obj.get("name")
        or client_obj.get("clientName")
        or client_obj.get("title")
        or "(unnamed customer)"
    )


def _pick_client_custom_id(client_obj: dict) -> str:
    return str(client_obj.get("customId") or "").strip()


def _pick_client_id(client_obj: dict) -> str:
    return str(client_obj.get("id") or client_obj.get("clientId") or "").strip()


def _engagement_status_label(engagement_obj: dict) -> str:
    raw_state = engagement_obj.get("state")
    if isinstance(raw_state, str) and raw_state.strip().isdigit():
        raw_state = int(raw_state.strip())

    if raw_state == 1:
        return "Active"
    if raw_state == 2:
        return "Inactive"
    if raw_state == 3:
        return "Archived"
    return f"State {raw_state}" if raw_state not in (None, "") else "Unknown"


def _build_skipped_file_report(logs: list[str]) -> list[str]:
    skipped_entries: list[str] = []
    current_file_name: str | None = None
    allowed_skip_reasons = {
        "already tracked as synced",
        "file already exists in Box",
    }

    for line in logs:
        if "Processing:" in line and line.lstrip().startswith("["):
            current_file_name = line.split("Processing:", 1)[1].strip()
            continue

        if line.startswith("Skipped ->"):
            reason = line.replace("Skipped ->", "", 1).strip()
            if reason not in allowed_skip_reasons:
                continue
            if current_file_name:
                skipped_entries.append(f"{current_file_name} | {reason}")
            else:
                skipped_entries.append(reason)

    if skipped_entries:
        return skipped_entries
    return ["No skipped files in this run."]


def _collect_interesting_fields(value, *, prefix: str = "") -> dict[str, str]:
    interesting_tokens = (
        "download",
        "read",
        "view",
        "status",
        "state",
        "history",
        "created",
        "updated",
        "modified",
        "deleted",
        "sync",
    )
    collected: dict[str, str] = {}

    if isinstance(value, dict):
        for key, nested_value in value.items():
            key_text = str(key)
            nested_prefix = f"{prefix}.{key_text}" if prefix else key_text
            lowered = nested_prefix.lower()
            if any(token in lowered for token in interesting_tokens) and isinstance(
                nested_value, (str, int, float, bool)
            ):
                collected[nested_prefix] = str(nested_value)
            if isinstance(nested_value, (dict, list)):
                collected.update(_collect_interesting_fields(nested_value, prefix=nested_prefix))
    elif isinstance(value, list):
        for index, nested_value in enumerate(value):
            nested_prefix = f"{prefix}[{index}]" if prefix else f"[{index}]"
            if isinstance(nested_value, (dict, list)):
                collected.update(_collect_interesting_fields(nested_value, prefix=nested_prefix))

    return collected


def _build_file_debug_rows(file_payloads: list[dict]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for file_obj in file_payloads:
        interesting_fields = _collect_interesting_fields(file_obj)
        interesting_text = (
            " | ".join(f"{key}={value}" for key, value in sorted(interesting_fields.items()))
            if interesting_fields
            else "(none found)"
        )
        rows.append(
            {
                "File Name": str(file_obj.get("fileName") or file_obj.get("name") or "(unnamed file)"),
                "File ID": str(file_obj.get("id") or ""),
                "Request ID": str(file_obj.get("requestId") or ""),
                "Interesting Fields": interesting_text,
            }
        )
    return rows


@st.cache_data(ttl=300, show_spinner=False)
def load_suralink_customer_options() -> list[dict[str, str]]:
    client = SuralinkClient(settings)
    try:
        all_clients = client.list_all_clients()
    finally:
        client.close()

    seen_keys: set[tuple[str, str]] = set()
    options: list[dict[str, str]] = []
    for client_obj in all_clients:
        customer_name = _pick_client_name(client_obj).strip()
        custom_id = _pick_client_custom_id(client_obj)
        if not customer_name:
            continue

        dedupe_key = (customer_name.lower(), custom_id)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        label = (
            f"{customer_name} (customId={custom_id})"
            if custom_id
            else customer_name
        )
        options.append(
            {
                "label": label,
                "customer_name": customer_name,
                "custom_id": custom_id,
                "client_id": _pick_client_id(client_obj),
            }
        )

    return sorted(options, key=lambda option: option["label"].lower())


@st.cache_data(ttl=300, show_spinner=False)
def load_suralink_engagement_options(
    client_id: str,
) -> list[dict[str, str]]:
    client = SuralinkClient(settings)
    try:
        engagements = client.list_client_engagements(client_id)
    finally:
        client.close()

    options: list[dict[str, str]] = []
    for engagement in engagements:
        engagement_id = str(engagement.get("id") or "").strip()
        if not engagement_id:
            continue

        engagement_name = str(
            engagement.get("name")
            or engagement.get("title")
            or f"engagement_{engagement_id}"
        )
        status_label = _engagement_status_label(engagement)
        options.append(
            {
                "label": f"{engagement_name} [{status_label}] (id={engagement_id})",
                "engagement_id": engagement_id,
                "engagement_name": engagement_name,
                "status_label": status_label,
            }
        )

    return sorted(options, key=lambda option: option["label"].lower())


@st.cache_data(ttl=300, show_spinner=False)
def load_suralink_engagement_file_payloads(
    engagement_id: str,
) -> list[dict]:
    client = SuralinkClient(settings)
    try:
        all_files: list[dict] = []
        offset = 0
        page_size = 50

        while True:
            batch = client.list_engagement_files(engagement_id, limit=page_size, offset=offset)
            if not batch:
                break

            all_files.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size

        return all_files
    finally:
        client.close()


def load_box_folder_picker_options() -> tuple[str, list[dict[str, str]]]:
    client = get_box_client(settings=settings)
    base_folder, folder_options = list_box_folder_path_options(
        client,
        root_folder_id=configured_root_folder_id,
        folder_path=configured_base_box_path,
    )

    picker_options: list[dict[str, str]] = []
    base_label = configured_base_box_path or "Box root"
    picker_options.append(
        {
            "label": f"(Base root) {base_label}",
            "path": configured_base_box_path,
            "folder_id": base_folder.folder_id,
        }
    )

    for option in folder_options[1:]:
        full_path = (
            f"{configured_base_box_path} / {option.relative_path}"
            if configured_base_box_path
            else option.relative_path
        )
        picker_options.append(
            {
                "label": option.relative_path,
                "path": full_path,
                "folder_id": option.folder_id,
            }
        )

    cache_key = f"{configured_root_folder_id}|{configured_base_box_path}|{base_folder.folder_id}"
    return cache_key, picker_options


def load_box_child_folder_picker_options(
    selected_folder_path: str,
    selected_folder_id: str,
) -> list[dict[str, str]]:
    client = get_box_client(settings=settings)
    _, folder_options = list_box_folder_path_options(
        client,
        root_folder_id=selected_folder_id,
        folder_path="",
    )

    picker_options: list[dict[str, str]] = []
    picker_options.append(
        {
            "label": "(Stop here)",
            "path": selected_folder_path,
            "folder_id": selected_folder_id,
        }
    )

    for option in folder_options[1:]:
        full_path = (
            f"{selected_folder_path} / {option.relative_path}"
            if selected_folder_path
            else option.relative_path
        )
        picker_options.append(
            {
                "label": option.relative_path,
                "path": full_path,
                "folder_id": option.folder_id,
            }
        )

    return picker_options


st.title("Suralink to Box")
st.caption("Move Suralink files into Box with a simple customer-to-folder workflow.")
st.caption(
    "Base Box root: "
    f"`{configured_base_box_path or 'Box root'}`. "
    "Choose an existing folder under this base root to avoid typos and accidental folder creation."
)
utility_col1, utility_col2 = st.columns([1, 1])
with utility_col1:
    refresh_folders = st.button("Refresh Box Folders")
with utility_col2:
    suralink_refresh = st.button("Refresh Suralink List")

if refresh_folders:
    st.session_state.pop("box_folder_picker_cache_key", None)
    st.session_state.pop("box_folder_picker_options", None)
    st.session_state.pop("box_folder_picker_error", None)

if suralink_refresh:
    load_suralink_customer_options.clear()
    load_suralink_engagement_options.clear()
    load_suralink_engagement_file_payloads.clear()

current_picker_cache_key = st.session_state.get("box_folder_picker_cache_key")
expected_picker_prefix = f"{configured_root_folder_id}|{configured_base_box_path}|"

box_folder_picker_error = st.session_state.get("box_folder_picker_error")
box_folder_picker_options: list[dict[str, str]] = st.session_state.get("box_folder_picker_options", [])

try:
    suralink_customer_options = load_suralink_customer_options()
    suralink_customer_error = None
except Exception as exc:
    suralink_customer_options = []
    suralink_customer_error = f"{type(exc).__name__}: {exc}"

st.markdown("### 1. What to Move")
st.caption("Pick a Suralink customer and, if needed, one engagement.")
source_mode = st.radio(
    "Choose source",
    options=["Pick from Suralink", "Type manually"],
    index=0,
    horizontal=True,
    help="Pick live customers and engagements from Suralink, or type values manually.",
)

selected_customer_name = ""
selected_customer_custom_id = ""
selected_client_id = ""
selected_engagement_id = ""
selected_engagement_name = ""
selected_engagement_label = "All engagements"

if source_mode == "Pick from Suralink" and suralink_customer_options:
    customer_labels = [option["label"] for option in suralink_customer_options]
    default_customer_label = next(
        (
            option["label"]
            for option in suralink_customer_options
            if option["customer_name"] == (settings.suralink_customer_name or "")
        ),
        customer_labels[0],
    )
    selected_customer_label = st.selectbox(
        "Customer",
        options=customer_labels,
        index=customer_labels.index(default_customer_label),
        help="Choose a customer from the live Suralink customer list.",
    )
    selected_customer_option = next(
        option for option in suralink_customer_options
        if option["label"] == selected_customer_label
    )
    selected_customer_name = selected_customer_option["customer_name"]
    selected_customer_custom_id = selected_customer_option["custom_id"]
    selected_client_id = selected_customer_option["client_id"]

    try:
        engagement_options = load_suralink_engagement_options(
            selected_client_id,
        )
        engagement_error = None
    except Exception as exc:
        engagement_options = []
        engagement_error = f"{type(exc).__name__}: {exc}"

    engagement_choice_options = ["(All engagements for this customer)"]
    engagement_choice_options.extend(option["label"] for option in engagement_options)
    selected_engagement_label = st.selectbox(
        "Engagement",
        options=engagement_choice_options,
        index=0,
        help="Choose one engagement, or keep the all-engagements option to sync the full customer.",
    )
    if selected_engagement_label != "(All engagements for this customer)":
        selected_engagement_option = next(
            option for option in engagement_options
            if option["label"] == selected_engagement_label
        )
        selected_engagement_id = selected_engagement_option["engagement_id"]
        selected_engagement_name = selected_engagement_option["engagement_name"]
    elif engagement_error:
        st.caption(f"Could not load engagements for this customer: {engagement_error}")
elif source_mode == "Pick from Suralink":
    st.warning("Could not load the Suralink customer list, so manual source entry is enabled.")
    if suralink_customer_error:
        st.caption(f"Suralink list error: {suralink_customer_error}")
    source_mode = "Type manually"

if source_mode == "Type manually":
    selected_customer_name = st.text_input(
        "Customer Name",
        help="Sync all engagements for a customer by name when no engagement id is provided.",
        placeholder="Example: Acme Corp",
        value=(settings.suralink_customer_name or ""),
    ).strip()
    selected_engagement_name = st.text_input(
        "Engagement Name (Optional)",
        help="Optional. If provided, sync only the engagement with this exact name.",
        placeholder="Example: LL FCZO",
        value=(settings.suralink_engagement_name or ""),
    ).strip()
    selected_engagement_label = (
        f"Engagement `{selected_engagement_name}`"
        if selected_engagement_name
        else "All engagements"
    )

with st.expander("Suralink File Diagnostics"):
    st.caption(
        "Optional diagnostic tool. Use this to inspect the raw Suralink file payload for one "
        "selected engagement so we can see whether flags like downloaded, viewed, or status are exposed."
    )
    if selected_engagement_id:
        inspect_files = st.button(
            "Inspect Selected Engagement Files",
            help="Load the current file metadata from Suralink for the selected engagement.",
        )
        if inspect_files:
            try:
                st.session_state["suralink_file_diagnostics_payloads"] = load_suralink_engagement_file_payloads(
                    selected_engagement_id
                )
                st.session_state["suralink_file_diagnostics_engagement_id"] = selected_engagement_id
            except Exception as exc:
                st.session_state["suralink_file_diagnostics_error"] = f"{type(exc).__name__}: {exc}"
            else:
                st.session_state["suralink_file_diagnostics_error"] = None

        diagnostics_error = st.session_state.get("suralink_file_diagnostics_error")
        diagnostics_engagement_id = st.session_state.get("suralink_file_diagnostics_engagement_id")
        diagnostics_payloads = (
            st.session_state.get("suralink_file_diagnostics_payloads", [])
            if diagnostics_engagement_id == selected_engagement_id
            else []
        )

        if diagnostics_error and diagnostics_engagement_id == selected_engagement_id:
            st.warning(f"Could not inspect the selected engagement files: {diagnostics_error}")

        if diagnostics_payloads:
            st.caption(f"Found `{len(diagnostics_payloads)}` file payload(s) for this engagement.")
            st.dataframe(
                _build_file_debug_rows(diagnostics_payloads),
                use_container_width=True,
            )

            file_picker_labels = [
                f"{file_obj.get('fileName') or file_obj.get('name') or '(unnamed file)'} "
                f"(id={file_obj.get('id') or '?'})"
                for file_obj in diagnostics_payloads
            ]
            selected_file_label = st.selectbox(
                "View raw payload for file",
                options=file_picker_labels,
                index=0,
                key=f"suralink_file_payload_picker_{selected_engagement_id}",
            )
            selected_file_index = file_picker_labels.index(selected_file_label)
            st.json(diagnostics_payloads[selected_file_index], expanded=False)
        elif diagnostics_engagement_id == selected_engagement_id and not diagnostics_error:
            st.info("No file payloads were returned for this engagement.")
    else:
        st.info("Pick a specific Suralink engagement first to inspect file metadata.")

st.markdown("### 2. Where to Put It in Box")
st.caption("Paste a Box folder shared link, or browse the configured Box root.")
box_destination_mode = st.radio(
    "Choose Box destination method",
    options=["Paste Box shared link", "Browse configured folders"],
    index=0,
    horizontal=True,
    help="Use the link copied from Box's Share button, or choose from the configured Box root.",
)
box_target_folder_shared_link = ""
box_destination_ready = True

if box_destination_mode == "Paste Box shared link":
    box_target_folder_shared_link = st.text_input(
        "Box Folder Shared Link",
        value=(settings.box_target_folder_shared_link or ""),
        help="Paste the shared link copied from Box after turning Share link on. The link must point to a folder the Box account can access.",
        placeholder="https://yourcompany.box.com/s/...",
    ).strip()
    new_folder_name = st.text_input(
        "Existing Or New Subfolder Path",
        value="",
        help="Optional. Existing paths below the shared-link folder are reused; missing paths are created.",
        placeholder="Example: Jay or Jay / Test",
        key="shared_link_subfolder_path",
    ).strip()
    box_target_folder_path = new_folder_name
    box_destination_ready = bool(box_target_folder_shared_link)

    if box_target_folder_shared_link:
        validate_shared_link = st.button(
            "Check Box Shared Link",
            help="Confirm the pasted link resolves to a Box folder before running the sync.",
        )
        if validate_shared_link:
            try:
                resolved_shared_folder = resolve_box_folder_shared_link(
                    get_box_client(settings=settings),
                    shared_link=box_target_folder_shared_link,
                )
            except Exception as exc:
                st.warning(f"Could not resolve the shared link: {type(exc).__name__}: {exc}")
            else:
                st.success(
                    "Shared link resolves to "
                    f"`{resolved_shared_folder.folder_name}` "
                    f"(id={resolved_shared_folder.folder_id})."
                )
    else:
        st.info("Paste a Box folder shared link to enable sync.")
else:
    current_picker_cache_key = st.session_state.get("box_folder_picker_cache_key")
    if (
        "box_folder_picker_options" not in st.session_state
        or current_picker_cache_key is None
        or not str(current_picker_cache_key).startswith(expected_picker_prefix)
    ):
        try:
            picker_cache_key, picker_options = load_box_folder_picker_options()
        except Exception as exc:
            st.session_state["box_folder_picker_error"] = f"{type(exc).__name__}: {exc}"
            st.session_state["box_folder_picker_options"] = []
            st.session_state["box_folder_picker_cache_key"] = None
        else:
            st.session_state["box_folder_picker_error"] = None
            st.session_state["box_folder_picker_options"] = picker_options
            st.session_state["box_folder_picker_cache_key"] = picker_cache_key

    box_folder_picker_error = st.session_state.get("box_folder_picker_error")
    box_folder_picker_options = st.session_state.get("box_folder_picker_options", [])

    if box_folder_picker_options:
        folder_option_labels = [option["label"] for option in box_folder_picker_options]
        box_target_folder_label = st.selectbox(
            "Select Existing Box Folder",
            options=folder_option_labels,
            index=0,
            help="Choose an existing Box folder under the configured base root.",
        )
        selected_folder_option = next(
            option for option in box_folder_picker_options
            if option["label"] == box_target_folder_label
        )
        selected_existing_folder_path = selected_folder_option["path"]
        selected_existing_folder_id = selected_folder_option["folder_id"]
        selected_is_base_root = box_target_folder_label.startswith("(Base root)")

        selected_subfolder_path = selected_existing_folder_path
        selected_subfolder_id = selected_existing_folder_id
        browse_nested_subfolders = st.checkbox(
            "Browse deeper existing folders",
            value=False,
            help="Drill down one folder level at a time inside the selected Box folder.",
        )
        if browse_nested_subfolders and selected_is_base_root:
            st.caption(
                "Choose a specific existing Box folder first, then enable nested browsing. "
                "That keeps the folder browser fast and easier to use."
            )
        elif browse_nested_subfolders:
            for level in range(1, 7):
                try:
                    child_folder_options = load_box_child_folder_picker_options(
                        selected_subfolder_path,
                        selected_subfolder_id,
                    )
                except Exception as exc:
                    st.caption(f"Could not load subfolders at level {level}: {type(exc).__name__}: {exc}")
                    break

                if len(child_folder_options) <= 1:
                    break

                selected_child_label = st.selectbox(
                    f"Folder level {level}",
                    options=[option["label"] for option in child_folder_options],
                    index=0,
                    key=f"box_subfolder_level_{level}",
                    help="Choose the next folder level, or stop here.",
                )
                if selected_child_label == "(Stop here)":
                    break

                selected_child_option = next(
                    option for option in child_folder_options
                    if option["label"] == selected_child_label
                )
                selected_subfolder_path = selected_child_option["path"]
                selected_subfolder_id = selected_child_option["folder_id"]

        new_folder_name = st.text_input(
            "Existing Or New Subfolder Path",
            value="",
            help="Optionally enter a subfolder path under the selected Box folder or subfolder. Existing paths are reused; missing paths are created.",
            placeholder="Example: Jay or Jay / Test",
        ).strip()
        box_target_folder_path = (
            f"{selected_subfolder_path} / {new_folder_name}"
            if new_folder_name
            else selected_subfolder_path
        )
    else:
        st.warning(
            "Could not load the Box folder list, so manual path entry is temporarily enabled."
        )
        if box_folder_picker_error:
            st.caption(f"Folder list error: {box_folder_picker_error}")
        box_target_folder_path = st.text_input(
            "Existing Or New Subfolder Path",
            help="Enter a subfolder path under the configured base root. Existing paths are reused; missing paths are created.",
            placeholder="Example: Jay or Jay / Test",
            value="",
        )

if box_destination_mode == "Paste Box shared link":
    destination_preview = "Shared-link folder"
    if box_target_folder_path.strip():
        destination_preview = f"{destination_preview} / {box_target_folder_path.strip()}"
else:
    destination_preview = box_target_folder_path.strip() or configured_base_box_path or "Box root"
st.caption(f"Final Box destination: `{destination_preview}`")

st.markdown("### 3. Optional Rules")
with st.expander("Advanced Options"):
    box_overwrite_existing = st.checkbox(
        "Upload new Box versions when the file name already exists",
        value=settings.box_overwrite_existing,
        help="If enabled, a same-name file in the target Box folder will receive a new version instead of being skipped.",
    )
    box_mirror_mode = st.checkbox(
        "Mirror Suralink removals in Box",
        value=getattr(settings, "box_mirror_mode", False),
        help="If enabled, files that were previously synced but are no longer listed in Suralink will be moved into an archive folder in Box.",
    )
    configured_structure_mode = str(getattr(settings, "box_structure_mode", "just_files") or "just_files")
    structure_option_labels = list(STRUCTURE_OPTIONS.keys())
    structure_option_values = list(STRUCTURE_OPTIONS.values())
    try:
        structure_index = structure_option_values.index(configured_structure_mode)
    except ValueError:
        structure_index = structure_option_values.index("just_files")
    box_structure_label = st.selectbox(
        "Box Folder Structure",
        options=structure_option_labels,
        index=structure_index,
        help="Choose how files should be organized inside each engagement folder in Box.",
    )
    suralink_approved_only = st.checkbox(
        "Only sync approved Suralink files",
        value=settings.suralink_approved_only,
        help="If enabled, only files whose Suralink request state is green or approved will be synced.",
    )

st.markdown("### Quick Check")
if selected_engagement_id or selected_engagement_name:
    source_label = (
        f"Customer `{selected_customer_name or 'manual'}` / "
        f"Engagement `{selected_engagement_label}`"
    )
elif selected_customer_name:
    source_label = f"Customer `{selected_customer_name}` / {selected_engagement_label}"
else:
    source_label = "Not set yet"
destination_label = destination_preview
overwrite_label = "Enabled" if box_overwrite_existing else "Disabled"
structure_label = box_structure_label
approved_label = "Approved only" if suralink_approved_only else "All statuses"

options_label = (
    f"Structure: `{structure_label}` | "
    f"Status: `{approved_label}` | "
    f"Overwrite existing: `{overwrite_label}` | "
    f"Mirror removals: `{'Enabled' if box_mirror_mode else 'Disabled'}`"
)

preflight_col1, preflight_col2, preflight_col3 = st.columns(3)
preflight_col1.info(f"Source: {source_label}")
preflight_col2.info(f"Destination: `{destination_label}`")
preflight_col3.info(options_label)

can_sync = bool(selected_customer_name or selected_engagement_id or selected_engagement_name)
can_sync = can_sync and box_destination_ready
if not selected_customer_name and not selected_engagement_id and not selected_engagement_name:
    st.info("Choose a customer, or type a customer and engagement name, to enable sync.")
elif not box_destination_ready:
    st.info("Paste a Box folder shared link to enable sync.")

submitted = st.button(
    "Start Sync",
    type="primary",
    use_container_width=True,
    disabled=not can_sync,
)


log_container = st.container()
summary_container = st.container()
log_placeholder = log_container.empty()


def render_logs() -> None:
    log_placeholder.code(
        "\n".join(st.session_state.get("sync_logs", [])) or "No sync run yet.",
        language=None,
    )

if submitted:
    st.session_state["sync_logs"] = []

    if not selected_customer_name and not selected_engagement_id and not selected_engagement_name:
        st.error("Select a Suralink customer or enter a Suralink engagement name.")
    else:
        def append_log(message: str) -> None:
            logs = st.session_state.setdefault("sync_logs", [])
            logs.append(message)
            render_logs()

        with st.spinner("Running sync..."):
            try:
                summary = sync_to_box(
                    overrides=SyncOverrides(
                        suralink_engagement_id=selected_engagement_id or None,
                        suralink_engagement_name=selected_engagement_name or None,
                        suralink_customer_name=selected_customer_name or None,
                        suralink_customer_custom_id=(
                            selected_customer_custom_id or None
                            if not selected_engagement_id and not selected_client_id
                            else None
                        ),
                        suralink_client_id=(
                            selected_client_id or None
                            if not selected_engagement_id
                            else None
                        ),
                        box_target_folder_shared_link=box_target_folder_shared_link or None,
                        box_target_folder_path=(
                            box_target_folder_path.strip()
                            if box_destination_mode == "Paste Box shared link"
                            else box_target_folder_path.strip() or None
                        ),
                        box_overwrite_existing=box_overwrite_existing,
                        box_mirror_mode=box_mirror_mode,
                        box_structure_mode=STRUCTURE_OPTIONS[box_structure_label],
                        suralink_approved_only=suralink_approved_only,
                    ),
                    log=append_log,
                )
            except Exception as exc:
                st.error(f"Sync failed: {type(exc).__name__}: {exc}")
            else:
                st.session_state["sync_logs"] = _build_skipped_file_report(
                    st.session_state.get("sync_logs", [])
                )
                render_logs()
                st.success("Sync finished.")
                col1, col2, col3 = summary_container.columns(3)
                col1.metric("Engagements Selected", summary.engagements_selected)
                col2.metric("Engagements With Files", summary.engagements_with_files)
                col3.metric("Files Found", summary.total_files_found)

                col4, col5, col6, col7, col8 = summary_container.columns(5)
                col4.metric("Uploaded", summary.uploaded_successfully)
                col5.metric("Archived Missing", summary.archived_missing)
                col6.metric("Skipped Existing", summary.skipped_existing_box)
                col7.metric("Skipped Tracked", summary.skipped_tracked)
                col8.metric("Failed", summary.failed)


with log_container:
    st.subheader("Run Log")
    render_logs()
