#!/usr/bin/env python3
"""arc2zen — Migrate Arc Browser spaces, tabs, and archived tabs to Zen Browser."""

import argparse, json, os, shutil, sqlite3, struct, sys, time, uuid
from datetime import datetime
from pathlib import Path

try:
    import lz4.block
except ImportError:
    sys.exit("Missing dependency: pip install lz4")

MAGIC = b"mozLz40\0"

def read_lz4(path):
    with open(path, "rb") as f:
        magic = f.read(8)
        size = struct.unpack("<I", f.read(4))[0]
        return magic, json.loads(lz4.block.decompress(f.read(), uncompressed_size=size))

def write_lz4(path, magic, obj):
    data = json.dumps(obj).encode()
    with open(path, "wb") as f:
        f.write(magic)
        f.write(struct.pack("<I", len(data)))
        f.write(lz4.block.compress(data, store_size=False))

def find_arc_dir():
    p = Path.home() / "Library/Application Support/Arc"
    if not p.exists():
        sys.exit("Arc not found. Is Arc installed?")
    return p

def find_zen():
    base = Path.home() / "Library/Application Support/Zen/Profiles"
    if not base.exists():
        sys.exit("Zen profiles not found. Is Zen installed?")
    profiles = [p for p in sorted(base.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)
                if (p / "zen-sessions.jsonlz4").exists()]
    if not profiles:
        sys.exit("No Zen profile found.")
    return profiles[0]

def parse_items(raw):
    items = {}
    for entry in raw:
        if isinstance(entry, dict):
            items[entry["id"]] = entry
    return items

def collect(item_id, items):
    item = items.get(item_id)
    if not item:
        return []
    tabs = []
    if "tab" in item.get("data", {}):
        t = item["data"]["tab"]
        url = t.get("savedURL", "")
        title = item.get("title") or t.get("savedTitle", url)
        if url and not url.startswith("file://"):
            tabs.append({"url": url, "title": title or url})
    for child in item.get("childrenIds", []):
        tabs.extend(collect(child, items))
    return tabs

def hex_color(r, g, b):
    return "#{:02X}{:02X}{:02X}".format(
        int(max(0, min(1, r)) * 255),
        int(max(0, min(1, g)) * 255),
        int(max(0, min(1, b)) * 255),
    )

_n = 0
def make_tab(url, title, now, workspace=None, pinned=False, essential=False):
    global _n
    _n += 1
    t = {
        "entries": [{"url": url, "title": title, "cacheKey": 0, "ID": _n,
                     "docshellUUID": "{" + str(uuid.uuid4()) + "}",
                     "originalURI": url, "resultPrincipalURI": None,
                     "hasUserInteraction": False,
                     "triggeringPrincipal_base64": "{\"3\":{}}",
                     "docIdentifier": _n}],
        "lastAccessed": now, "hidden": False,
        "zenSyncId": f"{now}-{_n}", "zenEssential": essential,
        "pinned": pinned, "zenDefaultUserContextId": None,
        "zenPinnedIcon": None, "zenIsEmpty": False, "zenHasStaticIcon": False,
        "zenGlanceId": None, "zenIsGlance": False, "zenLiveFolderItemId": None,
        "searchMode": None, "userContextId": 0, "attributes": {},
        "index": 1, "requestedIndex": 0, "image": None,
    }
    if workspace:
        t["zenWorkspace"] = workspace
    return t

# ── Spaces + tabs migration ────────────────────────────────────────────────────

def migrate_spaces(arc_dir, zen_path, dry_run, overwrite, verbose):
    sidebar_path = arc_dir / "StorableSidebar.json"
    if not sidebar_path.exists():
        print("StorableSidebar.json not found — skipping spaces migration.")
        return

    with open(sidebar_path) as f:
        arc = json.load(f)

    sidebar = arc["sidebar"]["containers"][1]
    items = parse_items(sidebar["items"])
    arc_spaces = [e for e in sidebar["spaces"] if isinstance(e, dict)]

    global_parent = next(
        (iid for iid, item in items.items()
         if item.get("parentID") is None
         and "itemContainer" in item.get("data", {})
         and item.get("childrenIds")),
        None
    )

    zs_path = zen_path / "zen-sessions.jsonlz4"
    ss_path = zen_path / "sessionstore.jsonlz4"
    zs_magic, zs = read_lz4(zs_path)

    existing = {s["name"] for s in zs.get("spaces", [])}
    space_map = {s["name"]: s["uuid"] for s in zs.get("spaces", [])}
    new_spaces = list(zs.get("spaces", []))
    added = []

    for s in arc_spaces:
        name = s.get("title", "Imported")
        if not overwrite and name in existing:
            print(f"  skip  {name} (already exists, use --overwrite to re-import)")
            continue
        sid = "{" + str(uuid.uuid4()) + "}"
        space_map[name] = sid
        info = s.get("customInfo", {})
        emoji = info.get("iconType", {}).get("emoji_v2", "")
        mid = info.get("windowTheme", {}).get("primaryColorPalette", {}).get("midTone", {})
        r, g, b = mid.get("red", 0.5), mid.get("green", 0.5), mid.get("blue", 0.5)
        c1, c2 = hex_color(r, g, b), hex_color(r * 0.7, g * 0.7, b * 0.7)
        obj = {"uuid": sid, "name": name,
               "theme": {"type": "gradient", "gradientColors": [c1, c2], "opacity": 0.5, "texture": 0},
               "containerTabId": 0, "hasCollapsedPinnedTabs": False}
        if emoji:
            obj["icon"] = emoji
        new_spaces.append(obj)
        added.append(name)

    now = int(time.time() * 1000)
    tabs = []
    global_tabs = collect(global_parent, items) if global_parent else []
    for t in global_tabs:
        tabs.append(make_tab(t["url"], t["title"], now, essential=True, pinned=True))
    print(f"  Essential tabs (all spaces): {len(global_tabs)}")

    for s in arc_spaces:
        name = s.get("title", "")
        sid = space_map.get(name)
        if not sid:
            continue
        cids = s.get("containerIDs", [])
        containers = {}
        for i, cid in enumerate(cids):
            if isinstance(cid, str) and len(cid) > 20:
                label = cids[i-1] if i > 0 and len(cids[i-1]) < 20 else "unpinned"
                containers[label] = cid
        pinned = collect(containers.get("pinned", ""), items)
        unpinned = collect(containers.get("unpinned", ""), items)
        emoji = s.get("customInfo", {}).get("iconType", {}).get("emoji_v2", "")
        icon = f"{emoji} " if emoji else ""
        print(f"  {icon}{name}: {len(pinned)} pinned  {len(unpinned)} unpinned")
        if verbose:
            for t in pinned:
                print(f"    📌 {t['title'][:70]}")
            for t in unpinned:
                print(f"       {t['title'][:70]}")
        for t in pinned:
            tabs.append(make_tab(t["url"], t["title"], now, workspace=sid, pinned=True))
        for t in unpinned:
            tabs.append(make_tab(t["url"], t["title"], now, workspace=sid))

    print(f"  {len(new_spaces)} spaces  {len(tabs)} tabs")

    if dry_run:
        return

    zs["spaces"] = new_spaces
    zs["tabs"] = tabs
    zs["lastCollected"] = now
    write_lz4(zs_path, zs_magic, zs)

    if ss_path.exists():
        ss_magic, ss = read_lz4(ss_path)
    else:
        ss_magic = MAGIC
        ss = {"version": ["sessionrestore", 1], "selectedWindow": 1,
              "_closedWindows": [], "session": {"lastUpdate": now, "startTime": now, "recentCrashes": 0},
              "global": {}, "windows": [{}]}

    if not ss.get("windows"):
        ss["windows"] = [{}]
    w = ss["windows"][0]
    w.update({"tabs": tabs, "spaces": new_spaces, "selected": 1,
              "activeZenSpace": space_map.get(added[0]) if added else w.get("activeZenSpace"),
              "_closedTabs": w.get("_closedTabs", []), "groups": w.get("groups", []),
              "closedGroups": w.get("closedGroups", []), "splitViewData": w.get("splitViewData", {}),
              "folders": w.get("folders", [])})
    write_lz4(ss_path, ss_magic, ss)

    rec_path = zen_path / "sessionstore-backups/recovery.jsonlz4"
    if rec_path.exists():
        rec_magic, rec = read_lz4(rec_path)
        if rec.get("windows"):
            rec["windows"][0].update({"tabs": tabs, "spaces": new_spaces, "selected": 1})
        write_lz4(rec_path, rec_magic, rec)

# ── Archived tabs → Zen bookmarks ─────────────────────────────────────────────

def migrate_archive(arc_dir, zen_path, dry_run, verbose):
    archive_path = arc_dir / "StorableArchiveItems.json"
    if not archive_path.exists():
        print("  StorableArchiveItems.json not found — skipping archived tabs.")
        return

    with open(archive_path) as f:
        d = json.load(f)

    items_raw = d.get("items", [])
    archived = []
    for entry in items_raw:
        if isinstance(entry, dict):
            si = entry.get("sidebarItem", {})
            tab = si.get("data", {}).get("tab", {})
            url = tab.get("savedURL", "")
            title = tab.get("savedTitle", "") or url
            if url and url.startswith("http"):
                archived.append({"url": url, "title": title})

    # Deduplicate by URL
    seen = set()
    unique = []
    for t in archived:
        if t["url"] not in seen:
            seen.add(t["url"])
            unique.append(t)

    print(f"  Archived tabs: {len(unique)} unique URLs")
    if verbose:
        for t in unique[:10]:
            print(f"    {t['title'][:70]}")
        if len(unique) > 10:
            print(f"    ... and {len(unique) - 10} more")

    if dry_run:
        return

    db_path = zen_path / "places.sqlite"
    if not db_path.exists():
        print("  places.sqlite not found — skipping bookmarks.")
        return

    con = sqlite3.connect(db_path)
    cur = con.cursor()

    # Find or create "Arc Archive" bookmark folder under Bookmarks Menu
    cur.execute("SELECT id FROM moz_bookmarks WHERE title='Bookmarks Menu' AND type=2")
    row = cur.fetchone()
    menu_id = row[0] if row else 2  # fallback to id=2

    cur.execute("SELECT id FROM moz_bookmarks WHERE title='Arc Archive' AND type=2 AND parent=?", (menu_id,))
    folder = cur.fetchone()
    if folder:
        folder_id = folder[0]
        # Clear existing entries
        cur.execute("DELETE FROM moz_bookmarks WHERE parent=?", (folder_id,))
    else:
        now_micro = int(time.time() * 1_000_000)
        cur.execute(
            "INSERT INTO moz_bookmarks (type, parent, position, title, dateAdded, lastModified, guid) VALUES (2,?,0,'Arc Archive',?,?,?)",
            (menu_id, now_micro, now_micro, str(uuid.uuid4())[:12])
        )
        folder_id = cur.lastrowid

    now_micro = int(time.time() * 1_000_000)
    inserted = 0
    for i, t in enumerate(unique):
        try:
            cur.execute("SELECT id FROM moz_places WHERE url=?", (t["url"],))
            place = cur.fetchone()
            if place:
                place_id = place[0]
            else:
                cur.execute(
                    "INSERT INTO moz_places (url, title, rev_host, visit_count, hidden, typed, guid) VALUES (?,?,?,0,0,0,?)",
                    (t["url"], t["title"], t["url"][::-1].split("/")[0] + ".", str(uuid.uuid4())[:12])
                )
                place_id = cur.lastrowid
            cur.execute(
                "INSERT INTO moz_bookmarks (type, fk, parent, position, title, dateAdded, lastModified, guid) VALUES (1,?,?,?,?,?,?,?)",
                (place_id, folder_id, i, t["title"][:255], now_micro, now_micro, str(uuid.uuid4())[:12])
            )
            inserted += 1
        except Exception:
            continue

    con.commit()
    con.close()
    print(f"  Saved {inserted} archived tabs → Bookmarks / Arc Archive")

# ── Main ───────────────────────────────────────────────────────────────────────

def run(arc_dir, zen_path, dry_run, overwrite, verbose, skip_spaces, skip_archive):
    print(f"Arc  {arc_dir}")
    print(f"Zen  {zen_path}\n")

    if not dry_run:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        bdir = zen_path / f"arc2zen-backup-{ts}"
        bdir.mkdir()
        for fname in ["zen-sessions.jsonlz4", "sessionstore.jsonlz4", "places.sqlite"]:
            src = zen_path / fname
            if src.exists():
                shutil.copy2(src, bdir)
        print(f"Backup → {bdir.name}/\n")

    if not skip_spaces:
        print("── Spaces & tabs ─────────────────────────────────────")
        migrate_spaces(arc_dir, zen_path, dry_run, overwrite, verbose)
        print()

    if not skip_archive:
        print("── Archived tabs → bookmarks ─────────────────────────")
        migrate_archive(arc_dir, zen_path, dry_run, verbose)
        print()

    if dry_run:
        print("[dry-run] Nothing written.")
    else:
        print("Done. Open Zen.")

def main():
    p = argparse.ArgumentParser(description="Migrate Arc Browser spaces, tabs, and archive to Zen Browser.")
    p.add_argument("--arc", help="Path to Arc's application support directory")
    p.add_argument("--zen", help="Path to Zen profile directory")
    p.add_argument("--dry-run", action="store_true", help="Preview without writing")
    p.add_argument("--overwrite", action="store_true", help="Re-import spaces that already exist")
    p.add_argument("--verbose", "-v", action="store_true", help="List every item")
    p.add_argument("--no-spaces", action="store_true", help="Skip spaces/tabs migration")
    p.add_argument("--no-archive", action="store_true", help="Skip archived tabs migration")
    args = p.parse_args()

    arc = Path(args.arc) if args.arc else find_arc_dir()
    zen = Path(args.zen) if args.zen else find_zen()
    run(arc, zen, args.dry_run, args.overwrite, args.verbose, args.no_spaces, args.no_archive)

if __name__ == "__main__":
    main()
