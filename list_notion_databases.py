"""Utility script for inspecting Notion databases and their data sources.

Usage:
    python list_notion_databases.py <database_id> [<database_id> ...]

If no command-line argument is provided, the script looks for a
NOTION_DATABASE_IDS environment variable containing a comma-separated list of
IDs. At least one database ID must be supplied from one of those sources.

For each database, the script prints the database title and ID, followed by the
associated data sources (name and ID). This helps verify the new data_source
concept introduced in the 2025-09-03 API version.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Iterable

from dotenv import load_dotenv
from notion_client import Client as NotionClient

load_dotenv()

NOTION_TOKEN = os.getenv("NOTION_TOKEN")


def _rich_text_to_plain_text(rich_text: Iterable[dict[str, Any]]) -> str:
    """Concatenate the plain text content from a list of rich text objects."""

    return "".join((block.get("plain_text", "") for block in rich_text))


def _resolve_database_ids() -> list[str]:
    """Determine which database IDs to inspect."""

    if len(sys.argv) > 1:
        return [arg.strip() for arg in sys.argv[1:] if arg.strip()]

    env_value = os.getenv("NOTION_DATABASE_IDS", "")
    candidates = [item.strip() for item in env_value.split(",") if item.strip()]
    if candidates:
        return candidates

    raise SystemExit(
        "Provide database IDs as command-line arguments or set NOTION_DATABASE_IDS."
    )


def _print_database_info(notion: NotionClient, database_id: str) -> None:
    """Fetch and print database and data source information."""

    database = notion.databases.retrieve(database_id)
    title = _rich_text_to_plain_text(database.get("title", [])) or "Untitled"
    print(f"Database Title: {title}")
    print(f"Database ID: {database_id}")

    data_sources = database.get("data_sources", [])
    if not data_sources:
        print("  (No data sources found)")
        print("-" * 40)
        return

    for data_source in data_sources:
        data_source_id = data_source.get("id")
        if not data_source_id:
            continue

        # Retrieve full data source details to access the rich text title
        data_source_detail = notion.data_sources.retrieve(data_source_id)
        data_source_title = (
            _rich_text_to_plain_text(data_source_detail.get("title", []))
            or data_source.get("name")
            or "Untitled data source"
        )
        print(f"  - Data Source Name: {data_source_title}")
        print(f"    Data Source ID: {data_source_id}")
    print("-" * 40)


def main() -> None:
    if not NOTION_TOKEN:
        raise SystemExit("NOTION_TOKEN is required.")

    notion = NotionClient(auth=NOTION_TOKEN)
    database_ids = _resolve_database_ids()

    for database_id in database_ids:
        _print_database_info(notion, database_id)


if __name__ == "__main__":
    main()
