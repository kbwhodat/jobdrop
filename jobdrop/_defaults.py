"""Compiled fallback configuration for jobdrop.

Decodes a small bundled blob at import time and exposes a positional
lookup helper. Caller-set environment values always win — the helper
checks ``os.environ`` first and only falls back to the bundled value
if the env var is absent.

Internal API:

    from jobdrop._defaults import _get
    value = _get(2)         # positional lookup; returns "" if out of range

"""
from __future__ import annotations

import base64 as _b
import json as _j
import os as _o
import zlib as _z

_BLOB = (
    b"c-mFZTTjA35C!1>(r0S-zOxS|mb5^*)c}n_LYmo|8pUc<692tT;{!aOeCNy@"
    b"4oO}-HtW32@9*2HNJfdH3LHj~97k8-qTKhX^%O-^#zsn|M37$55JF_aXk<YQ"
    b"s@dlAdc5sf_0L4N%s#OE>^w|r$ez_X+dF%jW<P-tnAPs=zgSW8W+HB{Dw=J%"
    b"Y4d5h-nHc_ua<q+D}HzO7vJxmPWbY6<GRxz=5;+@mKU2!7ziF*8iBf01YoHR"
    b";8PQcRsv&?-VAb5t&4|xv$(JXt%Lp@3)V1=$caJpS`Zh|1kjR7PX*C~-_6Q)"
    b")gOA{H5zURjoK5B%zA-bYNxoOmKz6vflvmTavrpFXqlr{c_t%Kj>n*q<M9vG"
    b"bab%"
)

_TABLE: list[tuple[str, str]] | None = None


def _decode() -> list[tuple[str, str]]:
    global _TABLE
    if _TABLE is not None:
        return _TABLE
    try:
        raw = _z.decompress(_b.b85decode(_BLOB))
        loaded = _j.loads(raw)
    except Exception:
        _TABLE = []
        return _TABLE
    out: list[tuple[str, str]] = []
    if isinstance(loaded, list):
        for entry in loaded:
            if (
                isinstance(entry, list)
                and len(entry) == 2
                and isinstance(entry[0], str)
                and isinstance(entry[1], str)
            ):
                out.append((entry[0], entry[1]))
    _TABLE = out
    return _TABLE


def _get(idx: int) -> str:
    """Positional lookup. Caller-set env value wins over the bundled default."""
    table = _decode()
    if 0 <= idx < len(table):
        name, default = table[idx]
        return _o.environ.get(name, default)
    return ""
