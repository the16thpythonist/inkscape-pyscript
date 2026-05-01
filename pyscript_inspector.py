#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-2.0-or-later
"""
pyscript_inspector.py
Entry point for the Parametric Inspector. Inkscape invokes this with the
current SVG passed either as a positional file path arg or via stdin (and
optionally `--output FILE` to redirect what would normally go to stdout).

Copyright (C) 2026 Jonas Teufel <jonseb1998@gmail.com>

This file is part of inkscape-pyscript.

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, see <https://www.gnu.org/licenses/>.

We patch xmlns:param into the SVG root if missing, write the SVG back so
Inkscape sees a no-op, then spawn the inspector window as a *detached*
process. The inspector communicates with Inkscape via
`inkscape --active-window --actions=...` only.
"""

import argparse
import os
import subprocess
import sys
import tempfile

from lxml import etree

PARAM_NS = 'http://fdmtech.com/inkscape/param'


def ensure_param_namespace(svg_bytes):
    try:
        root = etree.fromstring(svg_bytes)
    except etree.XMLSyntaxError:
        return svg_bytes
    if 'param' in (root.nsmap or {}):
        return svg_bytes
    new_nsmap = dict(root.nsmap)
    new_nsmap['param'] = PARAM_NS
    new_root = etree.Element(root.tag, nsmap=new_nsmap, attrib=root.attrib)
    for child in root:
        new_root.append(child)
    return etree.tostring(new_root, xml_declaration=True, encoding='UTF-8',
                          standalone=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('input_file', nargs='?', default=None)
    parser.add_argument('--output', default=None)
    # Swallow any --id=... or other arguments Inkscape may pass.
    args, _unknown = parser.parse_known_args()

    if args.input_file:
        with open(args.input_file, 'rb') as f:
            raw = f.read()
    else:
        raw = sys.stdin.buffer.read()

    patched = ensure_param_namespace(raw)

    tmpdir = tempfile.mkdtemp(prefix='pyscript-inspector-')
    svg_path = os.path.join(tmpdir, 'document.svg')
    with open(svg_path, 'wb') as f:
        f.write(patched)

    log_path = os.path.join(tmpdir, 'inspector.log')
    log_f = open(log_path, 'wb')

    here = os.path.dirname(os.path.abspath(__file__))
    inspector_script = os.path.join(here, 'pyscript', 'inspector.py')

    env = dict(os.environ)
    env['PYTHONPATH'] = here + os.pathsep + env.get('PYTHONPATH', '')

    subprocess.Popen(
        [sys.executable, '-u', inspector_script, svg_path],
        env=env,
        start_new_session=True,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )

    # Don't write to stderr — Inkscape pops a "received additional data"
    # dialog if we do. The log path is discoverable via
    #   ls -t /tmp/pyscript-inspector-*/inspector.log | head -1
    _ = log_path  # keep variable alive for clarity

    if args.output:
        with open(args.output, 'wb') as f:
            f.write(patched)
    else:
        sys.stdout.buffer.write(patched)


if __name__ == '__main__':
    main()
