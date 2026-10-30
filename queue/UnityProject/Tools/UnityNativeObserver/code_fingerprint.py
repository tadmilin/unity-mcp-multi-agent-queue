"""Deterministic code-object evidence, independent of marshal reference flags."""
import hashlib
import json
import types

VERSION = 'code-structure-sha256-v1'


def _constant(value):
    if isinstance(value, types.CodeType):
        return ['code', _code(value)]
    if value is None or value is Ellipsis:
        return ['singleton', repr(value)]
    if isinstance(value, bool):
        return ['bool', value]
    if isinstance(value, int):
        return ['int', str(value)]
    if isinstance(value, float):
        return ['float', value.hex()]
    if isinstance(value, complex):
        return ['complex', value.real.hex(), value.imag.hex()]
    if isinstance(value, str):
        return ['str', value]
    if isinstance(value, bytes):
        return ['bytes', value.hex()]
    if isinstance(value, tuple):
        return ['tuple', [_constant(x) for x in value]]
    if isinstance(value, frozenset):
        items = [_constant(x) for x in value]
        return ['frozenset', sorted(items, key=lambda x: json.dumps(x, sort_keys=True))]
    raise TypeError('Unsupported code constant: ' + type(value).__name__)


def _code(code):
    fields = ('co_argcount', 'co_posonlyargcount', 'co_kwonlyargcount', 'co_nlocals',
              'co_stacksize', 'co_flags', 'co_code', 'co_consts', 'co_names',
              'co_varnames', 'co_freevars', 'co_cellvars', 'co_filename',
              'co_name', 'co_qualname', 'co_firstlineno', 'co_linetable', 'co_exceptiontable')
    return {field: _constant(getattr(code, field)) for field in fields}


def fingerprint(code):
    if not isinstance(code, types.CodeType):
        raise TypeError('Expected code object')
    payload = json.dumps([VERSION, _code(code)], sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(payload.encode('ascii')).hexdigest()
