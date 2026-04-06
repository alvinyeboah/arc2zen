#!/usr/bin/env python3
"""
arc2zen — Migrate Arc Browser spaces and tabs to Zen Browser.
"""

import argparse
import glob
import json
import os
import shutil
import struct
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

try:
    import lz4.block
except ImportError:
    sys.exit("Missing dependency: pip install lz4")


# ── mozlz4 helpers ────────────────────────────────────────────────────────────

MOZLZ4_MAGIC = b"mozLz40\0"

def read_mozlz4(path: Path) -> tuple[bytes, dict]:
    with open(path, "rb") as f:
        magic = f.read(8)
        if magic != MOZLZ4_MAGIC:
            raise ValueError(f"{path.name} is not a valid mozlz4 file")
        size = struct.unpack("<I", f.read(4))[0]
        return magic, json.loads(lz4.block.decompress(f.read(), uncompressed_size=size).decode("utf-8"))

def write_mozlz4(path: Path, magic: bytes, obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8")
    with open(path, "wb") as f:
        f.write(magic)
        f.write(struct.pack("<I", len(data)))
        f.write(lz4.block.compress(data, store_size=False))


# ── Path detection ────────────────────────────────────────────────────────────

def find_arc_sidebar() -> Path:
    p = Path.home() / "Library/Application Support/Arc/StorableSidebar.json"
    if not p.exists():
        raise FileNotFoundError(f"Arc sidebar not found at {p}\nIs Arc installed?")
    return p

def find_zen_profile() -> Path:
    base = Path.home() / "Library/Application Support/Zen/Profiles"
    if not base.exists():
        raise FileNotFoundError(f"Zen profiles not found at {base}\nIs Zen installed?")
    profiles = sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    profiles = [p for p in profiles if (p / "zen-sessions.jsonlz4").exists()]
    if not profiles:
        raise FileNotFoundError("No Zen profile with zen-sessions.jsonlz4 found")
    return profiles[0]


# ── Arc parsing ───────────────────────────────────────────────────────────────

def parse_arc(sidebar_path: Path) -> tuple[list, list, dict]:
    """Returns (spaces, global_pinned_tabs, items_map)."""
    with open(sidebar_path) as f:
        data = json.load(f)

    sidebar = data["sidebar"]["containers"][1]

    # Parse items (alternating: uuid-string, object, ...)
    items: dict[str, dict] = {}
    for entry in sidebar["items"]:
        if isinstance(entry, dict):
            items[entry["id"]] = entry

    # Parse spaces
    spaces = []
    for entry in sidebar["spaces"]:
        if isinstance(entry, dict):
            spaces.append(entry)

    # Find global pinned container: item with no parent and type itemContainer
    # holding tabs like Claude, ChatGPT etc.
    global_parent = _find_global_pinned_parent(items)

    return spaces, global_parent, items


def _find_global_pinned_parent(items: dict) -> str | None:
    """Find the container that holds cross-space pinned tabs (no parentID)."""
    for iid, item in items.items():
        if item.get("parentID") is None and "itemContainer" in item.get("data", {}):
            children = item.get("childrenIds", [])
            if children:
                return iid
    return None


def collect_tabs(item_id: str, items: dict) -> list[dict]:
    item = items.get(item_id)
    if not item:
        return []
    tabs = []
    data = item.get("data", {})
    if "tab" in data:
        t = data["tab"]
        url = t.get("savedURL", "")
        title = item.get("title") or t.get("savedTitle", url)
        if url:
            tabs.append({"title": title or url, "url": url})
    for child_id in item.get("childrenIds", []):
        tabs.extend(collect_tabs(child_id, items))
    return tabs


def srgb_to_hex(r: float, g: float, b: float) -> str:
    r, g, b = max(0.0, min(1.0, r)), max(0.0, min(1.0, g)), max(0.0, min(1.0, b))
    return "#{:02X}{:02X}{:02X}".format(int(r * 255), int(g * 255), int(b * 255))


def arc_space_meta(space: dict) -> tuple[str, list[str]]:
    """Return (emoji, [color1, color2]) for a space."""
    info = space.get("customInfo", {})
    emoji = info.get("iconType", {}).get("emoji_v2", "")
    palette = info.get("windowTheme", {}).get("primaryColorPalette", {})
    mid = palette.get("midTone", {})
    r, g, b = mid.get("red", 0.5), mid.get("green", 0.5), mid.get("blue", 0.5)
    color = srgb_to_hex(r, g, b)
    dark = srgb_to_hex(r * 0.7, g * 0.7, b * 0.7)
    return emoji, [color, dark]


# ── Tab construction ──────────────────────────────────────────────────────────

_counter = 0

def _next_id() -> int:
    global _counter
    _counter += 1
    return _counter

def make_zen_tab(
    url: str,
    title: str,
    now_ms: int,
    workspace_uuid: str | None = None,
    pinned: bool = False,
    essential: bool = False,
) -> dict:
    n = _next_id()
    t = {
        "entries": [{
            "url": url,
            "title": title,
            "cacheKey": 0,
            "ID": n,
            "docshellUUID": "{" + str(uuid.uuid4()) + "}",
            "originalURI": url,
            "resultPrincipalURI": None,
            "hasUserInteraction": False,
            "triggeringPrincipal_base64": "{\"3\":{}}",
            "docIdentifier": n,
        }],
        "lastAccessed": now_ms,
        "hidden": False,
        "zenSyncId": f"{now_ms}-{n}",
        "zenEssential": essential,
        "pinned": pinned,
        "zenDefaultUserContextId": None,
        "zenPinnedIcon": None,
        "zenIsEmpty": False,
        "zenHasStaticIcon": False,
        "zenGlanceId": None,
        "zenIsGlance": False,
        "zenLiveFolderItemId": None,
        "searchMode": None,
        "userContextId": 0,
        "attributes": {},
        "index": 1,
        "requestedIndex": 0,
        "image": None,
    }
    if workspace_uuid:
        t["zenWorkspace"] = workspace_uuid
    return t


# ── Migration ─────────────────────────────────────────────────────────────────

def migrate(
    arc_path: Path,
    zen_profile: Path,
    dry_run: bool = False,
    skip_existing: bool = True,
    verbose: bool = False,
) -> None:
    print(f"Arc  → {arc_path}")
    print(f"Zen  → {zen_profile}")
    print()

    arc_spaces, global_parent, items = parse_arc(arc_path)

    zs_path = zen_profile / "zen-sessions.jsonlz4"
    ss_path = zen_profile / "sessionstore.jsonlz4"
    recovery_path = zen_profile / "sessionstore-backups/recovery.jsonlz4"

    zs_magic, zs = read_mozlz4(zs_path)
    ss_magic, ss = read_mozlz4(ss_path)

    existing_names = {s["name"] for s in zs.get("spaces", [])}

    if dry_run:
        print("[dry-run] No files will be written.\n")

    # ── Backup ────────────────────────────────────────────────────────────────
    if not dry_run:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = zen_profile / f"arc2zen-backup-{ts}"
        backup_dir.mkdir()
        shutil.copy2(zs_path, backup_dir / "zen-sessions.jsonlz4")
        shutil.copy2(ss_path, backup_dir / "sessionstore.jsonlz4")
        print(f"Backed up existing session → {backup_dir.name}/\n")

    # ── Build spaces ──────────────────────────────────────────────────────────
    new_spaces = list(zs.get("spaces", []))
    space_uuid_map: dict[str, str] = {s["name"]: s["uuid"] for s in new_spaces}

    spaces_added = []
    spaces_skipped = []

    for arc_space in arc_spaces:
        name = arc_space.get("title", "Imported")
        if skip_existing and name in existing_names:
            spaces_skipped.append(name)
            continue

        space_uuid = "{" + str(uuid.uuid4()) + "}"
        space_uuid_map[name] = space_uuid
        emoji, colors = arc_space_meta(arc_space)

        space_obj: dict = {
            "uuid": space_uuid,
            "name": name,
            "theme": {
                "type": "gradient",
                "gradientColors": colors,
                "opacity": 0.5,
                "texture": 0,
            },
            "containerTabId": 0,
            "hasCollapsedPinnedTabs": False,
        }
        if emoji:
            space_obj["icon"] = emoji

        new_spaces.append(space_obj)
        spaces_added.append(name)

    # ── Build tabs ────────────────────────────────────────────────────────────
    now_ms = int(time.time() * 1000)
    new_tabs: list[dict] = []

    # Global essential tabs (shown across all spaces)
    global_tabs = collect_tabs(global_parent, items) if global_parent else []
    for tab in global_tabs:
        new_tabs.append(make_zen_tab(tab["url"], tab["title"], now_ms, essential=True, pinned=True))

    # Per-space tabs
    tab_summary: dict[str, tuple[int, int]] = {}
    for arc_space in arc_spaces:
        name = arc_space.get("title", "")
        space_uuid = space_uuid_map.get(name)
        if not space_uuid:
            continue

        cids = arc_space.get("containerIDs", [])
        containers: dict[str, str] = {}
        for i, cid in enumerate(cids):
            if isinstance(cid, str) and len(cid) > 20:
                label = cids[i - 1] if i > 0 and isinstance(cids[i - 1], str) and len(cids[i - 1]) < 20 else "unpinned"
                containers[label] = cid

        pinned_tabs = collect_tabs(containers.get("pinned", ""), items)
        unpinned_tabs = collect_tabs(containers.get("unpinned", ""), items)
        tab_summary[name] = (len(pinned_tabs), len(unpinned_tabs))

        for tab in pinned_tabs:
            new_tabs.append(make_zen_tab(tab["url"], tab["title"], now_ms, workspace_uuid=space_uuid, pinned=True))
        for tab in unpinned_tabs:
            new_tabs.append(make_zen_tab(tab["url"], tab["title"], now_ms, workspace_uuid=space_uuid, pinned=False))

    # ── Print summary ─────────────────────────────────────────────────────────
    if spaces_skipped:
        print(f"Skipped (already exist): {', '.join(spaces_skipped)}")

    print(f"Essential tabs (all spaces): {len(global_tabs)}")
    if verbose:
        for t in global_tabs:
            print(f"  ⭐ {t['title']}")
    print()

    for name in spaces_added:
        pinned, unpinned = tab_summary.get(name, (0, 0))
        emoji, _ = arc_space_meta(next(s for s in arc_spaces if s.get("title") == name))
        icon = f"{emoji} " if emoji else ""
        print(f"  {icon}{name}: {pinned} pinned, {unpinned} unpinned")
        if verbose:
            space_uuid = space_uuid_map[name]
            for tab in new_tabs:
                if tab.get("zenWorkspace") == space_uuid:
                    pin = "📌" if tab["pinned"] else "  "
                    print(f"    {pin} {tab['entries'][0]['title'][:60]}")
    print()

    # ── Write ─────────────────────────────────────────────────────────────────
    if dry_run:
        print(f"[dry-run] Would write {len(new_spaces)} spaces, {len(new_tabs)} tabs.")
        return

    # Remove old tabs for spaces we're re-importing
    old_uuids = {space_uuid_map[n] for n in spaces_added if n in space_uuid_map}
    existing_tabs = [t for t in zs.get("tabs", []) if t.get("zenWorkspace") not in old_uuids]
    # Also strip old essential tabs if we have new ones
    if global_tabs:
        existing_tabs = [t for t in existing_tabs if not t.get("zenEssential")]
    all_tabs = existing_tabs + new_tabs

    # zen-sessions.jsonlz4
    zs["spaces"] = new_spaces
    zs["tabs"] = all_tabs
    zs["lastCollected"] = now_ms
    write_mozlz4(zs_path, zs_magic, zs)

    # sessionstore.jsonlz4
    w = ss["windows"][0]
    w["spaces"] = new_spaces
    w["tabs"] = all_tabs
    w["selected"] = 1
    w["activeZenSpace"] = space_uuid_map.get(spaces_added[0]) if spaces_added else w.get("activeZenSpace")
    write_mozlz4(ss_path, ss_magic, ss)

    # recovery
    if recovery_path.exists():
        rec_magic, rec = read_mozlz4(recovery_path)
        if rec.get("windows"):
            rec["windows"][0]["spaces"] = new_spaces
            rec["windows"][0]["tabs"] = all_tabs
            rec["windows"][0]["selected"] = 1
        write_mozlz4(recovery_path, rec_magic, rec)

    print(f"Written {len(new_spaces)} spaces and {len(all_tabs)} tabs.")
    print("Open Zen to see your spaces.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="arc2zen",
        description="Migrate Arc Browser spaces and tabs to Zen Browser.",
    )
    parser.add_argument("--arc", metavar="PATH", help="Path to Arc's StorableSidebar.json (auto-detected on macOS)")
    parser.add_argument("--zen", metavar="PATH", help="Path to Zen profile directory (auto-detected on macOS)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing anything")
    parser.add_argument("--overwrite", action="store_true", help="Re-import spaces that already exist in Zen")
    parser.add_argument("--verbose", "-v", action="store_true", help="List every tab being migrated")
    args = parser.parse_args()

    try:
        arc_path = Path(args.arc) if args.arc else find_arc_sidebar()
        zen_profile = Path(args.zen) if args.zen else find_zen_profile()
    except FileNotFoundError as e:
        sys.exit(f"Error: {e}")

    try:
        migrate(
            arc_path=arc_path,
            zen_profile=zen_profile,
            dry_run=args.dry_run,
            skip_existing=not args.overwrite,
            verbose=args.verbose,
        )
    except Exception as e:
        sys.exit(f"Migration failed: {e}")


if __name__ == "__main__":
    main()
