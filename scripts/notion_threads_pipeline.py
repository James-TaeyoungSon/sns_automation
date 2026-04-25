import argparse
import os
import sys
from dataclasses import asdict

from article_reader import discover_url_from_title, fetch_article
from llm_processor import generate_article_threads_content
from notion_manager import NotionManager, STATUS_DRAFT_READY, STATUS_TRIGGER
from threads_publisher import post_text_to_threads


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish Notion URL rows to Threads with AI analysis."
    )
    parser.add_argument(
        "--mode",
        choices=["status", "auto"],
        default=os.getenv("NOTION_PUBLISH_MODE", "status"),
        help="status: only rows whose status matches trigger. auto: any unposted row with URL.",
    )
    parser.add_argument(
        "--trigger-status",
        default=os.getenv("NOTION_TRIGGER_STATUS", STATUS_TRIGGER),
        help="Notion status/select value that triggers publishing in status mode.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=int(os.getenv("NOTION_BATCH_LIMIT", "5")),
        help="Maximum Notion rows to process.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and save AI content, but do not publish to Threads.",
    )
    args = parser.parse_args()

    try:
        notion = NotionManager()
    except Exception as exc:
        print(f"Could not initialize Notion: {exc}")
        sys.exit(1)

    pages = notion.query_candidate_pages(
        mode=args.mode,
        trigger_status=args.trigger_status,
        page_size=args.limit,
    )

    if not pages:
        print("No Notion rows to process.")
        return

    print(f"Found {len(pages)} Notion row(s) to process.")
    for page in pages:
        process_page(notion, page, dry_run=args.dry_run)


def process_page(notion: NotionManager, page: dict, dry_run: bool = False) -> None:
    page_id = page["id"]
    source_url = notion.get_url(page)
    if not source_url:
        title = notion.get_title(page)
        print(f"URL is missing. Trying to discover URL from title: {title[:80]}")
        source_url = discover_url_from_title(title)
        if source_url:
            notion.update_page(page_id, url=source_url)
            print(f"Discovered URL: {source_url}")
        else:
            message = (
                "URL property is empty and no reliable article URL could be "
                "discovered from the title."
            )
            if not dry_run:
                notion.set_failed(page_id, message)
            print(f"Skipped {page_id}: {message}")
            return

    try:
        if not dry_run:
            notion.set_processing(page_id)

        print(f"Reading article: {source_url}")
        article = fetch_article(source_url)

        print("Generating AI analysis and Threads post.")
        generated = generate_article_threads_content(asdict(article))
        final_post = fit_threads_limit(generated["threads_post"], source_url)

        notion.update_article_result(
            page_id=page_id,
            title=article.title,
            source_url=source_url,
            summary=generated["summary"],
            analysis=generated["analysis"],
            final_post=final_post,
        )

        if dry_run:
            notion.update_page(page_id, status=STATUS_DRAFT_READY, last_error="")
            print(f"Dry run complete for: {article.title}")
            return

        print("Publishing to Threads.")
        thread_id = post_text_to_threads(final_post, link_url=source_url)
        notion.set_published(page_id, thread_id)
        print(f"Published to Threads: {thread_id}")
    except Exception as exc:
        message = str(exc)
        notion.set_failed(page_id, message)
        print(f"Failed {page_id}: {message}")


def fit_threads_limit(text: str, source_url: str, limit: int = 500) -> str:
    text = (text or "").strip()
    if source_url and source_url not in text:
        suffix = f"\n\n원문: {source_url}"
    else:
        suffix = ""

    candidate = f"{text}{suffix}"
    if len(candidate) <= limit:
        return candidate

    room = limit - len(suffix) - 3
    if room <= 80:
        return candidate[:limit]
    return f"{text[:room].rstrip()}...{suffix}"


if __name__ == "__main__":
    main()
