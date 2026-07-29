#!/usr/bin/env python3
"""
Sort the children of a specific Docmost page (branch) alphabetically by title.

Reuses the Docmost API helpers from import_wiki.py (docmost_request, get_space,
list_pages, find_page_by_title) -- run `pip install -r requirements.txt` first,
same as for import_wiki.py.

Configuration via .env (same file as import_wiki.py):
    DOCMOST_URL         e.g. http://localhost:3000
    DOCMOST_API_KEY     Settings -> API keys (under Account) -> Create API Key
    SORT_BRANCH_PATH    "Space/Vendors" or "Space/Vendors/SubFolder"
                        (use just "Space" to sort the space's root pages)
    SORT_RECURSIVE      "true"/"false" (default: false) -- also sort every
                        nested sub-branch under the target branch
"""
import os
import sys

from dotenv import load_dotenv

from import_wiki import (
    docmost_request,
    get_space,
    list_pages,
    find_page_by_title,
)

load_dotenv()

SORT_BRANCH_PATH = os.getenv("SORT_BRANCH_PATH")
SORT_RECURSIVE = os.getenv("SORT_RECURSIVE", "false").strip().lower() not in ("0", "false", "no")


def resolve_existing_path(dest_path):
    """Parse 'Space/Page1/Page2' -> (space_id, page_id or None for space root).
    Unlike import_wiki's resolve_destination_path, this does NOT create
    missing pages -- it errors out if any segment doesn't exist."""
    parts = [p.strip() for p in dest_path.strip("/").split("/") if p.strip()]
    if not parts:
        print("ERROR: SORT_BRANCH_PATH cannot be empty.")
        sys.exit(1)

    space = get_space(parts[0])
    space_id = space["id"]

    current_parent_id = None
    for title in parts[1:]:
        child_pages = list_pages(space_id, current_parent_id)
        found = find_page_by_title(child_pages, title)
        if not found:
            print(f"ERROR: page '{title}' not found (path: {dest_path}).")
            sys.exit(1)
        current_parent_id = found["id"]

    return space_id, current_parent_id


def sort_children(space_id, parent_page_id, recursive=False):
    """Sort the direct children of parent_page_id (or the space root if
    parent_page_id is None) alphabetically by title, then optionally recurse
    into every child that itself has children."""
    children = list_pages(space_id, parent_page_id)
    label = parent_page_id or "<space root>"

    if len(children) > 1:
        # Reuse the existing (already-valid) position values, just reassigned
        # in title order -- avoids re-implementing fractional-indexing key
        # generation, and can't produce an invalid position.
        positions_sorted = sorted(p["position"] for p in children)
        pages_by_title = sorted(children, key=lambda p: (p.get("title") or "").casefold())

        print(f"Sorting {len(pages_by_title)} page(s) under {label}...")
        for position, page in zip(positions_sorted, pages_by_title):
            if page["position"] != position:
                docmost_request("POST", "/api/pages/move", {
                    "pageId": page["id"],
                    "parentPageId": parent_page_id,
                    "position": position,
                })
            print(f"  {page.get('title')}")
    else:
        print(f"Nothing to sort under {label} ({len(children)} page(s)).")

    if recursive:
        for page in children:
            if page.get("hasChildren"):
                sort_children(space_id, page["id"], recursive=True)


def main():
    if not SORT_BRANCH_PATH:
        print("ERROR: SORT_BRANCH_PATH is not set in .env")
        sys.exit(1)

    space_id, parent_page_id = resolve_existing_path(SORT_BRANCH_PATH)
    sort_children(space_id, parent_page_id, recursive=SORT_RECURSIVE)
    print("Done.")


if __name__ == "__main__":
    main()
