# -*- coding: utf-8 -*-
"""
inspector.py
Floating Parametric Inspector window for inkscape-pyscript.

Architecture:
- Launched as a detached process by pyscript_inspector.py.
- Polls the running Inkscape via `inkscape --active-window --actions=select-list`
  to track which element the user has selected on the canvas.
- Pushes parameter changes back to the running Inkscape via
  `inkscape --active-window --actions="select-by-id:ID;object-set-attribute:NAME,VALUE"`.
- Holds an in-memory lxml model of the SVG (loaded once at startup from the
  path passed in argv[1]). The model is the source of truth for `param:*`
  attribute *expressions*; evaluated values are pushed live to the canvas.
"""

import os
import re
import sys
import threading
import subprocess
import time
import ast
import json
from io import BytesIO
from lxml import etree

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GtkSource', '3.0')
from gi.repository import Gtk, GLib, Gdk, GdkPixbuf, GObject, Pango, GtkSource

PARAM_NS = 'http://fdmtech.com/inkscape/param'
PARAM_PREFIX = '{%s}' % PARAM_NS
SVG_NS = 'http://www.w3.org/2000/svg'

NSMAP_SEARCH = {'svg': SVG_NS, 'param': PARAM_NS}

POLL_INTERVAL_MS = 300
# Periodically re-fetch the document state from Inkscape regardless of
# whether the canvas selection changed — picks up edits like Fill/Stroke
# changes the user makes on an already-selected element. Skipped while
# the user is focused in an expression entry to avoid disrupting typing.
PERIODIC_REFRESH_MS = 1500

COMMON_ATTRS = [
    'cx', 'cy', 'r', 'rx', 'ry',
    'x', 'y', 'width', 'height',
    'x1', 'y1', 'x2', 'y2',
    'd', 'points', 'transform',
    'fill', 'stroke', 'stroke-width', 'opacity',
    'font-size',
]

# CSS properties that Inkscape stores inside style="..." instead of as
# top-level XML attributes. We expand the style attribute into virtual
# rows for these and push them via the `object-set-property` action,
# which Inkscape interprets as "set this CSS property in style".
CSS_PROPERTIES = {
    'fill', 'fill-opacity', 'fill-rule',
    'stroke', 'stroke-width', 'stroke-opacity',
    'stroke-dasharray', 'stroke-dashoffset',
    'stroke-linecap', 'stroke-linejoin', 'stroke-miterlimit',
    'opacity', 'visibility', 'display',
    'font-family', 'font-size', 'font-weight', 'font-style',
    'text-anchor', 'text-decoration',
    'paint-order', 'mix-blend-mode',
    'color', 'color-interpolation',
}


class _ScriptInk:
    """Minimal pyscript-compatible `ink` shim that gets injected into the
    Script tab's namespace. Mirrors the methods most pyscript samples use
    (`select_first`, `select`, `xpath`) so script code that worked under
    headless `pyscript_run` keeps working in the inspector."""

    _SELECTOR_RE = re.compile(
        r'#(?P<ident>[a-zA-Z0-9._\-:]+)|(?P<tag>(\w+:)*\w+)')

    def __init__(self, document):
        self.document = document  # lxml ElementTree

    def select(self, selector):
        nodes = []
        for m in self._SELECTOR_RE.finditer(selector):
            ident = m.group('ident')
            if ident:
                node = self.document.find('.//*[@id="%s"]' % ident)
                if node is not None:
                    nodes.append(node)
                continue
            tag = m.group('tag')
            if not tag:
                continue
            if ':' in tag:
                # Already namespace-prefixed — pass through to xpath with
                # SVG and inkscape NSS bound.
                nodes += self.xpath('//' + tag)
            else:
                nodes += self.xpath('//svg:' + tag)
        return nodes

    def select_first(self, selector):
        for n in self.select(selector):
            return n
        return None

    def xpath(self, expr):
        nsmap = {'svg': SVG_NS, 'param': PARAM_NS}
        # Pull in any namespaces declared on the SVG root so xpath like
        # //inkscape:label works.
        root = self.document.getroot()
        for prefix, uri in (root.nsmap or {}).items():
            if prefix and prefix not in nsmap:
                nsmap[prefix] = uri
        return self.document.xpath(expr, namespaces=nsmap)


class IdCompletionProvider(GObject.GObject, GtkSource.CompletionProvider):
    """When the user types '#' mid-line in the script editor, this
    provider offers matching SVG element ids as completion candidates.
    Useful for writing references like `ink.select_first('#c1')` without
    having to remember exact ids."""

    def __init__(self, parent_window):
        GObject.GObject.__init__(self)
        self._parent_window = parent_window

    def do_get_name(self):
        return 'SVG ids'

    def do_get_priority(self):
        return 90

    def do_get_activation(self):
        return GtkSource.CompletionActivation.INTERACTIVE

    @staticmethod
    def _line_text_to_cursor(it):
        """Return (line_text_up_to_cursor, cursor_line_offset)."""
        cursor = it.copy()
        line_start = cursor.copy()
        line_start.set_line_offset(0)
        return line_start.get_text(cursor), cursor.get_line_offset()

    def _trigger_info(self, iter_):
        """Find the most recent '#' on the current line that is NOT at
        column 0 and that has only id-safe characters between it and the
        cursor. Returns (prefix, hash_column) or (None, None)."""
        line_text, cursor_col = self._line_text_to_cursor(iter_)
        if not line_text:
            return None, None
        idx = -1
        for i in range(len(line_text) - 1, 0, -1):  # stop at column 1
            if line_text[i] == '#':
                idx = i
                break
        if idx < 0:
            return None, None
        prefix = line_text[idx + 1:]
        # Only treat as a trigger if everything after '#' is id-safe.
        if any(c.isspace() or c in '"\'`,;)]}' for c in prefix):
            return None, None
        return prefix, idx

    def do_populate(self, context):
        result = context.get_iter()
        if isinstance(result, tuple):
            success, it = result
            if not success:
                context.add_proposals(self, [], True)
                return
        else:
            it = result
        if it is None:
            context.add_proposals(self, [], True)
            return
        prefix, _hash_col = self._trigger_info(it)
        if prefix is None:
            context.add_proposals(self, [], True)
            return
        doc = getattr(self._parent_window, 'document', None)
        if doc is None:
            context.add_proposals(self, [], True)
            return
        prefix_lower = prefix.lower()
        matches = []
        for elem in doc.iter():
            eid = elem.get('id')
            if not eid or not eid.lower().startswith(prefix_lower):
                continue
            tag = etree.QName(elem.tag).localname
            matches.append((eid, tag))
        matches.sort(key=lambda x: x[0])
        items = []
        for eid, tag in matches[:30]:
            label = '%s   <%s>' % (eid, tag)
            items.append(GtkSource.CompletionItem.new(
                label, eid, None, 'id of <%s> in current SVG' % tag))
        context.add_proposals(self, items, True)

    def do_activate_proposal(self, proposal, iter_):
        buf = iter_.get_buffer()
        cursor = buf.get_iter_at_mark(buf.get_insert())
        _prefix, hash_col = self._trigger_info(cursor)
        if hash_col is None:
            return False
        replace_start = cursor.copy()
        replace_start.set_line_offset(hash_col + 1)  # right after '#'
        # GtkSource.CompletionItem stores the inserted text under "text".
        text = proposal.get_text()
        if not text:
            return False
        buf.delete(replace_start, cursor)
        buf.insert(replace_start, text, -1)
        return True


class ColorPickerProvider(GObject.GObject, GtkSource.CompletionProvider):
    """A GtkSourceView completion provider that, when the user is typing
    something that prefixes 'pick color', offers a single 'pick color...'
    proposal. Activating the proposal opens a Gtk.ColorChooserDialog and
    inserts the chosen color as a hex literal (e.g. '#ff0000') at the
    cursor, replacing whatever prefix the user had typed."""

    TRIGGER = 'pick color'

    def __init__(self, parent_window):
        GObject.GObject.__init__(self)
        self._parent_window = parent_window

    def do_get_name(self):
        return 'Color picker'

    def do_get_priority(self):
        return 100

    def do_get_activation(self):
        return GtkSource.CompletionActivation.INTERACTIVE

    def _prefix_at(self, context):
        """Return the trailing run of chars at the cursor position. The
        Python binding for context.get_iter() returns either a single
        Gtk.TextIter (older GtkSource) or a (success, iter) tuple
        (newer). Handle both."""
        result = context.get_iter()
        if isinstance(result, tuple):
            success, it = result
            if not success:
                return ''
        else:
            it = result
        if it is None:
            return ''
        end = it.copy()
        start = it.copy()
        start.backward_chars(min(20, start.get_offset()))
        return start.get_text(end)

    def do_populate(self, context):
        prefix = self._prefix_at(context).lower()
        # Match a *trailing* substring of "pick color" — i.e. the user has
        # typed enough to look like the start of the trigger phrase.
        match = False
        for i in range(1, len(self.TRIGGER) + 1):
            if prefix.endswith(self.TRIGGER[:i]):
                match = True
                break
        print('[inspector] color picker populate: prefix=%r match=%s'
              % (prefix[-12:], match), flush=True)
        if not match:
            context.add_proposals(self, [], True)
            return
        item = GtkSource.CompletionItem.new(
            'pick color…',          # label shown in popup
            'pick color…',          # text (we override on activate)
            None,                   # icon
            'Open a color picker and insert the hex value')
        context.add_proposals(self, [item], True)

    def do_activate_proposal(self, _proposal, iter_):
        print('[inspector] color picker activate fired', flush=True)
        # Find how many chars at the cursor match the trigger prefix, so
        # we can replace them with the inserted hex.
        buf = iter_.get_buffer()
        cursor = buf.get_iter_at_mark(buf.get_insert())
        # Walk back up to len(TRIGGER) chars; treat any partial match as
        # the prefix to replace.
        max_back = len(self.TRIGGER)
        start = cursor.copy()
        start.backward_chars(min(max_back, cursor.get_offset()))
        prefix_text = start.get_text(cursor).lower()
        # Find the longest suffix of prefix_text that is also a prefix
        # of TRIGGER.
        replace_len = 0
        for n in range(min(len(prefix_text), len(self.TRIGGER)), 0, -1):
            if prefix_text[-n:] == self.TRIGGER[:n]:
                replace_len = n
                break
        replace_start = cursor.copy()
        replace_start.backward_chars(replace_len)

        dialog = Gtk.ColorChooserDialog(title='Pick a color',
                                        parent=self._parent_window)
        dialog.set_use_alpha(False)
        try:
            response = dialog.run()
            if response == Gtk.ResponseType.OK:
                rgba = dialog.get_rgba()
                hex_color = '"#%02x%02x%02x"' % (
                    int(round(rgba.red * 255)),
                    int(round(rgba.green * 255)),
                    int(round(rgba.blue * 255)),
                )
                buf.delete(replace_start, cursor)
                buf.insert(replace_start, hex_color, -1)
        finally:
            dialog.destroy()
        return True


def parse_css_style(style_str):
    """Parse 'k1:v1;k2:v2;' into a dict. Order is preserved (3.7+ dict)."""
    result = {}
    if not style_str:
        return result
    for part in style_str.split(';'):
        part = part.strip()
        if ':' in part:
            k, v = part.split(':', 1)
            result[k.strip()] = v.strip()
    return result


def serialize_css_style(d):
    return ';'.join('%s:%s' % (k, v) for k, v in d.items())

CSS = b"""
.inspector-headerbar { padding: 6px 12px; }
.run-button { font-weight: 600; padding-left: 14px; padding-right: 14px; }

.selection-header {
    background-color: alpha(@theme_fg_color, 0.04);
    padding: 10px 14px;
    border-bottom: 1px solid alpha(@theme_fg_color, 0.08);
}
.element-id { font-family: monospace; font-size: 15px; font-weight: 600; }
.element-tag { color: alpha(@theme_fg_color, 0.55); font-family: monospace; font-size: 13px; }

.attr-table { padding: 8px 14px 14px 14px; }
.column-header {
    font-size: 11px;
    font-weight: 600;
    color: alpha(@theme_fg_color, 0.5);
    padding-bottom: 4px;
}

.attr-name { font-family: monospace; font-size: 13px; }
.attr-real { color: alpha(@theme_fg_color, 0.55); font-family: monospace; font-size: 13px; }

.param-expr {
    font-family: monospace;
    font-size: 13px;
    background-image: none;
    background-color: alpha(@theme_fg_color, 0.02);
    border: 1px solid alpha(@theme_fg_color, 0.12);
    border-radius: 4px;
    padding: 4px 8px;
    min-height: 0;
}
.param-expr:focus {
    background-color: @theme_base_color;
    border-color: alpha(#1f6feb, 0.5);
}
/* Status tints - chained selectors so they outrank `.param-expr:focus`
 * (same specificity tier; later rule wins) and stay visible while the
 * entry is focused. */
.param-expr.param-edited,
.param-expr.param-edited:focus {
    background-image: none;
    background-color: alpha(#d4a017, 0.15);
    border-color: alpha(#d4a017, 0.6);
}
.param-expr.param-ok,
.param-expr.param-ok:focus {
    background-image: none;
    background-color: alpha(#2e8b57, 0.13);
    border-color: alpha(#2e8b57, 0.6);
}
.param-expr.param-error,
.param-expr.param-error:focus {
    background-image: none;
    background-color: alpha(#cc0000, 0.13);
    border-color: alpha(#cc0000, 0.55);
}

.placeholder {
    color: alpha(@theme_fg_color, 0.5);
    font-style: italic;
    padding: 36px;
}

.status-ok { color: #2e8b57; }
.status-error { color: #cc0000; }
.status-running { color: #1f6feb; }

.terminal-panel {
    background-color: #1e2025;
    border-top: 1px solid alpha(@theme_fg_color, 0.15);
}
.terminal-header {
    color: alpha(#ffffff, 0.45);
    font-size: 11px;
    font-weight: 600;
}
.terminal-clear-btn {
    color: alpha(#ffffff, 0.6);
}
.terminal-clear-btn:hover {
    color: alpha(#ffffff, 0.9);
}
textview.terminal-view,
textview.terminal-view text {
    background-color: #1e2025;
    color: #d4d4d4;
    font-family: monospace;
    font-size: 12px;
    padding: 6px 12px;
}
"""


def run_inkscape_actions(actions, timeout=4.0):
    """Run `inkscape --active-window --actions=<actions>` and return
    (stdout, stderr, returncode). Never raises."""
    try:
        r = subprocess.run(
            ['inkscape', '--active-window', '--actions=' + actions],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return '', 'timeout', -1
    except Exception as e:
        return '', str(e), -1


_SVG_OPEN_TAG_RE = re.compile(rb'<svg\b[^>]*?>', re.DOTALL)


def parse_refresh_snapshot(path):
    """Parse the SVG written by Inkscape's `export-do` action.

    Inkscape's exporter doesn't include `xmlns:param` in the root <svg>
    element even when the document uses `param:foo` attributes, which makes
    the result a parser error for lxml ('Namespace prefix param ... is not
    defined'). We work around it by reading the file as bytes and injecting
    the missing namespace declaration before handing it to lxml.

    Returns an ElementTree or None on failure.
    """
    try:
        with open(path, 'rb') as f:
            content = f.read()
    except Exception:
        return None
    if b'param:' in content and b'xmlns:param=' not in content:
        match = _SVG_OPEN_TAG_RE.search(content)
        if match:
            tag = match.group(0)
            patch = b' xmlns:param="' + PARAM_NS.encode() + b'"'
            new_tag = tag[:-1] + patch + b'>'
            content = content[:match.start()] + new_tag + content[match.end():]
    try:
        return etree.parse(BytesIO(content))
    except Exception as e:
        print('[inspector] refresh-snapshot parse failed: %r' % e, flush=True)
        return None


def export_current_document(target_path, timeout=5.0):
    """Tell the running Inkscape to export its current document state to
    `target_path` as SVG. Non-invasive — does not change the document's
    saved/dirty status. Returns True on success.

    Note: previously included `export-area-drawing` here, but that action
    can subtly resize the page in some Inkscape configurations. For SVG
    exports it is not needed — Inkscape writes the full SVG content
    regardless of export area, which is a raster concern."""
    actions = (
        'export-filename:' + target_path
        + ';export-type:svg'
        + ';export-do'
    )
    _stdout, _stderr, rc = run_inkscape_actions(actions, timeout=timeout)
    return rc == 0 and os.path.exists(target_path) and os.path.getsize(target_path) > 0


def parse_select_list(stdout):
    """select-list output looks like:
        plate_frame cloned: false ref: 1 href: 0 total href: 0
    One id per line, id is the first whitespace-separated token."""
    ids = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        token = line.split(None, 1)[0]
        if token:
            ids.append(token)
    return ids


def escape_action_arg(s):
    """object-set-attribute uses ',' as the name/value separator. Inkscape
    actions don't have a documented escape mechanism; we simply forbid
    commas in expressions and warn at the UI layer."""
    return s


class InspectorWindow(Gtk.ApplicationWindow):

    def __init__(self, app, svg_path):
        super().__init__(application=app, title='Parametric Inspector')
        self.set_default_size(600, 720)

        self.svg_path = svg_path
        # Path used for non-invasive export-do refreshes of the canvas state.
        self._refresh_path = svg_path + '.refresh.svg'
        self.document = etree.parse(svg_path)
        self._ensure_param_ns()

        self._current_selected_id = None
        self._current_selected_elem = None
        self._poll_seq = 0
        # Names extracted from the script tab's source — used as
        # autocomplete candidates in expression entries. Refreshed on
        # every script edit (debounced via the 'changed' signal already
        # being cheap), and after every Run.
        self._available_names = self._default_completion_names()
        # The Python namespace from the most recent successful Run.
        # Used to (re-)color expression entries with green/red based on
        # whether they still evaluate cleanly. Survives re-renders that
        # happen after the post-Run refresh, so the green/red persists
        # until the user edits the expression.
        self._last_run_ns = None
        # Set of attr names (for the currently-selected element) the user
        # has edited since the last Run. Drives the yellow "edited but
        # not yet evaluated" tint.
        self._edited_since_last_run = set()
        # Hash of the most recently rendered (element_id, attribute set,
        # values). Used to short-circuit re-renders when the periodic
        # refresh fetches identical data — eliminates flicker.
        self._last_render_hash = None
        self._stop_polling = threading.Event()
        # Refresh request counter. Every refresh request gets a fresh seq;
        # results that come back with an outdated seq are discarded so we
        # never apply a stale snapshot over a newer one.
        self._refresh_seq = 0

        self._build_css()
        self._build_ui()
        self._install_accelerators()
        self._load_script_into_editor()
        self._render_inspector_for_selection([])
        self._start_selection_poller()

    def _install_accelerators(self):
        """Bind Ctrl+Enter to Run, anywhere in the window. Both the Return
        and KP_Enter keysyms are bound."""
        self._accel_group = Gtk.AccelGroup()
        self.add_accel_group(self._accel_group)
        for keysym in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._accel_group.connect(
                keysym, Gdk.ModifierType.CONTROL_MASK,
                Gtk.AccelFlags.VISIBLE,
                lambda *_args: (self._on_run_clicked(self.run_btn) or True),
            )
        self.run_btn.set_tooltip_text(
            'Execute script, evaluate params, push to canvas (Ctrl+Enter)')

    def _build_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _build_ui(self):
        # Header bar
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.props.title = 'Parametric Inspector'
        header.get_style_context().add_class('inspector-headerbar')
        self.set_titlebar(header)

        self.run_btn = Gtk.Button.new_with_label('Run')
        self.run_btn.get_style_context().add_class('suggested-action')
        self.run_btn.get_style_context().add_class('run-button')
        self.run_btn.set_tooltip_text('Execute script, evaluate params, push to canvas')
        self.run_btn.connect('clicked', self._on_run_clicked)
        header.pack_end(self.run_btn)

        self.status_label = Gtk.Label(label='Ready')
        header.pack_start(self.status_label)

        # Vertical split: tabs on top, terminal output on bottom
        self.main_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.main_paned.set_wide_handle(True)

        self.notebook = Gtk.Notebook()
        self.notebook.set_tab_pos(Gtk.PositionType.TOP)
        self.notebook.append_page(self._build_inspector_tab(), Gtk.Label(label='Inspector'))
        self.notebook.append_page(self._build_script_tab(), Gtk.Label(label='Script'))
        self.main_paned.pack1(self.notebook, resize=True, shrink=False)

        self.main_paned.pack2(self._build_terminal_panel(),
                              resize=False, shrink=True)

        self.add(self.main_paned)

        # Defer initial split position until the window has been allocated,
        # otherwise get_default_size returns the requested size which is
        # fine here but the position would be set before the paned itself
        # has any allocation.
        GLib.idle_add(self._set_initial_paned_position)

        self.connect('destroy', self._on_destroy)

    def _set_initial_paned_position(self):
        # Top 2/3 for tabs, bottom 1/3 for terminal.
        _w, h = self.get_default_size()
        self.main_paned.set_position(int(h * 0.66))
        return False

    def _build_terminal_panel(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.get_style_context().add_class('terminal-panel')

        # Header row
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_top(6)
        header.set_margin_bottom(2)
        header.set_margin_start(12)
        header.set_margin_end(8)

        title = Gtk.Label(label='OUTPUT')
        title.set_xalign(0.0)
        title.set_hexpand(True)
        title.get_style_context().add_class('terminal-header')

        clear_btn = Gtk.Button.new_from_icon_name(
            'edit-clear-symbolic', Gtk.IconSize.BUTTON)
        clear_btn.set_tooltip_text('Clear output')
        clear_btn.set_relief(Gtk.ReliefStyle.NONE)
        clear_btn.get_style_context().add_class('terminal-clear-btn')
        clear_btn.connect('clicked', self._on_terminal_clear)

        header.pack_start(title, True, True, 0)
        header.pack_end(clear_btn, False, False, 0)
        outer.pack_start(header, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self.terminal_buffer = Gtk.TextBuffer()
        self.terminal_buffer.create_tag('error',
                                        foreground='#ff6c6c',
                                        weight=Pango.Weight.BOLD)
        self.terminal_buffer.create_tag('info',
                                        foreground='#7eb6e0',
                                        style=Pango.Style.ITALIC)

        self.terminal_view = Gtk.TextView(buffer=self.terminal_buffer)
        self.terminal_view.set_editable(False)
        self.terminal_view.set_cursor_visible(False)
        self.terminal_view.set_monospace(True)
        self.terminal_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.terminal_view.get_style_context().add_class('terminal-view')

        scroller.add(self.terminal_view)
        outer.pack_start(scroller, True, True, 0)

        return outer

    def _on_terminal_clear(self, _btn):
        self._terminal_clear()

    def _terminal_clear(self):
        self.terminal_buffer.set_text('')

    def _terminal_append(self, text, tag=None):
        if not text:
            return
        end = self.terminal_buffer.get_end_iter()
        if tag is not None:
            self.terminal_buffer.insert_with_tags_by_name(end, text, tag)
        else:
            self.terminal_buffer.insert(end, text)
        # Auto-scroll to the new bottom.
        end = self.terminal_buffer.get_end_iter()
        mark = self.terminal_buffer.create_mark(None, end, False)
        self.terminal_view.scroll_to_mark(mark, 0.0, False, 0.0, 0.0)
        self.terminal_buffer.delete_mark(mark)

    # ----- Script tab ------------------------------------------------------

    def _build_script_tab(self):
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        lang_mgr = GtkSource.LanguageManager.get_default()
        python_lang = lang_mgr.get_language('python3') or lang_mgr.get_language('python')
        buf = GtkSource.Buffer.new_with_language(python_lang) if python_lang else GtkSource.Buffer()
        buf.set_highlight_syntax(True)
        buf.set_highlight_matching_brackets(True)

        scheme_mgr = GtkSource.StyleSchemeManager.get_default()
        for name in ('Adwaita-dark', 'oblivion', 'classic'):
            scheme = scheme_mgr.get_scheme(name)
            if scheme:
                buf.set_style_scheme(scheme)
                break

        view = GtkSource.View.new_with_buffer(buf)
        view.set_show_line_numbers(True)
        view.set_highlight_current_line(True)
        view.set_auto_indent(True)
        view.set_indent_on_tab(True)
        view.set_smart_backspace(True)
        view.set_indent_width(4)
        view.set_tab_width(4)
        view.set_insert_spaces_instead_of_tabs(True)
        view.set_monospace(True)
        view.set_show_right_margin(True)
        view.set_right_margin_position(100)

        font_desc = Pango.FontDescription('Monospace 11')
        view.override_font(font_desc)

        scroller.add(view)
        self.script_view = view
        self.script_buffer = buf

        # Register completion providers for the script editor.
        completion = view.get_completion()
        if completion is not None:
            try:
                completion.add_provider(ColorPickerProvider(self))
            except Exception as e:
                print('[inspector] failed to add color picker provider: %r'
                      % e, flush=True)
            try:
                completion.add_provider(IdCompletionProvider(self))
            except Exception as e:
                print('[inspector] failed to add id provider: %r'
                      % e, flush=True)

        return scroller

    def _load_script_into_editor(self):
        node = self._find_main_script_node()
        if node is not None and node.text:
            self.script_buffer.set_text(node.text)
        else:
            self.script_buffer.set_text(
                '# Define your parameters here.\n'
                '# Variables defined here are available as Python expressions\n'
                '# in any element\'s param:* attributes.\n\n'
                'WIDTH = 200\n'
                'HEIGHT = 100\n'
            )
        # Connect AFTER initial load so we don't fire on the seed text.
        self.script_buffer.connect('changed', lambda _b: self._refresh_available_names())
        self._refresh_available_names()

    def _get_script_text(self):
        start = self.script_buffer.get_start_iter()
        end = self.script_buffer.get_end_iter()
        return self.script_buffer.get_text(start, end, True)

    def _find_main_script_node(self):
        for node in self.document.iter():
            tag = etree.QName(node.tag).localname
            if tag == 'script' and node.get('type') == 'text/python':
                return node
        return None

    def _commit_script_to_document(self):
        text = self._get_script_text()
        node = self._find_main_script_node()
        if node is None:
            root = self.document.getroot()
            node = etree.SubElement(root, '{%s}script' % SVG_NS)
            node.set('type', 'text/python')
            node.set('id', 'pyscript_main')
        node.text = text

    # ----- Inspector tab ---------------------------------------------------

    def _build_inspector_tab(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Selection header — element id (left) and tag (right)
        self.sel_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.sel_header.get_style_context().add_class('selection-header')

        self.sel_id_label = Gtk.Label(label='— no selection —')
        self.sel_id_label.set_xalign(0.0)
        self.sel_id_label.set_hexpand(True)
        self.sel_id_label.set_selectable(True)
        self.sel_id_label.get_style_context().add_class('element-id')

        self.sel_tag_label = Gtk.Label(label='')
        self.sel_tag_label.set_xalign(1.0)
        self.sel_tag_label.get_style_context().add_class('element-tag')

        self.sel_header.pack_start(self.sel_id_label, True, True, 0)
        self.sel_header.pack_end(self.sel_tag_label, False, False, 0)
        outer.pack_start(self.sel_header, False, False, 0)

        # Body: scrollable, holds the unified attribute grid
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        body.get_style_context().add_class('attr-table')

        # The grid itself: 3 columns (name | current value | parametric expression).
        # Row 0 is the column header; data rows start at row 1 and are rebuilt
        # on every selection change.
        self.attr_grid = Gtk.Grid()
        self.attr_grid.set_column_spacing(20)
        self.attr_grid.set_row_spacing(6)
        body.pack_start(self.attr_grid, False, False, 0)

        # Empty-state placeholder, swapped in when nothing is selected.
        self.empty_placeholder = Gtk.Label(
            label='Click an element on the Inkscape canvas\n'
                  'to inspect and bind its attributes.')
        self.empty_placeholder.set_justify(Gtk.Justification.CENTER)
        self.empty_placeholder.get_style_context().add_class('placeholder')
        body.pack_start(self.empty_placeholder, True, True, 0)

        scroller.add(body)
        outer.pack_start(scroller, True, True, 0)

        # Map of {attr_local_name: Gtk.Entry} for the currently-rendered
        # element, used by the Run pipeline to highlight per-row errors.
        self._expr_entries = {}

        return outer

    def _build_grid_header(self):
        """Build the column header row (row 0 of self.attr_grid). Called by
        _render_inspector_for_selection after clearing the grid."""
        for col, label in enumerate(('ATTRIBUTE', 'CURRENT', 'PARAMETRIC EXPRESSION')):
            lbl = Gtk.Label(label=label)
            lbl.set_xalign(0.0)
            lbl.get_style_context().add_class('column-header')
            self.attr_grid.attach(lbl, col, 0, 1, 1)

    def _clear_attr_grid(self):
        for child in self.attr_grid.get_children():
            self.attr_grid.remove(child)
        self._expr_entries = {}

    def _update_empty_placeholder(self):
        if self._current_selected_elem is None:
            self.empty_placeholder.show()
            self.attr_grid.hide()
        else:
            self.empty_placeholder.hide()
            self.attr_grid.show()

    # ----- Autocomplete --------------------------------------------------

    @staticmethod
    def _default_completion_names():
        """A small set of useful built-ins for math/geometry. Always
        included alongside whatever the user defined in the script."""
        return sorted({
            'abs', 'min', 'max', 'round', 'len',
            'int', 'float', 'sum', 'pow', 'range', 'sorted',
        })

    def _refresh_available_names(self):
        """Parse the script and extract top-level identifiers (assignments,
        function/class defs, imports). On SyntaxError, keep the previous
        list — the script is mid-edit."""
        text = self._get_script_text()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return
        names = set(self._default_completion_names())
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        names.add(tgt.id)
                    elif isinstance(tgt, (ast.Tuple, ast.List)):
                        for el in tgt.elts:
                            if isinstance(el, ast.Name):
                                names.add(el.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    names.add(alias.asname or alias.name)
        self._available_names = sorted(names)
        # Push the new list to every visible expression entry's completion.
        for entry in self._expr_entries.values():
            comp = entry.get_completion()
            if comp is None:
                continue
            model = comp.get_model()
            if model is None:
                continue
            model.clear()
            for n in self._available_names:
                model.append([n])

    @staticmethod
    def _current_word_prefix(entry):
        """Return the trailing identifier-like word ending at the cursor.
        E.g. 'WIDTH/2' with cursor at end -> '2', 'foo + bar' -> 'bar'."""
        text = entry.get_text()
        pos = entry.get_position()
        i = pos - 1
        while i >= 0 and (text[i].isalnum() or text[i] == '_'):
            i -= 1
        return text[i + 1:pos]

    def _setup_entry_completion(self, entry):
        completion = Gtk.EntryCompletion()
        store = Gtk.ListStore(str)
        for name in self._available_names:
            store.append([name])
        completion.set_model(store)
        completion.set_text_column(0)
        completion.set_minimum_key_length(1)
        completion.set_popup_completion(True)
        completion.set_inline_completion(False)
        completion.set_inline_selection(False)

        def match_func(_comp, _key, iter_):
            prefix = self._current_word_prefix(entry)
            if not prefix:
                return False
            candidate = store.get_value(iter_, 0)
            return candidate.lower().startswith(prefix.lower())

        completion.set_match_func(match_func)

        def on_match_selected(_comp, model, iter_):
            candidate = model.get_value(iter_, 0)
            text = entry.get_text()
            pos = entry.get_position()
            prefix = self._current_word_prefix(entry)
            start = pos - len(prefix)
            new_text = text[:start] + candidate + text[pos:]
            # Block the 'changed' handler we'd otherwise fire twice.
            entry.set_text(new_text)
            entry.set_position(start + len(candidate))
            return True  # we handled the selection

        completion.connect('match-selected', on_match_selected)
        entry.set_completion(completion)

    def _apply_run_colors_to_entries(self):
        """Tint every visible expression entry:
          • yellow (param-edited) if the user changed it since the last Run
            (or there has been no Run yet)
          • green (param-ok) if it evaluates cleanly against the last Run's
            namespace
          • red (param-error) if it raises during eval
          • no class if empty
        Called right after Run and on every re-render so the tint persists
        through refreshes."""
        for attr_name, entry in self._expr_entries.items():
            ctx_style = entry.get_style_context()
            ctx_style.remove_class('param-edited')
            ctx_style.remove_class('param-ok')
            ctx_style.remove_class('param-error')
            expr = entry.get_text()
            if not expr:
                entry.set_tooltip_text('')
                continue
            # Edited since last Run, or never run — show as pending.
            if self._last_run_ns is None or attr_name in self._edited_since_last_run:
                ctx_style.add_class('param-edited')
                entry.set_tooltip_text('Not yet evaluated — press Run')
                continue
            try:
                value = eval(expr, self._last_run_ns)
            except Exception as e:
                ctx_style.add_class('param-error')
                entry.set_tooltip_text('%s: %s' % (type(e).__name__, e))
            else:
                ctx_style.add_class('param-ok')
                entry.set_tooltip_text('= %r' % (value,))

    def _on_expr_entry_changed(self, entry, attr_name):
        """User edited the expression cell for attr_name. Update the bound
        element's `param:<attr_name>` attribute. Empty value means
        unbind. Marks the entry yellow (edited, not yet evaluated)."""
        elem = self._current_selected_elem
        if elem is None:
            print('[inspector] expr changed for %s but no current elem'
                  % attr_name, flush=True)
            return
        print('[inspector] expr changed: %s = %r' % (attr_name, entry.get_text()),
              flush=True)
        text = entry.get_text()
        if text:
            elem.attrib[PARAM_PREFIX + attr_name] = text
        else:
            elem.attrib.pop(PARAM_PREFIX + attr_name, None)
        self._edited_since_last_run.add(attr_name)
        ctx = entry.get_style_context()
        ctx.remove_class('param-error')
        ctx.remove_class('param-ok')
        if text:
            ctx.add_class('param-edited')
            entry.set_tooltip_text('Not yet evaluated — press Run')
        else:
            ctx.remove_class('param-edited')
            entry.set_tooltip_text('')

    # ----- Selection polling ----------------------------------------------

    def _start_selection_poller(self):
        thread = threading.Thread(target=self._poll_loop, daemon=True)
        thread.start()
        GLib.timeout_add(PERIODIC_REFRESH_MS, self._on_periodic_refresh)

    def _any_expr_entry_focused(self):
        for entry in self._expr_entries.values():
            if entry.has_focus():
                return True
        return False

    def _on_periodic_refresh(self):
        """Background tick that re-pulls the document from Inkscape so
        canvas edits to the *same* selected element (e.g. adding a stroke
        via Fill & Stroke) show up in the inspector. Skipped if the user
        is mid-edit in an expression field — we don't want to wipe their
        cursor position."""
        if self._any_expr_entry_focused():
            return True
        if self._current_selected_id is None:
            return True
        ids = [self._current_selected_id]
        self._refresh_document_async(
            then=lambda: self._render_inspector_for_selection(ids))
        return True  # keep periodic timer alive

    def _poll_loop(self):
        while not self._stop_polling.is_set():
            stdout, _stderr, rc = run_inkscape_actions('select-list', timeout=2.0)
            if rc == 0:
                ids = parse_select_list(stdout)
                self._poll_seq += 1
                seq = self._poll_seq
                GLib.idle_add(self._on_selection_polled, ids, seq)
            time.sleep(POLL_INTERVAL_MS / 1000.0)

    def _on_selection_polled(self, ids, seq):
        if seq != self._poll_seq:
            return False
        new_id = ids[0] if ids else None
        if new_id != self._current_selected_id:
            self._current_selected_id = new_id
            # Refresh document from Inkscape so freshly-created elements and
            # any canvas edits since the inspector launched are picked up.
            self._refresh_document_async(
                then=lambda: self._render_with_fallback(ids))
        return False

    def _render_with_fallback(self, ids):
        """Render for the given selection. If the selected element wasn't
        found in our refreshed cache (Inkscape's export-do may lag behind
        very recent canvas changes by a tick), trigger ONE more refresh and
        try again."""
        self._render_inspector_for_selection(ids)
        if ids and self._current_selected_elem is None:
            # Element wasn't in cache. Retry once with a fresh export-do.
            self._refresh_document_async(
                then=lambda: self._render_inspector_for_selection(ids))

    def _refresh_document_async(self, then=None):
        """Spawn a background export-do call to fetch the current state of
        the running Inkscape document. NOT debounced — every request goes
        through. Out-of-order results are discarded via seq comparison so
        only the latest snapshot is ever applied."""
        self._refresh_seq += 1
        my_seq = self._refresh_seq

        def worker():
            ok = export_current_document(self._refresh_path, timeout=4.0)
            GLib.idle_add(self._on_refresh_done, ok, then, my_seq)

        threading.Thread(target=worker, daemon=True).start()

    def _on_refresh_done(self, ok, then, my_seq):
        # Only apply the *document update* for the latest in-flight refresh
        # — older results would overwrite a newer snapshot. But ALWAYS run
        # the caller's `then` callback, even when our seq is stale: the Run
        # pipeline chains through a refresh and would otherwise stall.
        if ok and my_seq == self._refresh_seq:
            new_doc = parse_refresh_snapshot(self._refresh_path)
            if new_doc is not None:
                pending = self._snapshot_param_edits()
                self.document = new_doc
                self._ensure_param_ns()
                self._reapply_param_edits(pending)
        if then:
            then()
        return False

    def _snapshot_param_edits(self):
        """Capture the current param:* state across all elements as
        {element_id: {local_attr_name: expression}} so we can re-apply it
        after a refresh replaces self.document. Empty-expr orphans (left
        behind by partial-typing in the inspector) are dropped."""
        snap = {}
        for elem in self.document.iter():
            eid = elem.get('id')
            if not eid:
                continue
            for qname, val in elem.attrib.items():
                if not qname.startswith(PARAM_PREFIX):
                    continue
                if not val:
                    continue
                snap.setdefault(eid, {})[qname[len(PARAM_PREFIX):]] = val
        return snap

    def _reapply_param_edits(self, snap):
        for eid, attrs in snap.items():
            elem = self.document.find('.//*[@id="%s"]' % eid)
            if elem is None:
                continue
            for local, expr in attrs.items():
                elem.attrib[PARAM_PREFIX + local] = expr

    def _render_inspector_for_selection(self, ids):
        if not ids:
            self._clear_attr_grid()
            self._current_selected_elem = None
            self.sel_id_label.set_text('— no selection —')
            self.sel_tag_label.set_text('')
            self._update_empty_placeholder()
            self._last_render_hash = None
            return

        elem_id = ids[0]
        elem = self.document.find('.//*[@id="%s"]' % elem_id)
        self._current_selected_elem = elem

        if elem is None:
            self._clear_attr_grid()
            self.sel_id_label.set_text('#%s' % elem_id)
            self.sel_tag_label.set_text('(refreshing…)')
            self._update_empty_placeholder()
            self._last_render_hash = None
            return

        # Short-circuit if nothing visible has changed since the last render.
        # The periodic refresh fires every ~1.5s and would otherwise rebuild
        # the grid every tick, causing visible flicker as Gtk briefly paints
        # entries without their tint classes applied.
        attr_signature = (
            elem_id,
            tuple(sorted((q, v) for q, v in elem.attrib.items())),
        )
        if attr_signature == self._last_render_hash:
            self._apply_run_colors_to_entries()
            return
        self._last_render_hash = attr_signature

        # We're committed to a full rebuild — clear the grid first.
        self._clear_attr_grid()

        tag = etree.QName(elem.tag).localname
        self.sel_id_label.set_text('#%s' % elem_id)
        self.sel_tag_label.set_text('<%s>' % tag)

        # Header row first.
        self._build_grid_header()

        # Build a unified view of attributes:
        #   real_attrs[local_name]   = current value (string)
        #   param_attrs[local_name]  = parametric expression (string)
        # Skip Inkscape/sodipodi internal namespaces — they pollute the view.
        real_attrs = {}
        param_attrs = {}
        for qname, val in elem.attrib.items():
            qn = etree.QName(qname)
            ns = qn.namespace
            local = qn.localname
            if ns == PARAM_NS:
                if val:  # drop orphan empty-expr param attrs (defensive)
                    param_attrs[local] = val
                else:
                    elem.attrib.pop(qname, None)
            elif ns is None or ns == SVG_NS:
                real_attrs[local] = val
            # Ignore inkscape:, sodipodi: etc.

        # Inkscape stores stroke / fill / opacity / font-* etc. inside the
        # `style="..."` attribute rather than as top-level XML attrs. Expand
        # those into virtual rows so they're individually inspectable and
        # bindable. The original style attribute is hidden from the table.
        style_str = real_attrs.pop('style', '')
        if style_str:
            for prop, prop_val in parse_css_style(style_str).items():
                # Don't overwrite a real attribute that already has the same
                # name (rare but possible — direct attr wins for display).
                if prop not in real_attrs:
                    real_attrs[prop] = prop_val

        # Union the two key sets so attrs that have a binding but no real
        # value (rare) still show up.
        all_names = sorted(set(real_attrs) | set(param_attrs))

        for row_idx, name in enumerate(all_names, start=1):
            self._build_attr_row(row_idx, name,
                                 real_attrs.get(name, ''),
                                 param_attrs.get(name, ''))

        self.attr_grid.show_all()
        self._update_empty_placeholder()
        # Persist last Run's coloring across the re-render that follows
        # every refresh.
        self._apply_run_colors_to_entries()

    def _build_attr_row(self, row_idx, attr_name, current_value, expression):
        # Column 0: attribute name (monospace, bold-ish)
        name_label = Gtk.Label(label=attr_name)
        name_label.set_xalign(0.0)
        name_label.set_selectable(True)
        name_label.get_style_context().add_class('attr-name')
        self.attr_grid.attach(name_label, 0, row_idx, 1, 1)

        # Column 1: current value (gray, ellipsized, selectable)
        value_label = Gtk.Label(label=str(current_value))
        value_label.set_xalign(0.0)
        value_label.set_max_width_chars(28)
        value_label.set_ellipsize(Pango.EllipsizeMode.END)
        value_label.set_selectable(True)
        value_label.set_tooltip_text(str(current_value) if current_value else '')
        value_label.get_style_context().add_class('attr-real')
        self.attr_grid.attach(value_label, 1, row_idx, 1, 1)

        # Column 2: parametric expression entry
        expr_entry = Gtk.Entry()
        expr_entry.set_text(expression)
        expr_entry.set_placeholder_text('')
        expr_entry.set_hexpand(True)
        expr_entry.set_width_chars(20)
        expr_entry.get_style_context().add_class('param-expr')
        expr_entry.connect('changed', self._on_expr_entry_changed, attr_name)
        self._setup_entry_completion(expr_entry)
        self.attr_grid.attach(expr_entry, 2, row_idx, 1, 1)

        self._expr_entries[attr_name] = expr_entry

    # ----- Run pipeline ----------------------------------------------------

    def _set_status(self, text, kind='ok'):
        self.status_label.set_text(text)
        ctx = self.status_label.get_style_context()
        for k in ('status-ok', 'status-error', 'status-running'):
            ctx.remove_class(k)
        ctx.add_class('status-' + kind)

    def _on_run_clicked(self, _btn):
        self._set_status('Running…', 'running')
        self.run_btn.set_sensitive(False)
        # Pull the freshest state from Inkscape before running so newly-created
        # or recently-renamed elements are picked up. Pending in-memory param
        # edits are preserved by the refresh logic.
        self._refresh_document_async(then=self._do_run)

    def _do_run(self):
        # Per the spec: terminal output is reset on every Run.
        self._terminal_clear()
        try:
            self._commit_script_to_document()
            ctx = self._build_namespace()
            # Refresh autocomplete now that we know the script parses.
            self._refresh_available_names()
        except SyntaxError as e:
            msg = 'SyntaxError on line %s: %s\n' % (e.lineno, e.msg)
            self._terminal_append(msg, tag='error')
            self._set_status('Script syntax error: line %s: %s' % (e.lineno, e.msg), 'error')
            self.run_btn.set_sensitive(True)
            return False
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self._terminal_append(tb, tag='error')
            self._set_status('Script error: %s: %s' % (type(e).__name__, e), 'error')
            self.run_btn.set_sensitive(True)
            return False

        # Save the namespace so colors can be re-applied after the post-Run
        # refresh re-renders the grid (otherwise the green/red would flash
        # and disappear). _apply_run_colors_to_entries does the visual work.
        self._last_run_ns = ctx
        # All current edits are now incorporated — yellow → green/red.
        self._edited_since_last_run.clear()
        self._apply_run_colors_to_entries()

        # Group actions by element id. We can't just append
        # `select-by-id` per attribute because Inkscape's `select-by-id`
        # action is ADDITIVE — without an explicit `select-clear` between
        # element groups, attributes from element A would also be applied
        # to element B (selection grows as we go).
        push_count = 0
        error_count = 0
        skipped_commas = 0
        error_details = []
        skip_root_dims = 0
        root = self.document.getroot()
        actions_by_elem = {}  # elem_id -> list of action strings (no select)

        for elem in self.document.iter():
            elem_id = elem.get('id')
            if not elem_id:
                continue
            for qname, expr in list(elem.attrib.items()):
                if not qname.startswith(PARAM_PREFIX):
                    continue
                local = qname[len(PARAM_PREFIX):]

                # Guard: never push width/height/viewBox to the SVG root —
                # that resizes the page, which is almost never what the user
                # intends from a parametric binding.
                if elem is root and local in ('width', 'height', 'viewBox'):
                    skip_root_dims += 1
                    error_details.append(
                        '#%s param:%s skipped (would resize page)' % (elem_id, local))
                    continue

                try:
                    value = eval(expr, ctx)
                except Exception as e:
                    error_count += 1
                    error_details.append(
                        '#%s param:%s = %r -> %s: %s' % (elem_id, local, expr, type(e).__name__, e))
                    continue
                value_str = str(value)
                # Inkscape action parser splits NAME,VALUE on the first comma;
                # values/expressions containing commas can't be pushed safely.
                if ',' in value_str or ',' in expr:
                    skipped_commas += 1
                    error_details.append(
                        '#%s param:%s skipped (comma in value/expr): %r -> %r' % (elem_id, local, expr, value_str))
                    continue
                group = actions_by_elem.setdefault(elem_id, [])
                group.append('object-set-attribute:param:%s,%s' % (local, expr))
                # CSS properties (fill, stroke, stroke-width, opacity, ...)
                # live inside the `style="..."` attribute. Inkscape's
                # `object-set-property` action knows how to merge into style;
                # `object-set-attribute` would just create a redundant XML
                # attribute that may or may not render.
                if local in CSS_PROPERTIES:
                    group.append('object-set-property:%s,%s' % (local, value_str))
                else:
                    group.append('object-set-attribute:%s,%s' % (local, value_str))
                push_count += 1

        if error_details:
            print('[inspector] Run errors:', flush=True)
            for d in error_details:
                print('  - ' + d, flush=True)

        # Build the final action chain. Before each element's group of
        # set-attribute calls, clear the selection and reselect just that
        # element — otherwise selection accumulates and later groups end
        # up overwriting attributes on every previously-selected element.
        actions = []
        for elem_id, group_actions in actions_by_elem.items():
            actions.append('select-clear')
            actions.append('select-by-id:' + elem_id)
            actions.extend(group_actions)

        # Append a chained export-do at the end so the post-Run snapshot
        # comes out of the same single CLI call. This guarantees the
        # exported SVG reflects the actions we just pushed (no race with
        # concurrent polling refreshes).
        actions.extend([
            'export-filename:' + self._refresh_path,
            'export-type:svg',
            'export-do',
        ])

        print('[inspector] Run pushing %d action steps for %d bindings' % (
            len(actions), push_count), flush=True)
        _stdout, stderr, rc = run_inkscape_actions(';'.join(actions), timeout=15.0)
        if rc != 0:
            self._set_status('Inkscape action failed: %s' % stderr.strip()[:120], 'error')
            self.run_btn.set_sensitive(True)
            return False

        # Read the post-action snapshot directly from disk. Skip the
        # async refresh — we already have the fresh state.
        new_doc = parse_refresh_snapshot(self._refresh_path)
        if new_doc is not None:
            pending = self._snapshot_param_edits()
            self.document = new_doc
            self._ensure_param_ns()
            self._reapply_param_edits(pending)
            if self._current_selected_id:
                cur = self.document.find(
                    './/*[@id="%s"]' % self._current_selected_id)
                if cur is not None:
                    attrs = {etree.QName(k).localname: v
                             for k, v in cur.attrib.items()
                             if not k.startswith(PARAM_PREFIX)}
                    print('[inspector] post-Run #%s attrs: %r'
                          % (self._current_selected_id, attrs), flush=True)
            # Force a re-render even if the hash hadn't changed —
            # we want to reflect the post-Run state.
            self._last_render_hash = None
        else:
            print('[inspector] post-Run snapshot unreadable: %s' %
                  self._refresh_path, flush=True)

        msgs = ['%d pushed' % push_count]
        if error_count:
            msgs.append('%d eval errors' % error_count)
        if skipped_commas:
            msgs.append('%d comma-skipped' % skipped_commas)
        if skip_root_dims:
            msgs.append('%d root-dim-skipped' % skip_root_dims)
        kind = 'error' if (error_count or skipped_commas or skip_root_dims) else 'ok'
        self._set_status('Run done — ' + ', '.join(msgs), kind)
        self.run_btn.set_sensitive(True)
        # Re-render with the freshly-parsed document so the Current column
        # shows the post-Run values.
        if self._current_selected_id:
            self._render_inspector_for_selection([self._current_selected_id])
        return False

    def _build_namespace(self):
        """Compile + execute the script and return its namespace.

        Captures anything written to stdout/stderr during exec into the
        terminal panel. Re-raises on script error so the caller can also
        handle it (set status bar, log traceback)."""
        text = self._get_script_text()
        # Compile first so SyntaxError carries a precise lineno.
        compile(text, '<inspector script>', 'exec')
        ns = {
            '__name__': '__inspector__',
            'ink': _ScriptInk(self.document),
        }
        import io
        stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout_buf, stderr_buf
        try:
            exec(text, ns, ns)
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
            captured_out = stdout_buf.getvalue()
            captured_err = stderr_buf.getvalue()
        if captured_out:
            self._terminal_append(captured_out)
        if captured_err:
            self._terminal_append(captured_err, tag='error')
        return ns

    # ----- Misc ------------------------------------------------------------

    def _ensure_param_ns(self):
        root = self.document.getroot()
        if 'param' in root.nsmap:
            return
        new_nsmap = dict(root.nsmap)
        new_nsmap['param'] = PARAM_NS
        new_root = etree.Element(root.tag, nsmap=new_nsmap, attrib=root.attrib)
        for child in root:
            new_root.append(child)
        self.document._setroot(new_root)

    def _on_destroy(self, _w):
        self._stop_polling.set()


def main():
    print('[inspector] main() starting; argv=%r' % sys.argv, flush=True)
    print('[inspector] DISPLAY=%r WAYLAND_DISPLAY=%r' % (
        os.environ.get('DISPLAY'), os.environ.get('WAYLAND_DISPLAY')), flush=True)

    if len(sys.argv) < 2:
        print('[inspector] usage: inspector.py <svg-path>', file=sys.stderr, flush=True)
        sys.exit(2)
    svg_path = sys.argv[1]
    if not os.path.exists(svg_path):
        print('[inspector] file not found: %s' % svg_path, file=sys.stderr, flush=True)
        sys.exit(2)

    print('[inspector] creating Gtk.Application', flush=True)
    app = Gtk.Application(application_id='com.fdmtech.inkscape.pyscript.inspector',
                          flags=0)

    def on_activate(application):
        print('[inspector] on_activate fired', flush=True)
        try:
            win = InspectorWindow(application, svg_path)
            print('[inspector] window built; calling show_all', flush=True)
            win.show_all()
            print('[inspector] show_all returned', flush=True)
        except Exception as e:
            import traceback
            print('[inspector] EXCEPTION building window:', flush=True)
            traceback.print_exc()
            raise

    app.connect('activate', on_activate)
    print('[inspector] calling app.run([])', flush=True)
    rc = app.run([])
    print('[inspector] app.run returned %r' % rc, flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        print('[inspector] UNCAUGHT EXCEPTION at top level:', flush=True)
        traceback.print_exc()
        raise
