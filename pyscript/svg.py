# -*- coding: utf-8 -*-
"""
svg.py
Utility Svg classes.

Copyright (C) 2019 Frank Martinez <mnesarco at gmail.com>

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
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
"""

import math
from lxml import etree
import collections
import copy
import inkex

from inkex.transforms import Transform
from inkex.paths import Path

class PathObject(object):
    
    def __init__(self, p=[], node=None, style={}, attrib={}):    
        self._p = p
        self._style = style
        self._attrib = attrib
        self._parseNode(node)

    def _abs_point(self, dx, dy):
        (x, y, c) = self.end_point()
        return (x+dx, y+dy)

    def parse(self, d):
        self._p = Path(d).to_arrays()

    def _parseNode(self, node):
        if node is not None:
            self._p = Path(node.attrib['d']).to_arrays()
            self._style = dict(inkex.Style.parse_str(node.attrib['style']))
            self._attrib = copy.copy(node.attrib)
            self._node = node
        else:
            self._node = None

    def create(self, svgdoc, parent, elem_id):
        node = svgdoc.select_first('#%s' % elem_id)
        if node is None:
            attrs = copy.copy(self._attrib)
            attrs['style'] = str(inkex.Style(self._style))
            attrs['id'] = elem_id
            attrs['d'] = str(Path(self._p))
            self._node = etree.SubElement(parent, 'path', attrs)
        else:
            self.commit(node)
            
    def commit(self, node = None):
        if node is None:
            node = self._node
        if node is None:
            raise ValueError('No svg:path node has been selected.')
        if len(self._attrib) > 0:
            node.attrib.update(self._attrib)
        if len(self._style) > 0:
            node.attrib['style'] = str(inkex.Style(self._style))
        if self._p is not None:
            node.attrib['d'] = str(Path(self._p))

    def attrib(self, name, value = None):
        if value is not None:
            self._attrib[name] = str(value)
        return self.attrib[name]

    def style(self, style = None):
        if isinstance(style, collections.abc.Mapping):
            self._style.update(style)
        return self._style

    def rotate(self, a, cx=0, cy=0):
        (x,y,c) = self.start_point()
        self.rotate_abs(a, x+cx, y+cy)
            
    def rotate_abs(self, a, cx=0, cy=0):
        self._p[:] = Path(self._p).rotate(math.degrees(a), (cx, cy)).to_arrays()
            
    def scale(self, fx, fy=None):
        if fy is None:
            fy = fx
        self._p[:] = Path(self._p).scale(fx, fy).to_arrays()

    def translate(self, dx, dy):
        self._p[:] = Path(self._p).translate(dx, dy).to_arrays()

    def start_point(self):
        c, params = self._p[0]
        return (params[0], params[1], c)

    def end_point(self, offset = -1):
        c, params = self._p[offset]
        if c == 'H':
            (px, py) = self.end_point(offset - 1)
            return (params[-1], py, c)
        elif c == 'V':
            (px, py) = self.end_point(offset - 1)
            return (px, params[-1], c)
        elif c == 'Z':
            (px, py, c) = self.start_point()
            return (px, py, c)
        else:
            return (params[-2], params[-1], c)

    def translate_to(self, x, y):
        (sx, sy, c) = self.start_point()
        self._p[:] = Path(self._p).translate(x - sx, y - sy).to_arrays()

    def move(self, dx, dy, mode='M'):
        if len(self._p) > 0:
            (x, y, c_) = self.end_point()
            self.move_to(x+dx, y+dy, mode)
        else:
            self._p = [['M', [dx, dy]]]

    def move_to(self, x, y, mode='M'):
        if len(self._p) > 0:
            self._p.append([mode.upper(), [x, y]])
        else:
            self._p = [['M', [x, y]]]

    def line(self, dx, dy):
        self.move(dx, dy, 'L')

    def line_to(self, x, y):
        self.move_to(x, y, 'L')

    def vertical(self, h):
        self.line(0, h)

    def vertical_to(self, y):
        (ex, ey, c) = self.end_point()
        self.line_to(ex, y)

    def horizontal(self, w):
        self.line(w, 0)

    def horizontal_to(self, x):
        (ex, ey, c) = self.end_point()
        self.line_to(x, ey)        

    def arc(self, rx, ry, a, l, s, dx, dy):
        (x, y) = self._abs_point(dx, dy)
        self.arc_to(rx, ry, a, l, s, x, y)

    def arc_to(self, rx, ry, a, l, s, x, y):
        self._p.append(['A', [rx, ry, a, l, s, x, y]])

    def c_bezier(self, dx0, dy0, dx1, dy1, dx, dy):
        (x0, y0) = self._abs_point(dx0, dy0)
        (x1, y1) = self._abs_point(dx1, dy1)
        (x, y) = self._abs_point(dx, dy)
        self.c_bezier_to(x0, y0, x1, y1, x, y)

    def c_bezier_to(self, x0, y0, x1, y1, x, y):
        self._p.append(['C', [x0, y0, x1, y1, x, y]])

    def q_bezier(self, dx0, dy0, dx, dy):
        (x0, y0) = self._abs_point(dx0, dy0)
        (x, y) = self._abs_point(dx, dy)
        self.q_bezier_to(x0, y0, x, y)

    def q_bezier_to(self, x0, y0, x, y):
        self._p.append(['Q', [x0, y0, x, y]])

    def t_bezier_to(self, x, y):
        c, params = self._p[-1]
        if (c == 'Q'):
            x0 = params[-2] - params[-4]
            y0 = params[-1] - params[-3]
        else:
            x0 = params[-2]
            y0 = params[-1]
        self.q_bezier_to(x0, y0, x, y)

    def t_bezier(self, dx, dy):
        (x, y) = self._abs_point(dx, dy)
        self.t_bezier_to(x, y)

    def s_bezier_to(self, x1, y1, x, y):
        c, params = self._p[-1]
        if (c == 'C'):
            x0 = params[-2] - params[-4]
            y0 = params[-1] - params[-3]
        else:
            x0 = params[-2]
            y0 = params[-1]
        self.c_bezier_to(x0, y0, x1, y1, x, y)

    def s_bezier(self, dx1, dy1, dx, dy):
        (x1, y1) = self._abs_point(dx1, dy1)
        (x, y) = self._abs_point(dx, dy)
        self.s_bezier_to(x1, y1, x, y)

    def rect(self, w, h):
        self.line(w, 0)
        self.line(0, h)
        self.line(-w, 0)
        self.line(0, -h)

    def circle_center(self, r, cx=0, cy=0):
        self.move(cx-r, cy)
        self.arc(r, r, 0, 0, 1, +r, -r)
        self.arc(r, r, 1, 0, 1, +r, +r)
        self.arc(r, r, 1, 0, 1, -r, +r)
        self.arc(r, r, 1, 0, 1, -r, -r)
        self.move(cx+r, cy)

    def circle(self, r):
        self.arc(r, r, 0, 0, 1, +r, -r)
        self.arc(r, r, 1, 0, 1, +r, +r)
        self.arc(r, r, 1, 0, 1, -r, +r)
        self.arc(r, r, 1, 0, 1, -r, -r)

    def close(self):
        self._p.append(['z', []])

