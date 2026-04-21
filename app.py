from __future__ import annotations

import streamlit as st

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

st.title("Suralink to Box")
st.caption("Sync Suralink engagement files into Box using the existing project credentials.")
with st.form("sync_form"):
    st.subheader("Sync Setup")
    customer_name = st.text_input(
        "Suralink Customer Name",
        help="Sync all engagements for a customer by name.",
        placeholder="Example: Acme Corp",
        value=(settings.suralink_customer_name or ""),
    )

    box_target_folder_path = st.text_input(
        "Box Target Folder Path",
        help="Example: Clients/2026 Uploads. Existing folders are reused; missing folders are created.",
        placeholder="Example: Clients/2026 Uploads",
        value=(settings.box_target_folder_path or ""),
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

    submitted = st.form_submit_button("Start Sync", use_container_width=True)


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
