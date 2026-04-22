from __future__ import annotations

import streamlit as st

from suralink_to_box.box_client import (
    get_box_client,
    list_box_folder_path_options,
)
from suralink_to_box.main import SyncOverrides, sync_to_box
from suralink_to_box.settings import get_settings

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


current_picker_cache_key = st.session_state.get("box_folder_picker_cache_key")
expected_picker_prefix = f"{configured_root_folder_id}|{configured_base_box_path}|"
refresh_folders = st.button("Refresh Box Folder List")
if refresh_folders:
    st.session_state.pop("box_folder_picker_cache_key", None)
    st.session_state.pop("box_folder_picker_options", None)
    st.session_state.pop("box_folder_picker_error", None)

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
box_folder_picker_options: list[dict[str, str]] = st.session_state.get("box_folder_picker_options", [])

st.title("Suralink to Box")
st.caption("Sync Suralink engagement files into Box using the existing project credentials.")
st.caption(
    "Base Box root: "
    f"`{configured_base_box_path or 'Box root'}`. "
    "Choose an existing folder under this base root to avoid typos and accidental folder creation."
)
st.subheader("Sync Setup")
customer_name = st.text_input(
    "Suralink Customer Name",
    help="Sync all engagements for a customer by name.",
    placeholder="Example: Acme Corp",
    value=(settings.suralink_customer_name or ""),
)

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
        "Browse Existing Nested Subfolders",
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
                f"Select Existing Box Subfolder Level {level}",
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
        "Existing or New Subfolder Path",
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
        "Existing or New Subfolder Path",
        help="Enter a subfolder path under the configured base root. Existing paths are reused; missing paths are created.",
        placeholder="Example: Jay or Jay / Test",
        value="",
    )
suralink_active_engagements_only = st.checkbox(
    "Only sync active engagements",
    value=settings.suralink_active_engagements_only,
    help="If enabled, inactive engagements for the customer will be filtered out before files are processed.",
)
box_overwrite_existing = st.checkbox(
    "Upload new Box versions when the file name already exists",
    value=settings.box_overwrite_existing,
    help="If enabled, a same-name file in the target Box folder will receive a new version instead of being skipped.",
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

st.markdown("### Preflight Summary")
source_label = f"Customer `{customer_name.strip()}`" if customer_name.strip() else "Not set yet"
destination_label = box_target_folder_path.strip() or "Box root folder"
engagement_label = "Active only" if suralink_active_engagements_only else "All engagements"
overwrite_label = "Enabled" if box_overwrite_existing else "Disabled"
structure_label = box_structure_label
approved_label = "Approved only" if suralink_approved_only else "All statuses"

preflight_col1, preflight_col2, preflight_col3, preflight_col4, preflight_col5, preflight_col6 = st.columns(6)
preflight_col1.info(f"Source: {source_label}")
preflight_col2.info(f"Destination: `{destination_label}`")
preflight_col3.info(f"Engagement Filter: `{engagement_label}`")
preflight_col4.info(f"Overwrite Existing: `{overwrite_label}`")
preflight_col5.info(f"Structure: `{structure_label}`")
preflight_col6.info(f"Status Filter: `{approved_label}`")

if not customer_name.strip():
    st.warning("Enter a Suralink customer name to run this sync.")

submitted = st.button("Start Sync", use_container_width=True)


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

    if not customer_name.strip():
        st.error("Enter a Suralink customer name.")
    else:
        def append_log(message: str) -> None:
            logs = st.session_state.setdefault("sync_logs", [])
            logs.append(message)
            render_logs()

        with st.spinner("Running sync..."):
            try:
                summary = sync_to_box(
                    overrides=SyncOverrides(
                        suralink_customer_name=customer_name.strip() or None,
                        suralink_active_engagements_only=suralink_active_engagements_only,
                        box_target_folder_path=box_target_folder_path.strip() or None,
                        box_overwrite_existing=box_overwrite_existing,
                        box_structure_mode=STRUCTURE_OPTIONS[box_structure_label],
                        suralink_approved_only=suralink_approved_only,
                    ),
                    log=append_log,
                )
            except Exception as exc:
                st.error(f"Sync failed: {type(exc).__name__}: {exc}")
            else:
                st.success("Sync finished.")
                col1, col2, col3 = summary_container.columns(3)
                col1.metric("Engagements Selected", summary.engagements_selected)
                col2.metric("Engagements With Files", summary.engagements_with_files)
                col3.metric("Files Found", summary.total_files_found)

                col4, col5, col6, col7 = summary_container.columns(4)
                col4.metric("Uploaded", summary.uploaded_successfully)
                col5.metric("Skipped Existing", summary.skipped_existing_box)
                col6.metric("Skipped Tracked", summary.skipped_tracked)
                col7.metric("Failed", summary.failed)


with log_container:
    st.subheader("Run Log")
    render_logs()
