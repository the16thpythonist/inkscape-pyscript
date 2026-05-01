# Changelog

All notable changes to this fork are documented in this file. Format roughly follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **Parametric Inspector** floating Gtk window (`Extensions → Python Scripting → Parametric Inspector`). Two-tab UI with a 3-column attribute table and an embedded GtkSource Python editor.
- **`param:` XML namespace** (`http://fdmtech.com/inkscape/param`). `param:foo="EXPR"` attributes are evaluated against the user's script after each Run and the result is written into the matching non-namespaced `foo` attribute. The expression is preserved for re-runs.
- `eval_params()` pass in `PYScript.execute()` — runs after the user script finishes; both the headless `Run and Update` entry and the inspector use it.
- Live canvas selection sync — the inspector polls Inkscape every 300 ms via `inkscape --active-window --actions=select-list` and updates the attribute table when the selection changes.
- Per-row state tinting — yellow while editing, green after a successful Run, red on error.
- Autocomplete providers in the Script editor: script identifiers, SVG `#ids`, and a color picker triggered on `#`-prefixed hex literals.
- Terminal panel at the bottom of the inspector with Clear button. Captures `print()` output and full tracebacks per Run.
- `Ctrl+Enter` keyboard shortcut for Run inside the inspector window.
- `pyscript_inspector.{py,inx}` — extension entry point that patches `xmlns:param` onto the SVG root, then spawns the inspector as a *detached* process so Inkscape's UI stays interactive.
- `pyscript/INSTALL_NOTES.txt` — install steps and patch history for future maintainers.

### Changed
- `pyscript/svg.py`: `collections.Mapping` → `collections.abc.Mapping`. The original moved/aliased class was removed in Python 3.10; without this, `PathObject.style({...})` raises `AttributeError` on modern Inkscape builds.

### Removed
- `pyscript_ide.{py,inx}` — the modal Code Editor is replaced by the Script tab inside the Parametric Inspector. The headless `pyscript_run` entry is preserved for keyboard-shortcut re-evaluation of `param:*`.

## Upstream baseline

The first commit on this fork (`b58f9cc Minor API adjustemts`) matches upstream `master` at [mnesarco/inkscape-pyscript](https://gitlab.com/mnesarco/inkscape-pyscript). All entries above are net-new on top.
