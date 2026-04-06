# arc2zen

Migrate Arc Browser to Zen Browser — spaces, tabs, and archived tabs.

## What gets migrated

| Arc | Zen |
|-----|-----|
| Spaces | Workspaces with matching icon + gradient color |
| Space emoji icon | Workspace icon |
| Pinned tabs per space | Pinned tabs per workspace |
| Unpinned tabs per space | Regular tabs per workspace |
| Global pinned tabs (across all spaces) | Essential tabs (across all workspaces) |
| Archived tabs (2000+) | Bookmarks › Arc Archive folder |

## Requirements

- macOS (paths are auto-detected)
- Python 3.10+
- **Zen must be fully closed (`Cmd+Q`) before running**

## Install

```bash
pip install lz4
```

## Usage

```bash
python3 arc2zen.py
```

Arc and Zen are auto-detected on macOS. A timestamped backup of your Zen session is saved before anything is written.

## Options

```
--dry-run        Preview without writing anything
--overwrite      Re-import spaces that already exist in Zen
--no-spaces      Skip spaces and tabs, only migrate archive
--no-archive     Skip archived tabs, only migrate spaces and tabs
--verbose, -v    List every tab being migrated
--arc PATH       Path to Arc's application support directory
--zen PATH       Path to your Zen profile directory
```

## Examples

```bash
# Preview everything
python3 arc2zen.py --dry-run

# Migrate spaces only (skip the 2000+ archived tabs)
python3 arc2zen.py --no-archive

# Migrate archive only
python3 arc2zen.py --no-spaces

# Force re-import spaces that already exist
python3 arc2zen.py --overwrite
```

## What doesn't migrate

- Tab browsing history
- Boosts (custom CSS/JS per site)
- Notes and Easel content
- Local file tabs (`file://`)
