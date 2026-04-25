import os
from datetime import datetime, timezone
from typing import Any, Optional

import requests


NOTION_API_BASE = "https://api.notion.com/v1"
DEFAULT_NOTION_VERSION = "2022-06-28"

STATUS_TRIGGER = "\ubc1c\ud589"
STATUS_PROCESSING = "\ubc1c\ud589\uc911"
STATUS_DRAFT_READY = "\ucd08\uc548\uc644\ub8cc"
STATUS_PUBLISHED = "\ubc1c\ud589\uc644\ub8cc"
STATUS_FAILED = "\uc2e4\ud328"


class NotionManager:
    def __init__(
        self,
        api_key: Optional[str] = None,
        database_id: Optional[str] = None,
        notion_version: Optional[str] = None,
    ):
        self.api_key = api_key or os.getenv("NOTION_API_KEY")
        self.database_id = database_id or os.getenv("NOTION_DATABASE_ID")
        self.notion_version = notion_version or os.getenv(
            "NOTION_VERSION", DEFAULT_NOTION_VERSION
        )
        if not self.api_key:
            raise ValueError("NOTION_API_KEY is not set.")
        if not self.database_id:
            raise ValueError("NOTION_DATABASE_ID is not set.")

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Notion-Version": self.notion_version,
                "Content-Type": "application/json",
            }
        )
        self.schema = self.retrieve_database()["properties"]
        self.fields = self._resolve_fields(self.schema)

    def retrieve_database(self) -> dict[str, Any]:
        res = self.session.get(f"{NOTION_API_BASE}/databases/{self.database_id}")
        if res.status_code == 404:
            raise RuntimeError(
                "Notion database was not found. Share it with the integration."
            )
        self._raise_for_response(res)
        return res.json()

    def query_candidate_pages(
        self,
        mode: str = "status",
        trigger_status: str = STATUS_TRIGGER,
        page_size: int = 20,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"page_size": page_size}
        filters = []

        url_field = self.fields.get("url")
        if url_field:
            url_type = self.schema[url_field]["type"]
            if url_type == "url":
                filters.append({"property": url_field, "url": {"is_not_empty": True}})
            elif url_type in {"rich_text", "title"}:
                filters.append(
                    {"property": url_field, url_type: {"is_not_empty": True}}
                )

        status_field = self.fields.get("status")
        if mode == "status":
            if not status_field:
                raise RuntimeError("Status trigger mode needs a Status/Select property.")
            status_type = self.schema[status_field]["type"]
            filters.append(
                {"property": status_field, status_type: {"equals": trigger_status}}
            )

        if len(filters) == 1:
            payload["filter"] = filters[0]
        elif len(filters) > 1:
            payload["filter"] = {"and": filters}

        pages = []
        start_cursor = None
        while True:
            if start_cursor:
                payload["start_cursor"] = start_cursor
            res = self.session.post(
                f"{NOTION_API_BASE}/databases/{self.database_id}/query",
                json=payload,
            )
            self._raise_for_response(res)
            data = res.json()
            pages.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            start_cursor = data.get("next_cursor")

        return [
            page
            for page in pages
            if not self.get_thread_id(page)
            and self.get_status(page) not in {STATUS_PROCESSING, STATUS_PUBLISHED}
        ]

    def get_url(self, page: dict[str, Any]) -> str:
        url_field = self.fields.get("url")
        if not url_field:
            return _find_url_in_text(self.get_title(page))

        prop = page["properties"].get(url_field, {})
        prop_type = prop.get("type")
        if prop_type == "url":
            return prop.get("url") or ""
        if prop_type == "rich_text":
            return _find_url_in_text(_plain_text(prop.get("rich_text", [])))
        if prop_type == "title":
            return _find_url_in_text(_plain_text(prop.get("title", [])))
        return ""

    def get_title(self, page: dict[str, Any]) -> str:
        title_field = self.fields.get("title")
        if not title_field:
            return ""
        return _plain_text(page["properties"].get(title_field, {}).get("title", []))

    def get_thread_id(self, page: dict[str, Any]) -> str:
        field = self.fields.get("thread_id")
        if not field:
            return ""
        prop = page["properties"].get(field, {})
        if prop.get("type") == "rich_text":
            return _plain_text(prop.get("rich_text", []))
        return ""

    def get_status(self, page: dict[str, Any]) -> str:
        field = self.fields.get("status")
        if not field:
            return ""
        prop = page["properties"].get(field, {})
        if prop.get("type") == "status":
            status = prop.get("status")
            return status.get("name", "") if status else ""
        if prop.get("type") == "select":
            select = prop.get("select")
            return select.get("name", "") if select else ""
        return ""

    def set_processing(self, page_id: str) -> None:
        self.update_page(page_id, status=STATUS_PROCESSING, last_error="")

    def set_failed(self, page_id: str, message: str) -> None:
        self.update_page(page_id, status=STATUS_FAILED, last_error=message[:1800])

    def set_published(self, page_id: str, thread_id: str) -> None:
        self.update_page(
            page_id,
            status=STATUS_PUBLISHED,
            thread_id=thread_id,
            published_at=datetime.now(timezone.utc).isoformat(),
            last_error="",
        )

    def update_article_result(
        self,
        page_id: str,
        title: str,
        source_url: str,
        summary: str,
        analysis: str,
        final_post: str,
    ) -> None:
        self.update_page(
            page_id,
            title=title,
            url=source_url,
            summary=summary,
            analysis=analysis,
            final_post=final_post,
        )

    def update_page(self, page_id: str, **values: Any) -> None:
        properties = {}
        for field_key, value in values.items():
            field = self.fields.get(field_key)
            if not field or value is None:
                continue
            prop_type = self.schema[field]["type"]
            properties[field] = _to_property_value(prop_type, value)

        if not properties:
            return

        res = self.session.patch(
            f"{NOTION_API_BASE}/pages/{page_id}",
            json={"properties": properties},
        )
        self._raise_for_response(res)

    def _resolve_fields(self, schema: dict[str, Any]) -> dict[str, str]:
        fields = {
            "title": _first_by_type(schema, "title"),
            "url": _find_property(
                schema,
                ["URL", "Url", "Link", "\ub9c1\ud06c", "\uc6d0\ubb38"],
                ["url", "rich_text", "title"],
            ),
            "status": _find_property(
                schema,
                ["Status", "\uc0c1\ud0dc", "Publish Status"],
                ["status", "select"],
            ),
            "summary": _find_property(
                schema,
                ["Summary", "AI Summary", "\uc694\uc57d"],
                ["rich_text"],
            ),
            "analysis": _find_property(
                schema,
                ["Analysis", "AI Analysis", "\ubd84\uc11d", "\ub0b4 \uc758\uacac"],
                ["rich_text"],
            ),
            "final_post": _find_property(
                schema,
                ["Threads Post", "Final Post", "\uc2a4\ub808\ub4dc \ubb38\uc548"],
                ["rich_text"],
            ),
            "thread_id": _find_property(
                schema,
                ["Thread Post ID", "Threads ID", "\uc2a4\ub808\ub4dc ID"],
                ["rich_text"],
            ),
            "published_at": _find_property(
                schema,
                ["Published At", "\ubc1c\ud589\uc77c", "\ubc1c\ud589 \uc2dc\uac04"],
                ["date"],
            ),
            "last_error": _find_property(
                schema,
                ["Last Error", "Error", "\uc624\ub958", "\uc2e4\ud328 \uc0ac\uc720"],
                ["rich_text"],
            ),
        }
        return {key: value for key, value in fields.items() if value}

    @staticmethod
    def _raise_for_response(res: requests.Response) -> None:
        if res.ok:
            return
        try:
            detail = res.json()
            message = detail.get("message") or str(detail)
        except ValueError:
            message = res.text
        raise RuntimeError(f"Notion API error {res.status_code}: {message}")


def _find_property(
    schema: dict[str, Any], preferred_names: list[str], allowed_types: list[str]
) -> Optional[str]:
    lower_map = {name.lower(): name for name in schema}
    for preferred in preferred_names:
        name = lower_map.get(preferred.lower())
        if name and schema[name]["type"] in allowed_types:
            return name

    for name, prop in schema.items():
        if prop["type"] in allowed_types and any(
            token.lower() in name.lower() for token in preferred_names
        ):
            return name

    for name, prop in schema.items():
        if prop["type"] in allowed_types:
            return name
    return None


def _first_by_type(schema: dict[str, Any], prop_type: str) -> Optional[str]:
    for name, prop in schema.items():
        if prop["type"] == prop_type:
            return name
    return None


def _to_property_value(prop_type: str, value: Any) -> dict[str, Any]:
    text = "" if value is None else str(value)
    if prop_type == "title":
        return {"title": _rich_text_chunks(text[:1800])}
    if prop_type == "rich_text":
        return {"rich_text": _rich_text_chunks(text)}
    if prop_type == "url":
        return {"url": text or None}
    if prop_type == "date":
        return {"date": {"start": text} if text else None}
    if prop_type == "status":
        return {"status": {"name": text}}
    if prop_type == "select":
        return {"select": {"name": text}}
    return {"rich_text": _rich_text_chunks(text)}


def _rich_text_chunks(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    return [
        {"type": "text", "text": {"content": text[index : index + 1800]}}
        for index in range(0, len(text), 1800)
    ]


def _plain_text(rich_text_items: list[dict[str, Any]]) -> str:
    return "".join(item.get("plain_text", "") for item in rich_text_items).strip()


def _find_url_in_text(value: str) -> str:
    import re

    match = re.search(r"https?://\S+", value or "")
    return match.group(0).rstrip(").,]") if match else ""
