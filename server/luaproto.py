"""
luaproto.py — encode/decode game messages by running the CLIENT's own Lua.

Rather than hand-port 566 message layouts, we embed a Lua 5.1 runtime (to match
the client's SLua) and load the unmodified protocol files from
ExportedProject/.../luascript. Each file exposes create_request()/
create_response() factories whose serial()/unserial() functions ARE the wire
format. We hand them a Python CByteBuffer (codec.py), so encoding can never
drift from the client.

Public API:
    proto = LuaProto(LUASCRIPT_DIR)
    fields = proto.decode(opcode, payload_bytes, kind="request")
    payload = proto.encode(opcode, {field: value, ...}, kind="response")

`opcode` is the uint32 servantname (e.g. 0x00010001). We resolve it to a module
path via netdefine.txt.
"""

from __future__ import annotations

import os
import re

import lupa
from lupa import lua51  # pin Lua 5.1 to match SLua

from codec import CByteBuffer

LUASCRIPT_DIR = os.environ.get(
    "LOD_LUASCRIPT_DIR",
    os.path.join(
        os.path.dirname(__file__), "..",
        "ExportedProject", "Assets", "Resources", "luascript",
    ),
)


def _parse_netdefine(luascript_dir: str) -> dict[int, str]:
    """opcode (int) -> module path under public.netimpl. (e.g. 'globalmgr.gbm_login_game')."""
    path = os.path.join(luascript_dir, "public", "netimpl", "netdefine.txt")
    text = open(path, encoding="utf-8").read()
    # NAME = { 0xAAAABBBB, "servant.path" },
    pat = re.compile(r"\{\s*(0x[0-9A-Fa-f]{8})\s*,\s*\"([^\"]*)\"\s*\}")
    out: dict[int, str] = {}
    for m in pat.finditer(text):
        op = int(m.group(1), 16)
        mod = m.group(2)
        if mod:  # skip prefix-only entries with ""
            out[op] = mod
    return out


class LuaProto:
    def __init__(self, luascript_dir: str = LUASCRIPT_DIR):
        self.dir = os.path.abspath(luascript_dir)
        self.opcode_to_module = _parse_netdefine(self.dir)
        self.lua = lua51.LuaRuntime(unpack_returned_tuples=True)
        self._install_loader()
        # the client's network.txt only forwards to buffer methods -> load as-is
        self.lua.require("network")
        # aoievent.txt references a global AOI_EVENT enum (from business.txt) — inject it
        self.lua.execute(
            "AOI_EVENT = {ADD_PLAYER=1,ADD_MONSTER=2,ADD_NPC=3,ADD_TELEPORT=4,"
            "ADD_DROPBAG=5,ADD_GATHER=6,DEL=7,MOVE=8,ATTACK=9,DAMAGE=10,"
            "ATTR_CHANGE=11,POSITION_CHANGE=12,DEL_DROPBAG=13,BOSS_RANKING=14,"
            "BUFF=15,ADD_PET=16,BUBBLING=17,NAME_CHANGE=18,ADD_ESCORT=29,"
            "ACTION=30,SHADOW=31}"
        )
        self._proto_cache: dict[str, object] = {}

    def encode_aoi(self, events: list[dict]) -> bytes:
        """Build a CL_AOI (aoievent) message body from a list of events. Each event
        is a dict with 'type' (AOI_EVENT value) + the sub-struct's fields
        (see public/netimpl/cellapp/ce_aoievent.txt). Uses the client's own Lua."""
        proto = self.lua.require("public.netimpl.aoievent")
        resp = proto["create_response"]()
        create_event = proto["create_EVENT"]
        aoilist = resp["aoilist"]
        for i, ev in enumerate(events, start=1):
            e = create_event(ev["type"])           # builds the right sub-struct, sets .type
            for k, v in ev.items():
                if k == "type":
                    continue
                e[k] = _py_to_lua(self.lua, v)
            aoilist[i] = e
        buf = CByteBuffer()
        resp["serial"](buf)
        return buf.ToBytes()

    def _install_loader(self):
        """Make `require "a.b.c"` read luascript/a/b/c.txt."""
        base = self.dir.replace("\\", "/")
        self.lua.execute(f'__LUA_BASE = "{base}"')
        self.lua.execute(r"""
            local base = __LUA_BASE
            local function searcher(name)
                local rel = name:gsub("%.", "/")
                local path = base .. "/" .. rel .. ".txt"
                local f = io.open(path, "r")
                if not f then return "\n\tno file " .. path end
                local src = f:read("*a"); f:close()
                local chunk, err = loadstring(src, "@" .. path)
                if not chunk then error(err) end
                return chunk
            end
            -- Lua 5.1: package.loaders; insert ahead of default searchers
            table.insert(package.loaders, 1, searcher)
        """)

    def module_for(self, opcode: int) -> str:
        # normalize like main.txt: try raw, then &0x0FFFFFFF, then |0x80000000
        for k in (opcode, opcode & 0x0FFFFFFF, opcode | 0x80000000):
            if k in self.opcode_to_module:
                return self.opcode_to_module[k]
        raise KeyError(f"no module for opcode 0x{opcode:08X}")

    def _protocol(self, opcode: int):
        mod = self.module_for(opcode)
        if mod not in self._proto_cache:
            self._proto_cache[mod] = self.lua.require("public.netimpl." + mod)
        return self._proto_cache[mod]

    def decode(self, opcode: int, payload: bytes, kind: str = "request") -> dict:
        proto = self._protocol(opcode)
        factory = proto["create_request"] if kind == "request" else proto["create_response"]
        obj = factory()
        buf = CByteBuffer(payload)
        obj["unserial"](buf)
        return _lua_to_py(obj)

    def encode(self, opcode: int, fields: dict, kind: str = "response") -> bytes:
        proto = self._protocol(opcode)
        factory = proto["create_response"] if kind == "response" else proto["create_request"]
        obj = factory()
        self._fill(obj, fields, proto)
        buf = CByteBuffer()
        obj["serial"](buf)
        return buf.ToBytes()

    def _fill(self, struct, data: dict, proto):
        for k, v in data.items():
            struct[k] = self._convert(v, proto)

    def _convert(self, v, proto):
        """Convert Python -> Lua, building nested protocol sub-structs (which carry
        their own serial/unserial) for dicts that match a create_<NAME> factory."""
        if isinstance(v, (list, tuple)):
            t = self.lua.table()
            for i, item in enumerate(v, start=1):
                t[i] = self._convert(item, proto)
            return t
        if isinstance(v, dict):
            sub = self._build_substruct(v, proto)
            return sub if sub is not None else _py_to_lua(self.lua, v)
        return v

    def _build_substruct(self, data: dict, proto):
        fac = self._match_factory(proto, set(data.keys()))
        if fac is None:
            return None
        st = fac()
        for k, val in data.items():
            st[k] = self._convert(val, proto)
        return st

    def _match_factory(self, proto, keys: set):
        """Find the create_<NAME> sub-struct factory whose field set is the tightest
        superset of `keys` (excludes create_request/create_response)."""
        best, best_extra = None, None
        for name, fac in proto.items():
            if not (isinstance(name, str) and name.startswith("create_")):
                continue
            if name in ("create_request", "create_response"):
                continue
            if type(fac).__name__ != "_LuaFunction":
                continue
            sample = fac()
            fields = {k for k in sample.keys() if k not in ("serial", "unserial")}
            if keys <= fields:
                extra = len(fields - keys)
                if best is None or extra < best_extra:
                    best, best_extra = fac, extra
        return best


def _is_lua_table(obj) -> bool:
    return type(obj).__name__ == "_LuaTable"


def _lua_to_py(obj):
    if type(obj).__name__ == "_LuaFunction":
        return None
    if not _is_lua_table(obj):
        return obj
    items = [(k, v) for k, v in obj.items()
             if k not in ("serial", "unserial") and type(v).__name__ != "_LuaFunction"]
    keys = [k for k, _ in items]
    # array-like (contiguous 1..n integer keys) -> list, else dict
    if keys and all(isinstance(k, int) for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
        return [_lua_to_py(v) for _, v in sorted(items)]
    return {k: _lua_to_py(v) for k, v in items}


def _py_to_lua(lua, v):
    if isinstance(v, (list, tuple)):
        t = lua.table()
        for i, item in enumerate(v, start=1):
            t[i] = _py_to_lua(lua, item)
        return t
    if isinstance(v, dict):
        t = lua.table()
        for k, item in v.items():
            t[k] = _py_to_lua(lua, item)
        return t
    return v


# --- smoke test: round-trip the real GBM_LOGIN_GAME via the client's Lua ---
if __name__ == "__main__":
    p = LuaProto()
    print(f"loaded {len(p.opcode_to_module)} opcodes from netdefine")

    GBM_LOGIN_GAME = 0x00010001
    # decode a request the client would send
    req_bytes = (lambda: (b := CByteBuffer(), b.WriteString("sess-abc"),
                          b.WriteUShort(1), b.ToBytes())[-1])()
    req = p.decode(GBM_LOGIN_GAME, req_bytes, kind="request")
    assert req["m_strSessionID"] == "sess-abc" and req["m_nServerID"] == 1, req
    print("decoded request:", req)

    # encode a response the client will parse
    resp = p.encode(GBM_LOGIN_GAME, {
        "m_nRetCode": 0,
        "m_nLastLoginPlayerID": "0",
        "m_vecPlayers": [],
    }, kind="response")
    # verify by decoding it back
    back = p.decode(GBM_LOGIN_GAME, resp, kind="response")
    assert back["m_nRetCode"] == 0, back
    print("encoded+decoded response OK, payload bytes:", resp.hex())
    print("luaproto smoke test OK")
