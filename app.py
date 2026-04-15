from __future__ import annotations

import streamlit as st

from suralink_to_box.main import SyncOverrides, sync_to_box


st.set_page_config(
    page_title="Suralink to Box",
    layout="wide",
)

st.title("Suralink to Box")
st.caption("Sync Suralink engagement files into Box using the existing project credentials.")
with st.form("sync_form"):
    st.subheader("Sync Setup")
    sync_mode = st.radio(
        "What do you want to sync?",
        options=("Single engagement", "Customer"),
        horizontal=True,
        help="Choose whether to sync one engagement or all engagements for a customer.",
    )

    engagement_id = ""
    customer_name = ""
    if sync_mode == "Single engagement":
        engagement_id = st.text_input(
            "Suralink Engagement ID",
            help="Sync one specific engagement by ID.",
            placeholder="Example: 12345",
        )
    else:
        customer_name = st.text_input(
            "Suralink Customer Name",
            help="Sync all engagements for a customer by name.",
            placeholder="Example: Acme Corp",
        )

    box_target_folder_path = st.text_input(
        "Box Target Folder Path",
        help="Example: Clients/2026 Uploads. Existing folders are reused; missing folders are created.",
        placeholder="Example: Clients/2026 Uploads",
    )

    st.markdown("### Preflight Summary")
    source_label = (
        f"Engagement ID `{engagement_id.strip()}`"
        if sync_mode == "Single engagement" and engagement_id.strip()
        else f"Customer `{customer_name.strip()}`"
        if sync_mode == "Customer" and customer_name.strip()
        else "Not set yet"
    )
    destination_label = box_target_folder_path.strip() or "Box root folder"

    preflight_col1, preflight_col2 = st.columns(2)
    preflight_col1.info(f"Source: {source_label}")
    preflight_col2.info(f"Destination: `{destination_label}`")

    if sync_mode == "Single engagement" and not engagement_id.strip():
        st.warning("Enter a Suralink engagement ID to run this sync.")
    if sync_mode == "Customer" and not customer_name.strip():
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

    if sync_mode == "Single engagement" and not engagement_id.strip():
        st.error("Enter a Suralink engagement ID.")
    elif sync_mode == "Customer" and not customer_name.strip():
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
                        suralink_engagement_id=engagement_id.strip() or None,
                        suralink_customer_name=customer_name.strip() or None,
                        box_target_folder_path=box_target_folder_path.strip() or None,
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
