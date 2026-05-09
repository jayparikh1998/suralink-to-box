from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile
import html
import re

import httpx

from suralink_to_box.main import (
    SyncOverrides,
    _fetch_all_requests,
    _pick_client_id,
    _pick_client_name,
    _pick_id,
    _pick_name,
    _resolve_category_folder_name,
    _resolve_request_folder_name,
    _settings_with_overrides,
)
from suralink_to_box.suralink_client import SuralinkClient


@dataclass(frozen=True)
class CommentRecord:
    author: str
    created_at: str
    text: str


@dataclass(frozen=True)
class RequestCommentGroup:
    category_name: str
    request_name: str
    request_id: str
    comments: tuple[CommentRecord, ...]


@dataclass(frozen=True)
class EngagementCommentGroup:
    engagement_name: str
    engagement_id: str
    requests: tuple[RequestCommentGroup, ...]


@dataclass(frozen=True)
class CommentsExportResult:
    filename: str
    docx_bytes: bytes
    engagement_count: int
    request_count: int
    comment_count: int


def _safe_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[ \t]+", " ", text).strip()


def _nested_text(payload: object, *paths: tuple[str, ...]) -> str:
    for path in paths:
        current = payload
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if isinstance(current, (dict, list)):
            continue
        text = _safe_text(current)
        if text:
            return text
    return ""


def _extract_author(payload: dict) -> str:
    direct = _nested_text(
        payload,
        ("author",),
        ("authorName",),
        ("createdByName",),
        ("userName",),
        ("displayName",),
        ("name",),
        ("createdBy", "name"),
        ("createdBy", "displayName"),
        ("createdBy", "email"),
        ("user", "name"),
        ("user", "displayName"),
        ("user", "email"),
        ("actor", "name"),
        ("actor", "displayName"),
        ("actor", "email"),
    )
    if direct:
        return direct

    first = _nested_text(
        payload,
        ("userFirstName",),
        ("createdBy", "firstName"),
        ("user", "firstName"),
    )
    last = _nested_text(
        payload,
        ("userLastName",),
        ("createdBy", "lastName"),
        ("user", "lastName"),
    )
    return " ".join(part for part in (first, last) if part) or "Unknown author"


def _extract_created_at(payload: dict) -> str:
    return _nested_text(
        payload,
        ("createdAt",),
        ("createdDate",),
        ("dateCreated",),
        ("timestamp",),
        ("ts",),
        ("activityDate",),
        ("modifiedAt",),
        ("updatedAt",),
    )


def _extract_comment_text(payload: dict) -> str:
    return _nested_text(
        payload,
        ("comment", "text"),
        ("comment", "body"),
        ("comment", "message"),
        ("comment", "content"),
        ("commentText",),
        ("body",),
        ("text",),
        ("note",),
        ("comment",),
    )


def _looks_like_comment_event(payload: dict) -> bool:
    for key, value in payload.items():
        lowered_key = str(key).lower()
        if "comment" in lowered_key:
            return True
        if lowered_key in {"eventtype", "event_type", "type", "action", "name"}:
            if "comment" in str(value).lower():
                return True
    return False


def _walk_comment_candidates(payload: object, *, under_comment_key: bool = False):
    if isinstance(payload, list):
        for item in payload:
            yield from _walk_comment_candidates(item, under_comment_key=under_comment_key)
        return

    if not isinstance(payload, dict):
        return

    if under_comment_key or _looks_like_comment_event(payload):
        yield payload

    for key, value in payload.items():
        lowered = str(key).lower()
        child_under_comment_key = under_comment_key or "comment" in lowered
        if isinstance(value, (dict, list)):
            yield from _walk_comment_candidates(value, under_comment_key=child_under_comment_key)


def _extract_comments(payloads: list[object]) -> list[CommentRecord]:
    comments: list[CommentRecord] = []
    seen: set[tuple[str, str, str]] = set()

    for payload in payloads:
        for candidate in _walk_comment_candidates(payload):
            text = _extract_comment_text(candidate)
            if not text:
                continue

            record = CommentRecord(
                author=_extract_author(candidate),
                created_at=_extract_created_at(candidate),
                text=text,
            )
            key = (record.author.lower(), record.created_at, record.text)
            if key in seen:
                continue
            seen.add(key)
            comments.append(record)

    return comments


def _get_optional_json(client: SuralinkClient, path: str):
    try:
        return client.get_json(path)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {400, 403, 404, 405}:
            return None
        raise


def _request_comment_payloads(
    client: SuralinkClient,
    *,
    engagement_id: str,
    request_id: str,
) -> list[object]:
    return client.list_request_history(
        request_id,
        request_list_id=engagement_id,
    )


def _list_selected_engagements(client: SuralinkClient, settings) -> list[dict]:
    engagement_id = _safe_text(getattr(settings, "suralink_engagement_id", None))
    engagement_name = _safe_text(getattr(settings, "suralink_engagement_name", None))
    customer_name = _safe_text(getattr(settings, "suralink_customer_name", None))
    customer_custom_id = _safe_text(getattr(settings, "suralink_customer_custom_id", None))
    client_id = _safe_text(getattr(settings, "suralink_client_id", None))

    if engagement_id:
        for engagement in client.list_all_engagements():
            if _pick_id(engagement, ["id"]) == engagement_id:
                return [engagement]
        return [{"id": engagement_id, "name": f"engagement_{engagement_id}"}]

    if client_id:
        engagements = client.list_client_engagements(client_id)
    elif customer_custom_id:
        engagements = client.list_engagements_by_custom_id(customer_custom_id)
    elif customer_name:
        engagements = []
        for customer in client.list_all_clients():
            if (_pick_client_name(customer) or "").strip().lower() != customer_name.lower():
                continue
            matched_client_id = _pick_client_id(customer)
            if matched_client_id:
                engagements.extend(client.list_client_engagements(matched_client_id))
    else:
        engagements = client.list_engagements()

    if engagement_name:
        target = engagement_name.lower()
        engagements = [
            engagement
            for engagement in engagements
            if _pick_name(engagement).strip().lower() == target
        ]

    return engagements


def collect_suralink_comments(
    *,
    overrides: SyncOverrides | None = None,
) -> list[EngagementCommentGroup]:
    settings = _settings_with_overrides(overrides)
    client = SuralinkClient(settings)
    try:
        engagement_groups: list[EngagementCommentGroup] = []
        for engagement in _list_selected_engagements(client, settings):
            engagement_id = _pick_id(engagement, ["id"])
            if not engagement_id:
                continue

            request_groups: list[RequestCommentGroup] = []
            requests = _fetch_all_requests(client, engagement_id)
            for request in requests:
                request_id = _pick_id(request, ["id"])
                if not request_id:
                    continue

                payloads = _request_comment_payloads(
                    client,
                    engagement_id=engagement_id,
                    request_id=request_id,
                )
                comments = _extract_comments([request, *payloads])
                if not comments:
                    continue

                request_groups.append(
                    RequestCommentGroup(
                        category_name=_resolve_category_folder_name(request),
                        request_name=_resolve_request_folder_name(request, request_id),
                        request_id=request_id,
                        comments=tuple(comments),
                    )
                )

            if request_groups:
                engagement_groups.append(
                    EngagementCommentGroup(
                        engagement_name=_pick_name(engagement),
                        engagement_id=engagement_id,
                        requests=tuple(request_groups),
                    )
                )

        return engagement_groups
    finally:
        client.close()


def collect_suralink_comments_for_engagements(
    client: SuralinkClient,
    *,
    engagements: list[dict],
) -> list[EngagementCommentGroup]:
    engagement_groups: list[EngagementCommentGroup] = []
    for engagement in engagements:
        engagement_id = _pick_id(engagement, ["id"])
        if not engagement_id:
            continue

        request_groups: list[RequestCommentGroup] = []
        requests = _fetch_all_requests(client, engagement_id)
        for request in requests:
            request_id = _pick_id(request, ["id"])
            if not request_id:
                continue

            payloads = _request_comment_payloads(
                client,
                engagement_id=engagement_id,
                request_id=request_id,
            )
            comments = _extract_comments([request, *payloads])
            if not comments:
                continue

            request_groups.append(
                RequestCommentGroup(
                    category_name=_resolve_category_folder_name(request),
                    request_name=_resolve_request_folder_name(request, request_id),
                    request_id=request_id,
                    comments=tuple(comments),
                )
            )

        if request_groups:
            engagement_groups.append(
                EngagementCommentGroup(
                    engagement_name=_pick_name(engagement),
                    engagement_id=engagement_id,
                    requests=tuple(request_groups),
                )
            )

    return engagement_groups


def collect_suralink_comments_for_requests(
    client: SuralinkClient,
    *,
    engagement: dict,
    requests: list[dict],
) -> EngagementCommentGroup | None:
    engagement_id = _pick_id(engagement, ["id"])
    if not engagement_id:
        return None

    request_groups: list[RequestCommentGroup] = []
    for request in requests:
        request_id = _pick_id(request, ["id"])
        if not request_id:
            continue

        payloads = _request_comment_payloads(
            client,
            engagement_id=engagement_id,
            request_id=request_id,
        )
        comments = _extract_comments([request, *payloads])
        if not comments:
            continue

        request_groups.append(
            RequestCommentGroup(
                category_name=_resolve_category_folder_name(request),
                request_name=_resolve_request_folder_name(request, request_id),
                request_id=request_id,
                comments=tuple(comments),
            )
        )

    if not request_groups:
        return None

    return EngagementCommentGroup(
        engagement_name=_pick_name(engagement),
        engagement_id=engagement_id,
        requests=tuple(request_groups),
    )


def _xml_text(text: str) -> str:
    return html.escape(text, quote=False)


def _paragraph(text: str = "", *, style: str | None = None, bold: bool = False) -> str:
    style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    bold_xml = "<w:rPr><w:b/></w:rPr>" if bold else ""
    lines = _safe_text(text).split("\n") or [""]
    runs = []
    for index, line in enumerate(lines):
        if index:
            runs.append("<w:br/>")
        runs.append(f"<w:t>{_xml_text(line)}</w:t>")
    return f"<w:p>{style_xml}<w:r>{bold_xml}{''.join(runs)}</w:r></w:p>"


def _document_xml(groups: list[EngagementCommentGroup], *, title: str) -> str:
    body: list[str] = [
        _paragraph(title, style="Title"),
        _paragraph(f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}", style="Subtitle"),
    ]

    if not groups:
        body.append(_paragraph("No Suralink comments were found for the selected source."))
    else:
        for engagement in groups:
            body.append(
                _paragraph(
                    f"{engagement.engagement_name} (Engagement ID: {engagement.engagement_id})",
                    style="Heading1",
                )
            )
            current_category = None
            for request in engagement.requests:
                if request.category_name != current_category:
                    current_category = request.category_name
                    body.append(_paragraph(current_category, style="Heading2"))

                body.append(
                    _paragraph(
                        f"{request.request_name} (Request ID: {request.request_id})",
                        style="Heading3",
                    )
                )
                for comment in request.comments:
                    meta = " | ".join(
                        part for part in (comment.author, comment.created_at) if part
                    )
                    if meta:
                        body.append(_paragraph(meta, bold=True))
                    body.append(_paragraph(comment.text))

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body)}"
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
        "</w:body></w:document>"
    )


def _styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/><w:rPr><w:color w:val="666666"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:rPr><w:b/><w:sz w:val="22"/></w:rPr></w:style>
</w:styles>"""


def _build_docx_bytes(groups: list[EngagementCommentGroup], *, title: str) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as docx:
        docx.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>""",
        )
        docx.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""",
        )
        docx.writestr(
            "word/_rels/document.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>""",
        )
        docx.writestr("word/document.xml", _document_xml(groups, title=title))
        docx.writestr("word/styles.xml", _styles_xml())
    return buffer.getvalue()


def build_suralink_comments_docx(
    *,
    overrides: SyncOverrides | None = None,
) -> CommentsExportResult:
    groups = collect_suralink_comments(overrides=overrides)
    return build_suralink_comments_docx_from_groups(groups)


def build_suralink_comments_docx_from_groups(
    groups: list[EngagementCommentGroup],
    *,
    filename: str | None = None,
) -> CommentsExportResult:
    comment_count = sum(len(request.comments) for group in groups for request in group.requests)
    request_count = sum(len(group.requests) for group in groups)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    resolved_filename = filename or f"suralink_comments_{timestamp}.docx"
    return CommentsExportResult(
        filename=resolved_filename,
        docx_bytes=_build_docx_bytes(groups, title="Suralink Comments Export"),
        engagement_count=len(groups),
        request_count=request_count,
        comment_count=comment_count,
    )
