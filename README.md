# arc2zen

Migrate Arc Browser spaces and tabs to Zen Browser.

Transfers all spaces with their icons and theme colors, pinned tabs per space, unpinned tabs, and cross-space essential tabs (the ones pinned across all spaces in Arc).

## Requirements

- macOS (Arc and Zen paths are auto-detected)
- Python 3.10+
- Zen Browser must be fully closed before running

## Install

```bash
pip install lz4
```

Or with a virtualenv:

```bash
python3 -m venv venv
source venv/bin/activate
pip install lz4
```

## Usage

Close Zen completely (`Cmd+Q`), then run:

```bash
python3 arc2zen.py
```

That's it. Arc and Zen are auto-detected. A backup of your existing Zen session is saved to your Zen profile directory before anything is written.

### Options

```
--dry-run        Preview what will be migrated without writing anything
--overwrite      Re-import spaces that already exist in Zen
--verbose, -v    List every tab as it's migrated
--arc PATH       Path to Arc's StorableSidebar.json (if not auto-detected)
--zen PATH       Path to your Zen profile directory (if not auto-detected)
```

### Examples

```bash
# Preview first
python3 arc2zen.py --dry-run

# Full migration with tab list
python3 arc2zen.py --verbose

# Re-run and overwrite previously imported spaces
python3 arc2zen.py --overwrite
```

## What gets migrated

| Arc | Zen |
|-----|-----|
| Spaces | Workspaces |
| Space emoji icon | Workspace icon |
| Space theme color | Workspace gradient |
| Pinned tabs per space | Pinned tabs per workspace |
| Unpinned tabs per space | Regular tabs per workspace |
| Global pinned tabs (across all spaces) | Essential tabs (across all workspaces) |

## What doesn't migrate

- Tab history (only the current URL per tab)
- Archived tabs
- Boosts
- Notes
- Easel

## Tested on

- Arc 1.x (macOS)
- Zen 1.x (macOS)
