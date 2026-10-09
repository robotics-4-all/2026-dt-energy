"""seg2xmi -- the model-to-model / model-to-text core of the pipeline (same role as wnetc.py in Sourli's thesis).

    .seg --(textX, T2M)--> in-memory model --(Jinja, M2T)--> .xmi  (instance of metamodel/seg_metamodel.ecore)
    .xmi --(pyecore)-------> validated in-memory model --> generate_twin.py

Public API:
    seg_to_xmi(seg_path, xmi_path, grammar_path, templates_dir)    -> writes the XMI
    load_xmi(xmi_path)                                             -> pyecore root (conforms to the Ecore)
    validate(root)                                                 -> list of violated constraints
    xmi_to_view(root)                                              -> light object graph for generate_twin.py
    seg_to_props(textx_model) / xmi_to_props(root)                 -> canonical property dicts (round-trip test)
"""
import os
import re
from xml.sax.saxutils import quoteattr

from jinja2 import Environment, FileSystemLoader
from pyecore.ecore import EAttribute, EReference, EEnumLiteral
from pyecore.resources import ResourceSet, URI
from textx import metamodel_from_file

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ECORE = os.path.join(HERE, 'metamodel', 'seg_metamodel.ecore')
DEFAULT_GRAMMAR = os.path.join(HERE, 'grammar', 'smartenergygrid.tx')
DEFAULT_TEMPLATES = os.path.join(HERE, 'templates')

# textX root list -> Ecore class (order = order of `elements` in the XMI)
ROOT_LISTS = [('substations', 'SubStation'), ('batteries', 'BatteryStorage'), ('transformers', 'Transformer'),
              ('thermalPlants', 'ThermalPlant'), ('solarParks', 'SolarPark'), ('windFarms', 'WindFarm'),
              ('residentials', 'Residential'), ('industrials', 'Industrial')]
LIST_OF_CLASS = {c: l for l, c in ROOT_LISTS}
DIST_CLASSES = {'WeibullDist', 'NormalDist', 'BetaDist', 'LogNormalDist', 'GammaDist', 'PoissonDist'}


# ----------------------------------------------------------------------------- naming differences DSL <-> Ecore
def tx_attr(owner, feature):
    """Ecore feature name -> textX attribute name (the only renames between DSL and Ecore)."""
    if feature == 'symbol':
        return 'name'
    if owner == 'PowerLine' and feature == 'id':
        return 'lineId'
    if owner not in ('PowerLine', 'PowerGrid') and owner not in DIST_CLASSES:
        return {'id': 'elementId', 'name': 'elementName', 'spatialData': 'spatial'}.get(feature, feature)
    return feature


def ecore_attr(owner, attr):
    """textX attribute name -> Ecore feature name (inverse of tx_attr)."""
    for f in ('symbol', 'id', 'name', 'spatialData'):
        pass
    if attr == 'name':
        return 'symbol'
    if owner == 'PowerLine' and attr == 'lineId':
        return 'id'
    return {'elementId': 'id', 'elementName': 'name', 'spatial': 'spatialData'}.get(attr, attr)


def optional_attrs(grammar_path):
    """Attributes the grammar declares optional: a line of the form  ('key:' attr=TYPE)?"""
    text = open(grammar_path, encoding='utf-8').read()
    return set(re.findall(r"\('[^']*'\s*(\w+)=[^)]*\)\?", text))


# ----------------------------------------------------------------------------- Ecore helpers
def load_metamodel(ecore_path=DEFAULT_ECORE):
    rset = ResourceSet()
    pkg = rset.get_resource(URI(ecore_path)).contents[0]
    rset.metamodel_registry[pkg.nsURI] = pkg
    return rset, pkg


def all_features(eclass):
    feats = []
    for sup in reversed(eclass.eAllSuperTypes()):
        feats.extend(sup.eStructuralFeatures)
    feats.extend(eclass.eStructuralFeatures)
    return feats


def _fmt(value, ftype_name):
    if isinstance(value, bool) or ftype_name == 'EBoolean':
        return 'true' if str(value).lower() == 'true' else 'false'
    if ftype_name == 'EFloat':
        return repr(float(value))
    if ftype_name == 'EInt':
        return str(int(value))
    return str(value)


def _node_attrs(obj, ename, pkg, optional, skip=()):
    """(name, value) pairs of the single-valued attributes of a textX object, in Ecore feature order."""
    eclass = pkg.getEClassifier(ename)
    out = []
    for f in all_features(eclass):
        if not isinstance(f, EAttribute) or f.many or f.name in skip:
            continue
        v = getattr(obj, tx_attr(ename, f.name), None)
        if v is None:
            continue
        if f.name in optional and v in ('', 0.0):  # textX fills unset optional attributes with the type default
            continue
        out.append((f.name, _fmt(v, f.eType.name if hasattr(f.eType, 'name') else '')))
    return out


# ----------------------------------------------------------------------------- T2M: textX model -> template context
def _dist_entry(entry, pkg, optional):
    d = entry.distribution
    cname = d.__class__.__name__
    return {'variable': entry.variable, 'cls': cname, 'attrs': _node_attrs(d, cname, pkg, optional)}


def build_context(model, pkg, optional):
    nodes = [(cname, n) for lst, cname in ROOT_LISTS for n in getattr(model, lst, [])]
    index = {id(n): i for i, (_, n) in enumerate(nodes)}
    ctx = {'ns_uri': pkg.nsURI, 'grids': [], 'elements': [], 'global_distributions': [], 'class_distributions': []}
    for g in model.grids:
        ctx['grids'].append({'attrs': _node_attrs(g, 'PowerGrid', pkg, optional)})
    for cname, n in nodes:
        e = {'cls': cname, 'attrs': _node_attrs(n, cname, pkg, optional), 'spatial': None,
             'lines': [], 'distributions': [], 'connected': None}
        if getattr(n, 'spatial', None) is not None:
            e['spatial'] = _node_attrs(n.spatial, 'SpatialData', pkg, optional)
        for ln in getattr(n, 'lines', []):
            e['lines'].append({'attrs': _node_attrs(ln, 'PowerLine', pkg, optional),
                               'source': f'//@elements.{index[id(ln.source)]}',
                               'target': f'//@elements.{index[id(ln.target)]}'})
        for ent in getattr(n, 'distributions', []):
            e['distributions'].append(_dist_entry(ent, pkg, optional))
        if getattr(n, 'connectedTo', None):
            e['connected'] = ' '.join(f'//@elements.{index[id(t)]}' for t in n.connectedTo)
        ctx['elements'].append(e)
    for b in model.globalDistributions:
        ctx['global_distributions'].append({'attrs': _node_attrs(b, 'GlobalDistributions', pkg, optional),
                                            'distributions': [_dist_entry(x, pkg, optional) for x in b.distributions]})
    for b in model.classDistributions:
        ctx['class_distributions'].append({'attrs': _node_attrs(b, 'ClassDistributions', pkg, optional),
                                           'distributions': [_dist_entry(x, pkg, optional) for x in b.distributions]})
    return ctx


# ----------------------------------------------------------------------------- M2T: Jinja
def render_xmi(ctx, templates_dir=DEFAULT_TEMPLATES):
    env = Environment(loader=FileSystemLoader(templates_dir), trim_blocks=False, lstrip_blocks=False)
    env.filters['xmlattrs'] = lambda pairs: ''.join(f' {k}={quoteattr(v)}' for k, v in pairs)
    return env.get_template('xmi/model.xmi.jinja').render(**ctx) + '\n'


def load_textx_metamodel(grammar_path=DEFAULT_GRAMMAR):
    """textX metamodel of the DSL, with `boolean` converted to a real Python bool.

    The grammar rule `boolean: 'true' | 'false'` matches the TEXT, so without this processor textX hands back the
    string 'false', and bool('false') is True (the old pipeline rendered `hasSmartAppliances: false` as True).
    Every place that parses a .seg must build its metamodel through this function."""
    mm = metamodel_from_file(grammar_path)
    mm.register_obj_processors({'boolean': lambda text: text == 'true'})
    return mm


def seg_to_xmi(seg_path, xmi_path, grammar_path=DEFAULT_GRAMMAR, templates_dir=DEFAULT_TEMPLATES,
               ecore_path=DEFAULT_ECORE):
    """Full chain .seg -> XMI. Returns (xmi_path, violations). Raises on parse errors."""
    _, pkg = load_metamodel(ecore_path)
    model = load_textx_metamodel(grammar_path).model_from_file(seg_path)
    text = render_xmi(build_context(model, pkg, optional_attrs(grammar_path)), templates_dir)
    os.makedirs(os.path.dirname(os.path.abspath(xmi_path)), exist_ok=True)
    with open(xmi_path, 'w', encoding='utf-8') as f:
        f.write(text)
    root = load_xmi(xmi_path, ecore_path)          # automatic validation, step 1: loads against the Ecore
    return xmi_path, validate(root)                # step 2: constraints


# ----------------------------------------------------------------------------- XMI side
def load_xmi(xmi_path, ecore_path=DEFAULT_ECORE):
    rset, _ = load_metamodel(ecore_path)
    return rset.get_resource(URI(xmi_path)).contents[0]


def _val(v):
    return v.name if isinstance(v, EEnumLiteral) else v


def _elements(root):
    return list(root.elements)


def validate(root):
    """OCL-style constraints written as Python. Returns [(constraint_id, where, message)]."""
    bad = []

    def chk(cid, where, ok, msg):
        if not ok:
            bad.append((cid, where, msg))

    els = _elements(root)
    # C01-C03 uniqueness
    for cid, key, what in (('C01', 'symbol', 'element symbol'), ('C02', 'id', 'element id')):
        seen = set()
        for e in els:
            v = getattr(e, key)
            chk(cid, e.symbol, v not in seen, f'duplicate {what} "{v}"')
            seen.add(v)
    seen = set()
    for e in els:
        for ln in e.lines:
            chk('C03', ln.symbol, ln.id not in seen, f'duplicate line id "{ln.id}"')
            seen.add(ln.id)
    for e in els:
        w = e.symbol
        chk('C04', w, e.voltageLevel is not None and e.voltageLevel > 0, 'voltageLevel must be > 0')
        chk('C05', w, e.status is not None, 'status is mandatory')
        n = e.eClass.name
        if hasattr(e, 'powerFactor') and e.powerFactor is not None:
            chk('C06', w, 0 < e.powerFactor <= 1, f'powerFactor {e.powerFactor} outside (0,1]')
        if hasattr(e, 'efficiency') and e.efficiency is not None:
            chk('C07', w, 0 <= e.efficiency <= 1, f'efficiency {e.efficiency} outside [0,1]')
        if n == 'BatteryStorage':
            chk('C08', w, 0 <= e.stateOfCharge <= 1, f'stateOfCharge {e.stateOfCharge} outside [0,1]')
            chk('C09', w, e.capacityMWh > 0, 'capacityMWh must be > 0')
        if hasattr(e, 'maxCapacityMW') and n not in ('PowerLine',):
            chk('C10', w, e.maxCapacityMW >= 0, 'maxCapacityMW must be >= 0')
        if e.spatialData is not None:
            sd = e.spatialData
            chk('C11', w, -90 <= sd.latitude <= 90 and -180 <= sd.longitude <= 180, 'coordinates out of range')
        for ln in e.lines:
            chk('C12', ln.symbol, ln.source is not None and ln.target is not None, 'line needs source and target')
            chk('C13', ln.symbol, ln.source is not ln.target, 'line source and target must differ')
            chk('C14', ln.symbol, ln.lengthKM > 0 and ln.maxCapacityMW > 0, 'lengthKM and maxCapacityMW must be > 0')
        if getattr(e, 'dailyProfile', None):
            try:
                prof = [float(x) for x in e.dailyProfile.split(',')]
            except ValueError:
                prof = None
            chk('C18', w, prof is not None and len(prof) == 24 and all(x > 0 for x in prof),
                'dailyProfile must be 24 comma-separated numbers, all > 0')
        if n == 'SubStation':
            chk('C15', w, all(t is not e for t in e.connectedTo), 'substation connected to itself')

    def dist_ok(entry, where):
        d = entry.distribution
        pos = {'WeibullDist': ('shape', 'scale'), 'NormalDist': ('sigma',), 'BetaDist': ('alpha', 'beta'),
               'LogNormalDist': ('sigma',), 'GammaDist': ('shape', 'scale'), 'PoissonDist': ('lam',)}[d.eClass.name]
        for p in pos:
            chk('C16', where, getattr(d, p) > 0, f'{entry.variable}: {d.eClass.name}.{p} must be > 0')

    for e in els:
        for ent in e.distributions:
            dist_ok(ent, e.symbol)
    for b in list(root.globalDistributions) + list(root.classDistributions):
        for ent in b.distributions:
            dist_ok(ent, b.symbol)
    # C17: one entry per variable inside a block
    for b in list(root.globalDistributions) + list(root.classDistributions) + [e for e in els]:
        vs = [x.variable.name for x in b.distributions]
        chk('C17', b.symbol, len(vs) == len(set(vs)), 'a variable is defined twice in the same block')
    return bad


# ----------------------------------------------------------------------------- canonical property dicts (round trip)
def _canon(v):
    if isinstance(v, str) and v in ('true', 'false'):
        return v == 'true'
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return float(v) if False else v
    return v


def _dist_props(entry, getter):
    d = entry.distribution
    cname = d.__class__.__name__ if getter == 'tx' else d.eClass.name
    return (str(_val(entry.variable)), cname,
            tuple(sorted((k, float(getattr(d, k))) for k in
                         {'WeibullDist': ('shape', 'scale'), 'NormalDist': ('mu', 'sigma'), 'BetaDist': ('alpha', 'beta'),
                          'LogNormalDist': ('mu', 'sigma'), 'GammaDist': ('shape', 'scale'),
                          'PoissonDist': ('lam',)}[cname])))


def seg_to_props(model, pkg, optional):
    """Every property of the textX model, keyed (symbol, feature) -- features named as in the Ecore."""
    props = {}

    def put(owner_key, ename, obj):
        eclass = pkg.getEClassifier(ename)
        for f in all_features(eclass):
            if not isinstance(f, EAttribute) or f.many:
                continue
            v = getattr(obj, tx_attr(ename, f.name), None)
            if v is None or (f.name in optional and v in ('', 0.0)):
                continue
            tn = f.eType.name if hasattr(f.eType, 'name') else ''
            props[(owner_key, f.name)] = (_canon(v) if tn == 'EBoolean' else v)

    for g in model.grids:
        put(('grid', g.name), 'PowerGrid', g)
    for lst, cname in ROOT_LISTS:
        for n in getattr(model, lst, []):
            put(('element', n.name), cname, n)
            props[('element', n.name, '@class')] = cname
            if getattr(n, 'spatial', None) is not None:
                put(('element', n.name, 'spatial'), 'SpatialData', n.spatial)
            for ln in getattr(n, 'lines', []):
                put(('line', ln.name), 'PowerLine', ln)
                props[('line', ln.name, 'owner')] = n.name
                props[('line', ln.name, 'source')] = ln.source.name
                props[('line', ln.name, 'target')] = ln.target.name
            for ent in getattr(n, 'distributions', []):
                props[('element', n.name, 'dist', ent.variable)] = _dist_props(ent, 'tx')
            if getattr(n, 'connectedTo', None):
                props[('element', n.name, 'connectedTo')] = tuple(t.name for t in n.connectedTo)
    for kind, lst in (('global', model.globalDistributions), ('class', model.classDistributions)):
        for b in lst:
            props[(kind, b.name, '@present')] = True
            if kind == 'class':
                props[(kind, b.name, 'appliesTo')] = b.appliesTo
            for ent in b.distributions:
                props[(kind, b.name, 'dist', ent.variable)] = _dist_props(ent, 'tx')
    return props


def xmi_to_props(root):
    props = {}

    def put(owner_key, obj):
        for f in all_features(obj.eClass):
            if not isinstance(f, EAttribute) or f.many:
                continue
            v = obj.eGet(f)
            if v is None or (v in ('', 0.0) and not obj.eIsSet(f)):
                continue
            if not obj.eIsSet(f):
                continue
            props[(owner_key, f.name)] = _val(v) if not isinstance(v, EEnumLiteral) else v.name

    for g in root.grids:
        put(('grid', g.symbol), g)
    els = _elements(root)
    for e in els:
        put(('element', e.symbol), e)
        props[('element', e.symbol, '@class')] = e.eClass.name
        if e.spatialData is not None:
            put(('element', e.symbol, 'spatial'), e.spatialData)
        for ln in e.lines:
            put(('line', ln.symbol), ln)
            props[('line', ln.symbol, 'owner')] = e.symbol
            props[('line', ln.symbol, 'source')] = ln.source.symbol
            props[('line', ln.symbol, 'target')] = ln.target.symbol
        for ent in e.distributions:
            props[('element', e.symbol, 'dist', ent.variable.name)] = _dist_props(ent, 'xmi')
        if e.eClass.name == 'SubStation' and len(e.connectedTo):
            props[('element', e.symbol, 'connectedTo')] = tuple(t.symbol for t in e.connectedTo)
    for kind, lst in (('global', root.globalDistributions), ('class', root.classDistributions)):
        for b in lst:
            props[(kind, b.symbol, '@present')] = True
            if kind == 'class':
                props[(kind, b.symbol, 'appliesTo')] = b.appliesTo.name
            for ent in b.distributions:
                props[(kind, b.symbol, 'dist', ent.variable.name)] = _dist_props(ent, 'xmi')
    return props


def roundtrip(seg_path, grammar_path=DEFAULT_GRAMMAR, templates_dir=DEFAULT_TEMPLATES, ecore_path=DEFAULT_ECORE,
              xmi_path=None):
    """.seg -> XMI -> reload; compare every property. Returns dict with counts and differences."""
    import tempfile
    _, pkg = load_metamodel(ecore_path)
    opt = optional_attrs(grammar_path)
    model = load_textx_metamodel(grammar_path).model_from_file(seg_path)
    a = seg_to_props(model, pkg, opt)
    if xmi_path is None:
        xmi_path = os.path.join(tempfile.mkdtemp(), 'rt.xmi')
    _, viol = seg_to_xmi(seg_path, xmi_path, grammar_path, templates_dir, ecore_path)
    b = xmi_to_props(load_xmi(xmi_path, ecore_path))
    only_seg = sorted(map(str, set(a) - set(b)))
    only_xmi = sorted(map(str, set(b) - set(a)))
    diff = [(str(k), a[k], b[k]) for k in a if k in b and a[k] != b[k] and not _same(a[k], b[k])]
    return {'properties': len(a), 'only_in_seg': only_seg, 'only_in_xmi': only_xmi, 'different': diff,
            'violations': viol}


def _same(x, y):
    if isinstance(x, float) or isinstance(y, float):
        try:
            return abs(float(x) - float(y)) <= 1e-12 * max(1.0, abs(float(x)))
        except (TypeError, ValueError):
            return False
    return x == y


# ----------------------------------------------------------------------------- XMI -> object graph for generate_twin.py
class _Obj:
    """Plain attribute bag; __class__.__name__ is the Ecore class name (the generator relies on that)."""


_CLASS_CACHE = {}


def _make(cname):
    if cname not in _CLASS_CACHE:
        _CLASS_CACHE[cname] = type(cname, (_Obj,), {})
    return _CLASS_CACHE[cname]()


def xmi_to_view(root):
    """Adapter: exposes the XMI with the attribute names the generator already uses
    (name=symbol, elementId, elementName, lineId, spatial, root lists)."""
    memo = {}

    def conv(obj):
        if id(obj) in memo:
            return memo[id(obj)]
        cname = obj.eClass.name
        v = _make(cname)
        memo[id(obj)] = v
        for f in all_features(obj.eClass):
            if not obj.eIsSet(f):
                continue
            raw = obj.eGet(f)
            tn = tx_attr(cname, f.name)
            if isinstance(f, EReference):
                if f.many:
                    setattr(v, tn, [conv(x) for x in raw])
                else:
                    setattr(v, tn, conv(raw))
            else:
                setattr(v, tn, raw.name if isinstance(raw, EEnumLiteral) else raw)
        return v

    top = _make('SmartEnergyGridModel')
    for lst, _ in ROOT_LISTS:
        setattr(top, lst, [])
    top.grids = [conv(g) for g in root.grids]
    for e in root.elements:
        getattr(top, LIST_OF_CLASS[e.eClass.name]).append(conv(e))
    top.globalDistributions = [conv(b) for b in root.globalDistributions]
    top.classDistributions = [conv(b) for b in root.classDistributions]
    return top


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        sys.exit('usage: python seg2xmi.py model.seg [more.seg ...]   (round-trip + constraint check)')
    failed = False
    for path in sys.argv[1:]:
        r = roundtrip(path)
        ok = not (r['only_in_seg'] or r['only_in_xmi'] or r['different'] or r['violations'])
        failed |= not ok
        print(f"{'OK  ' if ok else 'FAIL'} {path}: {r['properties']} properties compared, "
              f"{len(r['different'])} different, {len(r['only_in_seg'])}+{len(r['only_in_xmi'])} missing, "
              f"{len(r['violations'])} constraint violations")
        for v in r['violations']:
            print('   ', v)
    sys.exit(1 if failed else 0)
