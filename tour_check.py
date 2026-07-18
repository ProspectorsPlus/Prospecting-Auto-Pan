#!/usr/bin/env python3
"""Verification protocol for the Prospectors Plus tutorial work.
Run from the repo root. Checks (per tutorial_handoff/02_TOUR_SYSTEM.md §7):
  1. py_compile all 8 active python files
  2. node --check every <script> from all six HTML surfaces (both copies)
  3. exactly 144 unique data-key settings
  4. every tour step's sel/tab/open resolves in the rendered HTML
  5. lockstep: the edited shared blocks are byte-identical mac <-> windows
  6. finds_sim.py -> ALL SCENARIOS PASS   (run separately)
"""
import os, re, sys, subprocess, tempfile, importlib.util

ROOT = os.getcwd()
FAILS = []

def fail(msg):
    FAILS.append(msg)
    print('  FAIL:', msg)

def ok(msg):
    print('  ok:', msg)

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m

# ---- 1. compile ------------------------------------------------------------
print('[1] py_compile')
import py_compile
for f in ['prospecting_app.py', 'prospecting_old.py',
          'prospector_engine/engine.py', 'prospector_engine/platform_mac.py',
          'prospector_engine/platform_win.py', 'prospector_engine/sensing.py',
          'prospecting_ui.py',
          'prospecting_assistant.py',
          'windows/prospecting_app.py', 'windows/prospecting_old.py',
          'windows/prospecting_ui.py', 'windows/prospecting_assistant.py']:
    try:
        py_compile.compile(f, doraise=True)
        ok(f)
    except Exception as e:
        fail(f'{f}: {e}')

# ---- render + JS + keys + targets per copy --------------------------------
def check_copy(tag, path, uipath):
    print(f'[2-4] {tag}: render, node --check, keys, tour targets')
    # isolated import (windows copy shadows prospecting_ui)
    for mod in list(sys.modules):
        if mod.startswith('prospecting'):
            del sys.modules[mod]
    sys.path.insert(0, os.path.dirname(os.path.abspath(uipath)) or '.')
    app = load(path, 'prospecting_app_' + tag)
    sys.path.pop(0)
    html = app.build_html()
    surfaces = {'main': html}
    try: surfaces['hud'] = app._hud_html()
    except Exception as e: fail(f'{tag} _hud_html: {e}')
    for nm in ['ANALYTICS_HTML', 'COACH_HTML', 'PILL_HTML', '_OVERLAY_HTML']:
        v = getattr(app, nm, None)
        if v: surfaces[nm] = v
    try: surfaces['studio'] = app._studio_html()
    except Exception as e: fail(f'{tag} _studio_html: {e}')
    # 2. node --check each script
    bad = 0
    for nm, doc in surfaces.items():
        for i, m in enumerate(re.finditer(r'<script[^>]*>(.*?)</script>', doc, re.S)):
            js = m.group(1)
            if not js.strip(): continue
            with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as t:
                t.write(js); tf = t.name
            r = subprocess.run(['node', '--check', tf], capture_output=True, text=True)
            os.unlink(tf)
            if r.returncode != 0:
                bad += 1
                fail(f'{tag} {nm}#{i}: {r.stderr.strip()[:400]}')
    if not bad: ok('all scripts pass node --check')
    # 3. data-key count — on markup only (scripts stripped: tour selectors
    #    and JS templates legitimately mention data-key inside strings)
    markup = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.S)
    keys = re.findall(r'data-key="([^"]+)"', markup)
    uniq = set(keys)
    if len(uniq) == 144: ok('144 unique data-keys')
    else: fail(f'{tag}: {len(uniq)} unique data-keys (want 144); dupes/missing!')
    if len(keys) != len(uniq):
        from collections import Counter
        fail(f'{tag} duplicated keys: ' + str([k for k, c in Counter(keys).items() if c > 1]))
    # 4. tour targets — tours now live in Python TOUR_DEFAULTS; walk the merged
    #    content and assert every step's tab/sel/open resolves in the HTML.
    src = open(path, encoding='utf-8').read()
    content = app._tutorial_merged()
    tours = content['tours']
    html_studio = surfaces.get('studio', '')
    tabs, sels, opens, st_sels = set(), set(), set(), set()
    for tname, steps in tours.items():
        for st in steps:
            if tname == 'studio_editor':
                if st.get('sel'): st_sels.add(st['sel'])
                continue
            if st.get('tab'): tabs.add(st['tab'])
            if st.get('sel'): sels.add(st['sel'])
            if st.get('open'): opens.add(st['open'])
    miss = 0
    for t in tabs:
        if f'data-tab="{t}"' not in html:
            miss += 1; fail(f'{tag} tour tab does not resolve: {t}')
    for s in st_sels:
        if not (s.startswith('#') and f'id="{s[1:]}"' in html_studio):
            miss += 1; fail(f'{tag} studio tour sel does not resolve: {s}')
    for s in sels | opens:
        if s.startswith('#'):
            hit = f'id="{s[1:]}"' in html
        elif s.startswith('[data-key='):
            hit = ('data-key="%s"' % re.findall(r'data-key="?([A-Z0-9_]+)', s)[0]) in html
        elif '[data-pkey=' in s:
            hit = ('data-pkey="%s"' % re.findall(r'data-pkey="([A-Z0-9_]+)"', s)[0]) in html
        elif '[data-regionkey=' in s:
            hit = ('data-regionkey="%s"' % re.findall(r'data-regionkey="([A-Z]+)"', s)[0]) in html
        elif s.startswith('.'):
            tok = s[1:].split('[')[0].split('.')[0]
            hit = re.search(r'class="[^"]*\b' + re.escape(tok) + r'\b', html) is not None
        else:
            hit = False
        if not hit:
            miss += 1; fail(f'{tag} tour sel does not resolve: {s}')
    if not miss: ok(f'all {len(tabs)} tabs + {len(sels|opens)} sels + {len(st_sels)} studio sels resolve')
    steps = {n: len(v) for n, v in tours.items()}
    print('  tours:', steps, 'total steps:', sum(steps.values()))
    # deep help coverage: every setting + every UI_HELP key present
    helpmap = content['help']
    allkeys = [k for _, items in _ui_sections(app) for k, *_ in items]
    nohelp = [k for k in allkeys if ('help:' + k) not in helpmap]
    if nohelp: fail(f'{tag} settings missing deep help: {nohelp[:8]}')
    else: ok(f'{len(allkeys)} settings + {len([k for k in helpmap if not k.startswith("help:")])} UI keys have help')
    # em-dash / &amp; sweep across everything a user sees
    dirty = []
    for steps2 in tours.values():
        for st in steps2:
            if '—' in (st.get('title','')+st.get('body','')) or '&amp;' in (st.get('title','')+st.get('body','')):
                dirty.append(st['id'])
    for k, v in helpmap.items():
        if '—' in v.get('body','') or '&amp;' in v.get('body',''):
            dirty.append(k)
    if dirty: fail(f'{tag} em-dash/&amp; in user text: {dirty[:8]}')
    else: ok('no em dashes or &amp; in tour/help text')
    return src


def _ui_sections(app):
    return app.SECTIONS

src_mac = check_copy('mac', 'prospecting_app.py', 'prospecting_ui.py')
src_win = check_copy('win', 'windows/prospecting_app.py', 'windows/prospecting_ui.py')

# ---- 5. lockstep on the edited shared blocks -------------------------------
print('[5] lockstep (edited blocks byte-identical)')
def slice_between(src, a, b, name):
    try:
        i = src.index(a); j = src.index(b, i)
        return src[i:j]
    except ValueError:
        fail(f'anchor missing for {name}'); return None

REGIONS = [
    ('howbox',   "'<div class=\"calbanner\" id=\"calbanner\"></div>'", "'<div class=\"runbtns\">"),
    ('studio-model', '# PROSPECTOR STUDIO -- script model', '\ndef _coerce'),
    ('studio-api',   '# ---- PROSPECTOR STUDIO: script library Api', '\n    # ---- calibrate'),
    ('studio-html',  'def _studio_eval(js):', '\ndef _quit_everything'),
    ('studio-panel', 'nav("studio"', "'<section class=\"panel\" id=\"pkeys\">"),
    ('tour-css', ' .tour{position:fixed', ' .introbox{'),
    ('scaffold', '<div id="tour" class="tour"', '<script>'),
    ('tour-js',  ' (function(){\n   function T(id)', "\n window.addLog"),
    ('tutorial-defaults', 'TOUR_DEFAULTS = {', '\ndef _tutorial_local'),
]
for name, a, b in REGIONS:
    sm = slice_between(src_mac, a, b, name)
    sw = slice_between(src_win, a, b, name)
    if sm is None or sw is None: continue
    if sm == sw: ok(f'{name}: identical ({len(sm)} bytes)')
    else:
        fail(f'{name}: DIFFERS (mac {len(sm)} vs win {len(sw)} bytes)')
        import difflib
        for d in list(difflib.unified_diff(sm.splitlines(), sw.splitlines(), lineterm=''))[:12]:
            print('   ', d[:160])

EXTRA = [
    ('prospecting_ui.py', 'windows/prospecting_ui.py',
     [('studio-schema', '# PROSPECTOR STUDIO -- the block schema', '\ndef render(')]),
]
for mpath, wpath, regs in EXTRA:
    sm_src = open(mpath, encoding='utf-8').read()
    sw_src = open(wpath, encoding='utf-8').read()
    for name, a, b in regs:
        sm = slice_between(sm_src, a, b, name)
        sw = slice_between(sw_src, a, b, name)
        if sm is None or sw is None: continue
        if sm == sw: ok(f'{name}: identical ({len(sm)} bytes)')
        else: fail(f'{name}: DIFFERS (mac {len(sm)} vs win {len(sw)} bytes)')

# [Phase 04 C7] the engine is single-source: the studio-engine block lives only
# in prospector_engine/engine.py. The old mac<->win engine-mirror lockstep is
# replaced by: launcher shims byte-identical + no second engine body under
# windows/ (platform I/O lives in prospector_engine/platform_mac|win.py).
print('[5b] engine single-source (shims identical, no windows engine copy)')
_sm = open('prospecting_old.py', encoding='utf-8').read()
_sw = open('windows/prospecting_old.py', encoding='utf-8').read()
if _sm == _sw: ok(f'launcher shims identical ({len(_sm)} bytes)')
else: fail('launcher shims differ between mac and windows')
if 'def treasure_tick' in _sw or 'CUSTOM SCRIPTS (Prospector Studio)' in _sw:
    fail('windows/prospecting_old.py contains an engine body (second copy)')
elif 'def treasure_tick' not in open('prospector_engine/engine.py', encoding='utf-8').read():
    fail('prospector_engine/engine.py lost the studio-engine block')
else: ok('single engine source (block present once, in the package)')

print()
print('RESULT:', 'ALL CHECKS PASS' if not FAILS else f'{len(FAILS)} FAILURES')
sys.exit(1 if FAILS else 0)
