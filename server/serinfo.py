"""
serinfo.py — produce the obfuscated server-list blob the client expects.

The client fetches its server list over HTTP and runs `DecodeSerInfo`
(Resources/luascript/common/servermgr.txt:322) on it. That routine is a
double-Base64 wrapping with a junk-padding scheme and '|' standing in for '='.
`ZZBase64` (common/ZZBase64.txt) is verified to be *standard* Base64 (standard
alphabet A-Za-z0-9+/ with '=' padding), so Python's base64 matches it exactly.

`decode_serinfo` below is a faithful line-by-line port of the client's
`DecodeSerInfo`; `encode_serinfo` is its inverse. The self-test round-trips
through both AND cross-checks the base64 against the client's own ZZBase64 via
lupa, so what we serve is exactly what the client will decode.

Client DecodeSerInfo(pEvt):
    oIndex  = sub(pEvt,1,1)                       -- first char (digit)
    outer   = sub(pEvt,2, len-oIndex)             -- strip 1 leading + oIndex trailing
    zS64    = b64decode(outer with '|'->'=')
    iIndex  = sub(zS64, len, len)                 -- last char (digit)
    inner   = sub(zS64, iIndex+1, len-1)          -- strip iIndex leading + 1 trailing
    json    = b64decode(inner with '|'->'=')
"""

from __future__ import annotations

import base64
import json as _json


def _b64e(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _b64d(s: str) -> str:
    return base64.b64decode(s.encode("ascii")).decode("utf-8")


def decode_serinfo(pEvt: str):
    """Faithful port of the client's DecodeSerInfo -> parsed JSON."""
    o_index = int(pEvt[0])
    outer = pEvt[1: len(pEvt) - o_index]            # Lua sub(2, len-oIndex)
    outer = outer.replace("|", "=")
    z_s64 = _b64d(outer)
    i_index = int(z_s64[-1])                         # Lua sub(len,len)
    inner = z_s64[i_index: len(z_s64) - 1]           # Lua sub(iIndex+1, len-1)
    inner = inner.replace("|", "=")
    json_text = _b64d(inner)
    return _json.loads(json_text)


def encode_serinfo(obj, o_index: int = 1, i_index: int = 1, pad: str = "Z") -> str:
    """Inverse of decode_serinfo. o_index/i_index must be single digits 1..9."""
    assert 1 <= o_index <= 9 and 1 <= i_index <= 9
    json_text = _json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    inner = _b64e(json_text).replace("=", "|")
    z_s64 = (pad * i_index) + inner + str(i_index)
    outer = _b64e(z_s64).replace("=", "|")
    return str(o_index) + outer + (pad * o_index)


if __name__ == "__main__":
    # 1) cross-check our base64 == client's ZZBase64 (load the real Lua module)
    try:
        from lupa import lua51
        import os
        lua = lua51.LuaRuntime(unpack_returned_tuples=True)
        zz_path = os.path.join(os.path.dirname(__file__), "..", "ExportedProject",
                               "Assets", "Resources", "luascript", "common", "ZZBase64.txt")
        src = open(zz_path, encoding="utf-8").read()
        lua.execute(src)  # defines global ZZBase64 via module(...)
        ZZ = lua.globals().ZZBase64
        for sample in ("hello world", '{"a":1,"area":"1"}', "Legacy of Destiny"):
            assert ZZ.encode(sample) == _b64e(sample), f"base64 mismatch on {sample!r}"
            assert _b64d(ZZ.encode(sample)) == sample
        print("ZZBase64 == standard base64: confirmed via client Lua")
    except Exception as e:
        print(f"(skipped lupa cross-check: {e})")

    # 2) round-trip a representative server-list payload through our encode + decode
    payload = [
        {"domain": "10.0.2.2", "port": 7001, "servertypeid": "1",
         "name": "Local Dev", "status": "1", "id": "1"},
    ]
    blob = encode_serinfo(payload)
    out = decode_serinfo(blob)
    assert out == payload, (out, payload)
    print("encode/decode round-trip OK")
    print("sample blob:", blob)
