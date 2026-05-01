# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-2.0-or-later
"""
main.py
pyscript core inkscape extension.

Copyright (C) 2019 Frank Martinez <mnesarco at gmail.com>
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
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
"""

import gi

gi.require_version('Gtk', '3.0')
gi.require_version('GtkSource', '3.0')

import inkex, copy, ast, sys, traceback, re
from pyscript import ui, svg
from lxml import etree
from inkex.deprecated import deprecate

version = "0.1"

SELECTOR = re.compile(r'#(?P<ident>[a-zA-Z0-9._\-:]+)|(?P<tag>(\w+:)*\w+)')

PARAM_NS = 'http://fdmtech.com/inkscape/param'
PARAM_PREFIX = '{%s}' % PARAM_NS

class PYScriptExceptionInfo(object):
    def __init__(self, lineno, message):
        self.lineno = lineno
        self.message = message

class PYScriptInfo(object):

    def __init__(self, node):
        self.node = node
        self.id = node.attrib['id']
        self.label = self.id[9:] if self.id.startswith('pyscript_') else self.id
        self.is_main = self.id == 'pyscript_main'

    def source(self, source = None):
        if source is not None:
            self.node.text = source
        return self.node.text

    def compile(self):
        try:
            ast.parse(self.source(), self.label)
            return [True, self, None]
        except SyntaxError as err:
            error_class = err.__class__.__name__
            detail = err.args[0]
            line_number = err.lineno
            message = "%s at line %d of %s: %s" % (error_class, line_number, self.label, detail)
            return [False, self, PYScriptExceptionInfo(lineno=line_number, message=message)]

    def execute(self, gctx, lctx):
        try:
            exec(self.source(), gctx, lctx)            
            return [True, self, None]
        except SyntaxError as err:
            error_class = err.__class__.__name__
            detail = err.args[0]
            line_number = err.lineno
        except Exception as err:
            error_class = err.__class__.__name__
            detail = err.args[0] + ' ' + traceback.format_exc()
            cl, exc, tb = sys.exc_info()
            line_number = traceback.extract_tb(tb)[-1][1]
            del(cl, exc, tb)
        message = "%s at line %d of %s: %s" % (error_class, line_number, self.label, detail)
        return [False, self, PYScriptExceptionInfo(lineno=line_number, message=message)]

class PYScript(inkex.EffectExtension):

    def __init__(self, edit = True):
        inkex.EffectExtension.__init__(self)
        self.__edit = edit
        self.scripts = dict()

    @deprecate
    def getElementById(self, id_):
        """select_first('#%s' % id)"""
        return self.svg.getElementById(id_)

    def select(self, selector):
        nodes = []
        for m in SELECTOR.finditer(selector):
            ident = m.group('ident')
            if ident:
                nodes += self.xpath("//*[@id='%s']" % ident)
            else:
                tag = m.group('tag')
                if tag:
                    if ':' in tag:
                        nodes += self.xpath("//%s" % tag)
                    else:
                        nodes += self.xpath("//svg:%s" % tag)
        return nodes

    def select_first(self, selector):
        nodes = self.select(selector)
        if nodes:
            return nodes[0]

    def xpath(self, expr):
        return self.document.xpath(expr, namespaces=inkex.NSS)

    def create_script(self, sid = 'pyscript_main'):
        root = self.document.getroot()
        node = etree.SubElement(root, 'script', {'id' : sid, 'type': 'text/python'})
        script = PYScriptInfo(node)
        script.source("\n".join(['# Script: %s' % script.label,
            '"""', 
            'Extension: pyscript v%s <by Frank D. Martinez>' % version,
            'You can write valid python code here.',
            'Your code will be embedded into the document.',
            'Help: https://gitlab.com/mnesarco/inkscape-pyscript',
            '"""']))
        self.scripts[script.id] = script
        return script

    def register_script(self, node):
        script = PYScriptInfo(node)
        self.scripts[script.id] = script
        return script

    def get_all_script_nodes(self):
        root = self.document.getroot()
        nodes = self.document.xpath('//svg:script[@type="text/python"]', namespaces=inkex.NSS)
        if len(nodes) == 0:
            nodes = self.document.xpath('//script[@type="text/python"]', namespaces=inkex.NSS)
        return nodes

    def save_state(self):
        return copy.deepcopy(self.document)

    def restore_state(self, state):
        self.document = state
        self.__reload()

    def __reload(self):
        self.scripts = dict()
        for node in self.get_all_script_nodes():
            self.register_script(node)
        if not ('pyscript_main' in self.scripts):
            self.create_script()

    def compile(self):
        ok = True
        results = []
        for sid, script in self.scripts.items():
            r = script.compile()
            results.append(r)
            ok = ok and r[0]
        return (ok, results)

    def execute(self):
        ok, results = self.compile()
        if ok:
            saved = self.save_state()
            smain = None
            ctx = {'ink' : self}
            sresults = []
            for sid, script in self.scripts.items():
                if script.is_main:
                    smain = script
                else:
                    r = script.execute(ctx, ctx)
                    sresults.append(r)
                    ok = ok and r[0]
            if ok and (smain is not None):
                r = smain.execute(ctx, ctx)
                sresults.append(r)
                ok = ok and r[0]
            if ok:
                self.last_ctx = ctx
                param_errors = self.eval_params(ctx)
                self.last_param_errors = param_errors
                for elem_id, attr, msg in param_errors:
                    sresults.append([False, None, PYScriptExceptionInfo(
                        lineno=0,
                        message="param:%s on #%s: %s" % (attr, elem_id, msg))])
                ok = ok and not param_errors
            if not ok:
                self.restore_state(saved)
            return (ok, sresults)
        else:
            return (ok, results)

    def eval_params(self, ctx):
        """Walk every element, evaluate `param:*` attributes against ctx, and
        write the result into the corresponding non-namespaced attribute.
        Returns a list of (element_id, attr_name, error_message) tuples."""
        errors = []
        root = self.document.getroot()
        if 'param' not in root.nsmap:
            self._declare_param_ns()
        for elem in self.document.iter():
            for qname in list(elem.attrib):
                if not qname.startswith(PARAM_PREFIX):
                    continue
                local = qname[len(PARAM_PREFIX):]
                expr = elem.attrib[qname]
                try:
                    value = eval(expr, ctx)
                except Exception as e:
                    errors.append((elem.get('id', '?'), local, "%s: %s" % (type(e).__name__, e)))
                    continue
                elem.attrib[local] = str(value)
        return errors

    def _declare_param_ns(self):
        """lxml can't add namespace declarations to existing trees. We rebuild
        the root with the param namespace included."""
        from lxml import etree
        root = self.document.getroot()
        if 'param' in root.nsmap:
            return
        new_nsmap = dict(root.nsmap)
        new_nsmap['param'] = PARAM_NS
        new_root = etree.Element(root.tag, nsmap=new_nsmap, attrib=root.attrib)
        for child in root:
            new_root.append(child)
        self.document._setroot(new_root)

    def effect(self):
        self.__reload()
        if self.__edit:
            ide = ui.MainWindow(self)
            ide.show()
        else:
            self.run_script()

    def run_script(self):
        (ok, results) = self.execute()
        if not ok:
            for (ok, script, err) in results:
                if not ok:
                    inkex.errormsg(err.message)

