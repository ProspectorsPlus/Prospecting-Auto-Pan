"""Engine-owned settings: schema registry, v0->v1 migration, single
atomic writer (Phase 04, checkpoint C5; architecture sections 6-7).

The schema is derived from the engine module itself: every key the legacy
`load_config` honors is an UPPERCASE module global, and the canonical
default IS the baked global (section 6.1) -- so the registry can never
drift from the engine's acted-on truth. Platform key constants and
derived values are excluded (they are code, not configuration).

Migration (v0 -> v1) is normalizing, additive, and lossless: typed keys
coerce with exactly the app's legacy `_coerce` garbage-in semantics,
unknown keys are preserved verbatim, and every engine-schema key is
materialized so a v1 file is self-describing and legacy-loadable with
identical behavior. EASY_* layering stays a bind-time transform inside
`load_config`; stored values never contain offsets, and because v1
materializes the 8 layered targets, re-binding per run start cannot creep.
"""
import json
import os

SCHEMA_VERSION = 1

# Not configuration: platform keycodes, derived values, module plumbing.
EXCLUDE = {
    "CONFIG_FILE", "CONFIG_SCHEMA", "CAP_START_PIXEL", "SYNC_URL",
    "KEY_W", "KEY_A", "KEY_S", "KEY_D", "KEY_SHIFT", "KEY_SPACE",
    "SLOT_KEYCODES", "TOGGLE_VK", "TOGGLE_NAME", "SOFTSTOP_VK",
    "EMIT", "RELICS_HELP", "STUDIO_HTML",
}

_SCALARS = {bool: "bool", int: "int", float: "float", str: "str"}


class SchemaTooNew(Exception):
    def __init__(self, found, supported=SCHEMA_VERSION):
        super().__init__("config schema %r is newer than supported %d"
                         % (found, supported))
        self.found = found
        self.supported = supported


def _eligible(name, value):
    if not name.isupper() or name.startswith("_"):
        return False
    if name in EXCLUDE:
        return False
    return isinstance(value, (bool, int, float, str, list, tuple, dict))


def schema(po):
    """{key: {"type", "default", "applies"}} over the engine module.
    Defaults are the BAKED module globals -- call before load_config
    mutates them (the server snapshots at startup)."""
    out = {}
    for name in dir(po):
        try:
            value = getattr(po, name)
        except Exception:
            continue
        if not _eligible(name, value):
            continue
        t = _SCALARS.get(type(value))
        if t is None:
            t = "list" if isinstance(value, (list, tuple)) else "dict"
        default = list(value) if isinstance(value, tuple) else value
        out[name] = {"type": t, "default": default, "applies": "run-start"}
    return out


def coerce(t, v):
    """The legacy app `_coerce` semantics exactly: same garbage-in
    behavior, no new clamping."""
    if t == "bool":
        return bool(v)
    if t == "str":
        return str(v)
    if t == "float":
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    if t == "int":
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0
    return v          # list/dict: applied raw, as legacy load_config does


def validate(schema_map, values, opaque=False):
    """-> per-key error map (empty = valid). Coercible values are legal
    (they coerce); only unknown keys (engine path) or engine keys (opaque
    path) fail in v1.0 -- matching legacy no-clamping."""
    per_key = {}
    for k in values:
        if opaque:
            if k in schema_map:
                per_key[k] = "ENGINE_KEY"
        elif k not in schema_map:
            per_key[k] = "UNKNOWN_KEY"
    return per_key


def atomic_write(path, doc):
    """The engine's single write path: tmp + rename, rolling .bak of the
    previous content (the prospecting_scripts.json discipline)."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    if os.path.exists(path):
        try:
            os.replace(path, path + ".bak")
        except OSError:
            pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_doc(path):
    """-> (doc, corrupt). Missing file -> ({}, False). Corrupt JSON ->
    ({}, True) with the original preserved as .corrupt.bak (no silent
    reset)."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return {}, False
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            raise ValueError("config root is not an object")
        return doc, False
    except ValueError:
        try:
            with open(path + ".corrupt.bak", "w", encoding="utf-8") as f:
                f.write(raw)
        except OSError:
            pass
        return {}, True


def migrate_doc(doc, schema_map):
    """Pure v0->v1 migration (input dict -> output dict). Raises
    SchemaTooNew on a config from the future (never silently strips)."""
    ver = doc.get("CONFIG_SCHEMA", 0)
    if isinstance(ver, (int, float)) and ver > SCHEMA_VERSION:
        raise SchemaTooNew(ver)
    out = dict(doc)                       # unknown keys preserved verbatim
    for key, spec in schema_map.items():
        if key in out:
            if spec["type"] in ("bool", "int", "float", "str"):
                out[key] = coerce(spec["type"], out[key])
        else:
            # materialize with the engine's acted-on default -- a missing
            # key never overwrites a baked default with zero
            out[key] = spec["default"]
    out["CONFIG_SCHEMA"] = SCHEMA_VERSION
    return out


def migrate_file(path, schema_map):
    """Engine-startup / settings.import migration. -> (doc, changed:list).
    Writes only when the document changed; keeps .bak of the original."""
    doc, corrupt = read_doc(path)
    new = migrate_doc(doc, schema_map)
    changed = sorted(k for k in new
                     if k not in doc or doc.get(k) != new.get(k))
    if corrupt or changed:
        atomic_write(path, new)
    return new, changed
