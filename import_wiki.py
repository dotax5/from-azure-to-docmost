#!/usr/bin/env python3
"""
Import Azure DevOps Wiki pages into Docmost
"""
import re
import urllib.parse
import os
import sys
from dotenv import load_dotenv
import requests

load_dotenv()
# ============================================================
# CONFIGURATION (edit these before running)
# ============================================================

# Azure DevOps
AZURE_ORG = os.getenv("AZURE_ORG")
AZURE_PROJECT = os.getenv("AZURE_PROJECT")
AZURE_WIKI_REPO = os.getenv("AZURE_WIKI_REPO")
# Create PAT: https://dev.azure.com/{org}/_usersSettings/tokens (Code -> Read)
AZURE_PAT = os.getenv("AZURE_PAT")

# Docmost
DOCMOST_URL = os.getenv("DOCMOST_URL")
# Create in Docmost: Settings -> API keys (under Account) -> Create API Key
DOCMOST_API_KEY = os.getenv("DOCMOST_API_KEY")

# Paths
WIKI_SOURCE_PATH = os.getenv("WIKI_SOURCE_PATH")
WIKI_SOURCE_PATH = WIKI_SOURCE_PATH.replace(" ", "-")
WIKI_DEST_PATH = os.getenv("WIKI_DEST_PATH")
# ============================================================
# Azure DevOps API
# ============================================================

AZURE_BASE = f"https://{AZURE_ORG}.visualstudio.com/{AZURE_PROJECT}/_apis"
AZURE_GIT = f"{AZURE_BASE}/git/repositories/{AZURE_WIKI_REPO}"


def azure_request(path, params=None):
    url = f"{AZURE_GIT}/{path}"
    headers = {"Accept": "application/json"}
    auth = ("", AZURE_PAT)
    resp = requests.get(url, headers=headers, auth=auth, params=params, timeout=30)
    if resp.status_code == 203:
        resp = requests.get(
            resp.headers.get("Location", url),
            headers=headers,
            auth=auth,
            params=params,
            timeout=30,
        )
    if resp.status_code == 401:
        print("ERROR: Azure PAT is invalid or expired. Create a new token and update AZURE_PAT.")
        sys.exit(1)
    resp.raise_for_status()
    return resp.json()


def fetch_wiki_tree(path_prefix=""):
    """Fetch all files from Azure DevOps Wiki repo under given path.
    Returns (md_files_list, all_files_dict) where all_files_dict maps
    absolute path -> item metadata for every blob (including images, PDFs, etc.)."""
    scope = path_prefix or "/"
    try:
        data = azure_request("items", {
            "scopePath": scope,
            "recursionLevel": "Full",
            "api-version": "6.0",
        })
    except requests.exceptions.HTTPError:
        scope = scope.replace("-", "%2D")
        data = azure_request("items", {
            "scopePath": scope,
            "recursionLevel": "Full",
            "api-version": "6.0",
        })
    items = data.get("value", [])
    md_files = []
    all_files = {}
    for item in items:
        if item.get("gitObjectType") != "blob":
            continue
        path = urllib.parse.unquote(item["path"])
        if path.endswith(".md"):
            md_files.append(path)
        else:
            all_files[path] = item
    return sorted(set(md_files)), all_files


def fetch_file_content(file_path):
    try:
        data = azure_request("items", {
            "path": file_path,
            "includeContent": "true",
            "api-version": "6.0",
        })
    except requests.exceptions.HTTPError:
        data = azure_request("items", {
            "path": file_path.replace("-", "%2D"),
            "includeContent": "true",
            "api-version": "6.0",
        })
    return data.get("content", "")


def stream_azure_file(file_path):
    """Stream a binary file from Azure DevOps repo.
    Returns a requests.Response with stream=True. Use resp.raw as a file-like object.
    If the first request fails with 404, retries with hyphens encoded as %2D."""
    url = f"{AZURE_GIT}/items"
    headers = {"Accept": "application/octet-stream"}
    auth = ("", AZURE_PAT)

    def _build_url(encoded):
        if not encoded:
            return url, {"path": file_path, "download": "true", "api-version": "6.0"}
        # Manual URL build to avoid requests double-encoding %2D
        p = urllib.parse.quote(file_path, safe='/').replace('-', '%2D')
        return f"{url}?download=true&api-version=6.0&path={p}", None

    req_url, req_params = _build_url(encoded=False)
    resp = requests.get(req_url, headers=headers, auth=auth,
                        params=req_params, timeout=30, stream=True)

    if resp.status_code == 401:
        resp.close()
        print("ERROR: Azure PAT is invalid or expired.")
        sys.exit(1)

    if not resp.ok:
        resp.close()
        req_url, _ = _build_url(encoded=True)
        resp = requests.get(req_url, headers=headers, auth=auth,
                            timeout=30, stream=True)
        if resp.status_code == 401:
            resp.close()
            print("ERROR: Azure PAT is invalid or expired.")
            sys.exit(1)
        resp.raise_for_status()

    return resp


def upload_to_docmost(page_id, file_name, azure_response):
    """Upload a file to Docmost as a page attachment, streaming from Azure.
    azure_response is a requests.Response with stream=True (resp.raw will be read)."""
    url = f"{DOCMOST_URL.rstrip('/')}/api/files/upload"
    headers = {
        "Authorization": f"Bearer {DOCMOST_API_KEY}",
    }
    files = {"file": (file_name, azure_response.raw, "application/octet-stream")}
    data = {"pageId": page_id}
    resp = requests.post(url, headers=headers, files=files, data=data, timeout=60)
    if resp.status_code == 401:
        print("ERROR: Docmost auth failed.")
        sys.exit(1)
    resp.raise_for_status()
    return resp.json()


def replace_attachments(md_content, md_path, all_files, page_id):
    """Parse markdown for local file references, upload them to Docmost,
    and replace the old paths with Docmost URLs.
    Returns (updated_md, stats_dict)."""
    md_dir = os.path.dirname(md_path)
    refs = re.findall(r'\[([^\[\]]*)\]\(([^)\s]+)\)', md_content)

    seen = set()
    stats = {"uploaded": 0, "skipped": 0, "errors": 0}

    for ref_text, ref_path in refs:
        if ref_path in seen:
            continue
        seen.add(ref_path)

        if ref_path.startswith("http://") or ref_path.startswith("https://"):
            continue

        abs_path = os.path.normpath(os.path.join(md_dir, ref_path)).replace("\\", "/")

        if abs_path not in all_files:
            print(f"    SKIPPED (not found in Azure): {ref_path}")
            stats["skipped"] += 1
            continue

        file_name = os.path.basename(abs_path)
        azure_resp = None
        try:
            azure_resp = stream_azure_file(abs_path)
            attachment = upload_to_docmost(page_id, file_name, azure_resp)
        except requests.exceptions.RequestException as e:
            print(f"    ERROR with {file_name}: {e}")
            stats["errors"] += 1
            continue
        finally:
            if azure_resp is not None:
                azure_resp.close()

        attachment_id = attachment.get("id")
        if not attachment_id:
            print(f"    ERROR: no 'id' in Docmost response for {file_name}")
            stats["errors"] += 1
            continue

        docmost_url = f"/api/files/{attachment_id}/{file_name}"
        md_content = md_content.replace(f"({ref_path})", f"({docmost_url})")
        stats["uploaded"] += 1
        print(f"    Uploaded: {file_name}")

    return md_content, stats


# ============================================================
# Docmost API
# ============================================================

def docmost_request(method, path, json_body=None):
    url = f"{DOCMOST_URL.rstrip('/')}/{path.lstrip('/')}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DOCMOST_API_KEY}",
    }
    resp = requests.request(method, url, headers=headers, json=json_body, timeout=15)
    if resp.status_code == 401:
        print("ERROR: Docmost auth failed. Check DOCMOST_API_KEY (Settings > API keys).")
        sys.exit(1)
    resp.raise_for_status()
    ct = resp.headers.get("Content-Type", "")
    if "application/json" not in ct:
        return None
    payload = resp.json()
    # Docmost wraps every response in an envelope like:
    #   {"data": <actual payload>, "success": true, "status": 200}
    # Unwrap it here so every caller just works with the real payload.
    if isinstance(payload, dict) and "data" in payload and "success" in payload:
        return payload["data"]
    return payload


def get_space_by_name(name):
    """Find a space by name, return its id and slug or None."""
    data = docmost_request("POST", "/api/spaces/", {
        "page": 1, "perPage": 100,
    })
    for item in data.get("items", []):
        if item.get("name") == name or item.get("slug") == name.lower().replace(" ", "-"):
            return {"id": item["id"], "slug": item.get("slug")}
    return None

def get_space(name):
    space = get_space_by_name(name)
    if space:
        print(f"  Using existing space '{name}' (id: {space['id']})")
        return space
    print(f"Check if the space {name} exists")
    sys.exit(1)


def list_pages(space_id, parent_page_id=None):
    """List pages in a space, optionally under a parent page."""
    body = {"spaceId": space_id, "page": 1, "perPage": 500}
    if parent_page_id:
        body["pageId"] = parent_page_id
    data = docmost_request("POST", "/api/pages/sidebar-pages", body)

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            return items
        print(f"  DEBUG list_pages: unexpected response shape: {data}")
        return []
    return []


def find_page_by_title(pages, title):
    for p in pages:
        if p.get("title") == title:
            return p
    return None


def create_page(space_id, title, content, parent_page_id=None):
    """Create a page with markdown content."""
    body = {
        "title": title,
        "spaceId": space_id,
        "content": content,
        "format": "markdown",
    }
    if parent_page_id:
        body["parentPageId"] = parent_page_id
    data = docmost_request("POST", "/api/pages/create", body)

    if isinstance(data, dict) and "id" not in data:
        print(f"  DEBUG create_page response (no 'id' key): {data}")
        for key in ("page", "data"):
            inner = data.get(key)
            if isinstance(inner, dict) and "id" in inner:
                return inner
        print(f"  ERROR: /api/pages/create returned no usable 'id' for page '{title}'.")
        sys.exit(1)

    return data


def strip_source_segments(wiki_file, n_prefix_segments):
    """
    Return the path of wiki_file relative to the source folder, by dropping
    exactly n_prefix_segments leading path segments.

    We deliberately do NOT compare this against WIKI_SOURCE_PATH as a
    string: Azure DevOps already scoped the file list to that folder via
    scopePath, so every file it returns is guaranteed to live inside it.
    Segment-count stripping avoids false negatives from case differences,
    trailing slashes, or hyphen-encoding quirks in Azure's own path strings
    that a plain str.startswith() comparison could miss -- and which used
    to leave a stray leading folder (e.g. an extra "Vendors") in the
    imported structure.
    """
    segments = [s for s in wiki_file.strip("/").split("/") if s]
    remaining = segments[n_prefix_segments:]
    if not remaining:
        return None
    return "/".join(remaining)


def find_conflicting_pages(space_id, parent_page_id, files, n_prefix_segments, skip_files):
    """
    Dry-run check (read-only, creates nothing): for every file we are about
    to import, walk the destination page tree and see if a page with the
    same title already exists there. Returns a list of wiki file paths that
    would collide with an existing Docmost page.
    """
    conflicts = []
    children_cache = {}

    def get_children(pid):
        key = pid or "__root__"
        if key not in children_cache:
            children_cache[key] = list_pages(space_id, pid)
        return children_cache[key]

    for wiki_file in files:
        if wiki_file in skip_files:
            continue

        relative_path = strip_source_segments(wiki_file, n_prefix_segments)
        if relative_path is None:
            continue

        title = os.path.splitext(os.path.basename(wiki_file))[0]
        parent = parent_page_id

        rel_dir = os.path.dirname(relative_path)
        folder_missing = False
        if rel_dir:
            for part in rel_dir.replace("\\", "/").split("/"):
                found = find_page_by_title(get_children(parent), part)
                if found:
                    parent = found["id"]
                else:
                    # Intermediate folder page doesn't exist yet, so the
                    # leaf page under it can't exist either -> no conflict.
                    folder_missing = True
                    break
        if folder_missing:
            continue

        if find_page_by_title(get_children(parent), title):
            conflicts.append(wiki_file)

    return conflicts


def resolve_destination_path(dest_path):
    """Parse 'Space/Page1/Page2' -> (space_id, parent_page_id or None, path_parts)."""
    parts = [p.strip() for p in dest_path.strip("/").split("/") if p.strip()]
    if not parts:
        print("ERROR: Destination path cannot be empty.")
        sys.exit(1)

    space_name = parts[0]
    space = get_space(space_name)
    space_id = space["id"]

    current_parent_id = None
    for i in range(1, len(parts)):
        page_title = parts[i]
        child_pages = list_pages(space_id, current_parent_id)
        found = find_page_by_title(child_pages, page_title)
        if found:
            current_parent_id = found["id"]
        else:
            print(f"  Creating parent page '{page_title}'...")
            new_page = create_page(space_id, page_title, f"# {page_title}\n\n", current_parent_id)
            current_parent_id = new_page["id"]

    return space_id, current_parent_id


def import_file(space_id, parent_page_id, wiki_file_path, relative_path, all_files):
    """Import a single wiki file into Docmost."""
    title = os.path.splitext(os.path.basename(wiki_file_path))[0]
    content = fetch_file_content(wiki_file_path)
    if not content.strip():
        content = f"# {title}\n\n"

    parent = parent_page_id

    rel_dir = os.path.dirname(relative_path)
    if rel_dir:
        parts = rel_dir.replace("\\", "/").split("/")
        child_pages = list_pages(space_id, parent)
        for part in parts:
            found = find_page_by_title(child_pages, part)
            if found:
                parent = found["id"]
            else:
                new_page = create_page(space_id, part, f"# {part}\n\n", parent)
                parent = new_page["id"]
            child_pages = list_pages(space_id, parent)

    child_pages = list_pages(space_id, parent)
    existing = find_page_by_title(child_pages, title)

    if existing:
        print(f"  SKIPPED (page already exists, not overwriting): {wiki_file_path}")
        return "skipped"

    print(f"  Creating: {wiki_file_path}")
    page = create_page(space_id, title, content, parent)
    page_id = page["id"]

    updated_content, attach_stats = replace_attachments(content, wiki_file_path, all_files, page_id)

    if attach_stats["uploaded"] + attach_stats["errors"] > 0:
        print(f"    Attachments: {attach_stats['uploaded']} uploaded, "
              f"{attach_stats['skipped']} skipped, {attach_stats['errors']} errors")

    if updated_content != content:
        docmost_request("POST", "/api/pages/update", {
            "pageId": page_id,
            "content": updated_content,
            "format": "markdown",
            "operation": "replace",
        })

    return "created"


# ============================================================
# MAIN
# ============================================================

def main():

    if not WIKI_DEST_PATH:
        print("ERROR: Destination path is required.")
        sys.exit(1)

    print("\nFetching wiki file list...")
    try:
        files, all_files = fetch_wiki_tree(WIKI_SOURCE_PATH)
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Cannot connect to Azure DevOps: {e}")
        print("Check that AZURE_PAT, AZURE_ORG, AZURE_PROJECT are correct.")
        sys.exit(1)

    if not files:
        print("No .md files found in the specified path.")
        sys.exit(0)

    print(f"Found {len(files)} .md file(s).")
    print("  Sample of raw Azure paths returned (for sanity-checking):")
    for f in files[:3]:
        print(f"    {f}")

    print(f"\nConnecting to Docmost ({DOCMOST_URL})...")
    try:
        docmost_request("POST", "/api/spaces/", {"page": 1, "perPage": 1})
    except requests.exceptions.ConnectionError:
        print(f"ERROR: Cannot connect to Docmost at {DOCMOST_URL}")
        print("Make sure Docmost is running (docker-compose up -d)")
        sys.exit(1)

    print("\nResolving destination path...")
    space_id, parent_page_id = resolve_destination_path(WIKI_DEST_PATH)

    # Number of path segments that make up the source folder itself -- these
    # get dropped from every returned file's path to compute where it lands
    # relative to the destination. Azure DevOps already scoped fetch_wiki_tree()
    # to this folder, so we trust that scoping rather than re-matching strings.
    has_source_folder = bool(WIKI_SOURCE_PATH) and not WIKI_SOURCE_PATH.endswith(".md")
    source_segments = [s for s in WIKI_SOURCE_PATH.strip("/").split("/") if s] if has_source_folder else []
    n_prefix_segments = len(source_segments)

    skip_files = set()
    if has_source_folder:
        source_root = source_segments[-1]  # the actual folder being imported
        source_parent_dir = "/".join(source_segments[:-1])  # sibling dir of the companion .md

        companion_content = None
        companion_path = f"/{source_parent_dir}/{source_root}.md" if source_parent_dir else f"/{source_root}.md"
        try:
            desc_data = azure_request("items", {
                "path": companion_path,
                "includeContent": "true",
                "api-version": "6.0",
            })
        except requests.exceptions.HTTPError:
            encoded_path = companion_path.replace("-", "%2D")
            desc_data = azure_request("items", {
                "path": encoded_path,
                "includeContent": "true",
                "api-version": "6.0",
            })
            companion_content = desc_data.get("content", "")
            if companion_content.strip():
                print(f"  Found companion page: {companion_path}")
                skip_files.add(companion_path)
        except requests.exceptions.RequestException:
            pass

        dest_last_segment = WIKI_DEST_PATH.strip("/").split("/")[-1] if WIKI_DEST_PATH.strip("/") else ""
        dest_already_is_container = (
            dest_last_segment.replace(" ", "-").lower() == source_root.replace(" ", "-").lower()
        )

        if dest_already_is_container:
            # WIKI_DEST_PATH already ends with the source folder's own name
            # (e.g. ".../Vendors/Hardware-Resources" importing "Hardware-Resources"),
            # so the page resolve_destination_path() already found/created IS
            # the container -- don't create another one nested inside it.
            print(f"  Destination already ends with '{source_root}', using it as the container (no extra nesting).")
        else:
            root_children = list_pages(space_id, parent_page_id)
            found = find_page_by_title(root_children, source_root)
            if found:
                if companion_content:
                    print(f"  Container '{source_root}' already exists, keeping its current content (not overwriting).")
                parent_page_id = found["id"]
            else:
                content = companion_content if companion_content else f"# {source_root}\n\n"
                print(f"  Creating container page '{source_root}'...")
                new_page = create_page(space_id, source_root, content, parent_page_id)
                parent_page_id = new_page["id"]

    print("\nChecking for existing pages that would be overwritten...")
    conflicts = find_conflicting_pages(space_id, parent_page_id, files, n_prefix_segments, skip_files)
    if conflicts:
        print("\nERROR: The following page(s) already exist in Docmost. "
              "Nothing was imported, to avoid overwriting existing content:")
        for f in conflicts:
            print(f"  - {f}")
        print("\nRename/move the conflicting page(s) in Docmost (or change WIKI_DEST_PATH) and re-run.")
        sys.exit(1)
    print("  No conflicts found.")

    stats = {"created": 0, "skipped": 0, "errors": 0}
    for wiki_file in files:
        if wiki_file in skip_files:
            print(f"  Skipping (used as container): {wiki_file}")
            continue

        relative_path = strip_source_segments(wiki_file, n_prefix_segments)
        if relative_path is None:
            continue

        try:
            result = import_file(space_id, parent_page_id, wiki_file, relative_path, all_files)
            stats[result] += 1
        except Exception as e:
            print(f"  ERROR on {wiki_file}: {e}")
            stats["errors"] += 1

    print("IMPORT COMPLETE")
    print(f"  Created:  {stats['created']}")
    print(f"  Skipped:  {stats['skipped']}")
    print(f"  Errors:   {stats['errors']}")


if __name__ == "__main__":
    main()