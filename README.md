# inkscape-pyscript

Python scripting and OnShape-style **parametric variables** for [Inkscape](https://inkscape.org).

Define variables in a Python script, reference them from `param:*` attributes on any SVG element, and a single floating window keeps the document in sync as you edit. Fork of [mnesarco/inkscape-pyscript](https://gitlab.com/mnesarco/inkscape-pyscript) on top of which a parametric inspector UI was added.

## What it does

- A floating **Parametric Inspector** window (Extensions → Python Scripting → Parametric Inspector) with two tabs:
  - **Inspector** — a 3-column table showing every attribute and CSS property of the currently selected element. The third column accepts a Python expression that gets evaluated against your script and pushed back to the canvas on Run.
  - **Script** — a [GtkSource](https://wiki.gnome.org/Projects/GtkSourceView) Python editor with autocomplete for script identifiers, SVG `#ids`, and a color picker.
- A custom `param:` XML namespace (`http://fdmtech.com/inkscape/param`) lets you store the *expression* alongside the evaluated value, so re-runs and round-trips through Inkscape preserve parametric intent.
- A headless **Run and Update** menu entry that re-evaluates `param:*` expressions without opening the inspector — handy for keyboard-shortcut workflows once your variables are set.

```xml
<circle id="ball"
        cx="50" cy="50"
        param:r="WIDTH / 4"
        r="40" />
```

```python
WIDTH = 160
```

After Run, `r="40"` becomes `r="40.0"` (the evaluated value), and `param:r="WIDTH / 4"` is preserved so the next Run picks up new `WIDTH` values.

## Install

Requires Inkscape 1.0+, Python 3.5+ (3.10+ tested), Gtk 3.x, GtkSource 3.0.

Copy the repository contents into your Inkscape extensions directory:

```bash
cp -r pyscript pyscript_inspector.py pyscript_inspector.inx \
      pyscript_run.py pyscript_run.inx \
      ~/.config/inkscape/extensions/
```

Restart Inkscape. The extension appears under **Extensions → Python Scripting**.

To bind the inspector to a keyboard shortcut, add to `~/.config/inkscape/keys/default.xml`:

```xml
<bind gaction="app.com.fdmtech.inkscape.pyscript.inspector"
      keys="&lt;Primary&gt;&lt;Shift&gt;i" />
```

See [`pyscript/INSTALL_NOTES.txt`](pyscript/INSTALL_NOTES.txt) for full install notes and patch history.

## Usage

1. Open an SVG in Inkscape.
2. Run **Extensions → Python Scripting → Parametric Inspector** (or `Ctrl+Shift+I` if you bound it).
3. In the **Script** tab, define your variables:

   ```python
   WIDTH = 200
   MARGIN = 10
   ACCENT = "#cc0000"
   ```

4. Select an element on the canvas. The **Inspector** tab populates with its attributes.
5. Click any cell in the third column and type a Python expression — the row turns yellow.
6. Press **Run** (or `Ctrl+Enter`). Rows turn green on success, red on error. The terminal panel at the bottom captures `print()` output and tracebacks.

The inspector polls Inkscape every 300 ms, so picking a different element on the canvas updates the table live.

### Sample documents

See `pyscript_samples/` for working examples (`basic1.svg`, `basic2-rotation.svg`, `py-car-y2.svg`).

## How it talks to Inkscape

The inspector runs as a *detached* process so Inkscape's UI never freezes. All round-tripping happens via:

```
inkscape --active-window --actions=...
```

with `select-list`, `select-by-id`, `object-set-attribute`, `object-set-property`, `export-do`, etc. The launcher patches `xmlns:param` onto the SVG root so the namespace round-trips through Inkscape cleanly.

## Limitations

- `object-set-attribute` uses comma as the name/value separator. Expression results that contain commas are skipped and reported in the status bar — use `translate(50 50)` (space) instead of `translate(50, 50)`.
- The inspector talks to *one* Inkscape window via `--active-window`. Multiple Inkscape windows aren't disambiguated.
- CSS-vs-XML attribute classification is a fixed allowlist (`fill`, `stroke`, `stroke-width`, `opacity`, etc.) — exotic CSS properties may need to be added to `CSS_PROPERTIES` in `pyscript/inspector.py`.

## Credits

Fork of [mnesarco/inkscape-pyscript](https://gitlab.com/mnesarco/inkscape-pyscript) — the original `pyscript_run` evaluator, sample documents, and Gtk plumbing are by Frank Martinez. The Parametric Inspector window, `param:*` namespace evaluator, and detached-process architecture are added on top.
