from suralink_to_box.settings import get_settings
from suralink_to_box.suralink_client import SuralinkClient
from suralink_to_box.file_utils import save_bytes
from suralink_to_box.box_client import get_box_client, upload_bytes_to_box, ensure_box_subfolder
from suralink_to_box.sync_tracker import SyncTracker


PAGE_SIZE = 50


def _pick_id(obj: dict, keys: list[str]) -> str | None:
    for k in keys:
        v = obj.get(k)
        if v is not None:
            return str(v)
    return None


def _pick_name(obj: dict | None) -> str:
    if not obj:
        return "(unnamed)"
    return str(
        obj.get("requestName")
        or obj.get("name")
        or obj.get("title")
        or "(unnamed)"
    )


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
        return detected

    configured = configured_customer_name.strip()
    if configured:
        return configured

    return "unknown_client"


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


def main() -> None:
    s = get_settings()
    client = SuralinkClient(s)
    tracker = SyncTracker()

    try:
        print("\nSync Suralink engagements to Box with persistent file-id tracking")

        configured_engagement_id = (getattr(s, "suralink_engagement_id", None) or "").strip()
        configured_customer_custom_id = (getattr(s, "suralink_customer_custom_id", None) or "").strip()
        configured_customer_name = (getattr(s, "suralink_customer_name", None) or "").strip()

        selected_engagements: list[dict] = []

        # Priority: single engagement ID > customer custom ID > customer name > fallback
        if configured_engagement_id:
            print("1) Use engagement from settings")
            selected_engagements = [{"id": configured_engagement_id, "name": f"engagement_{configured_engagement_id}"}]

            # Best effort: resolve friendly name.
            try:
                for engagement in client.list_engagements():
                    if _pick_id(engagement, ["id"]) == configured_engagement_id:
                        selected_engagements = [engagement]
                        break
            except Exception:
                pass

        elif configured_customer_custom_id:
            print("1) Use customer custom ID from settings")
            selected_engagements = client.list_engagements_by_custom_id(configured_customer_custom_id)
            print(f"Found {len(selected_engagements)} engagement(s) for customId={configured_customer_custom_id}.")

        elif configured_customer_name:
            print("1) Resolve customer by name from /clients, then list customer engagements")
            all_clients = client.list_all_clients()
            matching_clients = [
                c for c in all_clients
                if (_pick_client_name(c) or "").strip().lower() == configured_customer_name.lower()
            ]

            if matching_clients:
                if len(matching_clients) > 1:
                    print(
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
                    print(
                        f"Found {len(selected_engagements)} engagement(s) for customer "
                        f"'{configured_customer_name}' (customId={matched_custom_id})."
                    )
                else:
                    print(
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
                    print(
                        f"Found {len(selected_engagements)} engagement(s) for customer "
                        f"'{configured_customer_name}' via client-id/name fallback."
                    )

            if not selected_engagements:
                print("Fallback: filter /engagements by customer name fields")
                all_engagements = client.list_all_engagements()
                selected_engagements = [
                    e for e in all_engagements if _engagement_matches_customer_name(e, configured_customer_name)
                ]
                print(
                    f"Found {len(selected_engagements)} engagement(s) for customer name "
                    f"'{configured_customer_name}'."
                )

        else:
            print("1) Fallback mode: find one engagement with files")
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
            print("No matching engagements found.")
            return

        box_client = get_box_client()
        root_folder_id = (getattr(s, "box_target_folder_id", None) or "0").strip()

        total_engagements_with_files = 0
        total_files_found = 0
        uploaded_count = 0
        skipped_existing_box_count = 0
        skipped_tracked_count = 0
        failed_count = 0

        for engagement in selected_engagements:
            engagement_id = _pick_id(engagement, ["id"])
            engagement_name = _pick_name(engagement)
            client_name = _resolve_client_folder_name(engagement, configured_customer_name)
            if not engagement_id:
                continue

            print(f"\nProcessing engagement: {engagement_name} (id={engagement_id})")

            try:
                files = _fetch_all_files(client, engagement_id)
            except Exception as e:
                print(f"Failed to list files for engagement {engagement_id}: {e}")
                continue

            if not files:
                print("No files found for this engagement. Skipping.")
                continue

            total_engagements_with_files += 1
            total_files_found += len(files)

            try:
                requests = _fetch_all_requests(client, engagement_id)
            except Exception as e:
                print(f"Could not list requests for engagement {engagement_id}: {e}")
                requests = []

            request_map: dict[str, dict] = {}
            for request in requests:
                rid = _pick_id(request, ["id"])
                if rid:
                    request_map[rid] = request

            client_folder = ensure_box_subfolder(
                box_client,
                parent_folder_id=root_folder_id,
                folder_name=client_name,
            )
            engagement_folder = ensure_box_subfolder(
                box_client,
                parent_folder_id=client_folder.folder_id,
                folder_name=engagement_name,
            )
            folder_id = engagement_folder.folder_id
            print(
                f"Using Box folder path: {client_folder.folder_name} / "
                f"{engagement_folder.folder_name} (id={folder_id})"
            )

            print(f"Found {len(files)} file(s) in engagement.")

            for index, file_obj in enumerate(files, start=1):
                file_id = str(file_obj["id"])
                request_id = str(file_obj["requestId"])
                file_name = file_obj.get("fileName", f"file_{file_id}.bin")

                req = request_map.get(request_id)
                request_name = _pick_name(req) if req else "(unknown request)"
                request_state = str(req.get("state")) if req else "(unknown)"

                print(f"\n[{index}/{len(files)}] Processing: {file_name}")
                print(
                    f"Request: {request_name} | Request ID: {request_id} | "
                    f"State: {request_state} | File ID: {file_id}"
                )

                if tracker.is_synced(suralink_file_id=file_id, engagement_id=engagement_id):
                    skipped_tracked_count += 1
                    print("Skipped -> already tracked as synced")
                    continue

                try:
                    downloaded = client.download_engagement_file(
                        audit_id=engagement_id,
                        request_id=request_id,
                        file_id=file_id,
                        fallback_filename=file_name,
                    )

                    out_path = save_bytes(f"downloads/{downloaded.filename}", downloaded.data)
                    print(f"Downloaded -> {out_path}")
                    print(f"Content-Type: {downloaded.content_type}")
                    print(f"Size (bytes): {len(downloaded.data)}")

                    result = upload_bytes_to_box(
                        box_client,
                        folder_id=folder_id,
                        file_name=downloaded.filename,
                        content=downloaded.data,
                    )

                    print("Upload complete")
                    print(f"Box File Name: {result.file_name}")
                    print(f"Box File ID: {result.file_id}")

                    tracker.mark_synced(
                        suralink_file_id=file_id,
                        engagement_id=engagement_id,
                        request_id=request_id,
                        file_name=downloaded.filename,
                        box_file_id=result.file_id,
                        box_folder_id=folder_id,
                    )

                    uploaded_count += 1

                except Exception as e:
                    error_text = str(e)
                    if "item_name_in_use" in error_text or "same name already exists" in error_text:
                        skipped_existing_box_count += 1
                        print("Skipped -> file already exists in Box")
                        print(f"Existing file name: {file_name}")

                        tracker.mark_synced(
                            suralink_file_id=file_id,
                            engagement_id=engagement_id,
                            request_id=request_id,
                            file_name=file_name,
                            box_file_id="",
                            box_folder_id=folder_id,
                        )
                    else:
                        failed_count += 1
                        print(f"Failed -> {type(e).__name__}: {e}")

        print("\nSync summary")
        print(f"Engagements selected: {len(selected_engagements)}")
        print(f"Engagements with files: {total_engagements_with_files}")
        print(f"Total files found: {total_files_found}")
        print(f"Uploaded successfully: {uploaded_count}")
        print(f"Skipped (already existed in Box this run): {skipped_existing_box_count}")
        print(f"Skipped (already tracked from previous runs): {skipped_tracked_count}")
        print(f"Failed: {failed_count}")

    finally:
        client.close()
        tracker.close()


if __name__ == "__main__":
    main()
