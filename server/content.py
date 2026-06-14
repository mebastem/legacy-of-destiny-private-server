"""
content.py — build the full AOI population for a map from the client's own static
data (tb_mapmonster / tb_npc / tb_teleport / tb_monsterinfo), so every monster
spawn, NPC, and teleport appears at its real coordinates.

network coord = world coord * 10 (GetTransPosUp). Monster attrs are POSITIONAL per
MONSTER_AOI_ATTR = [MAXHP, HP, LEVEL, SPEED, CAMP].
"""

from __future__ import annotations

# how many monsters to actually spawn per spawn-point (configs say up to ~25;
# cap so the emulator isn't flooded while still showing every spawn location)
MONSTERS_PER_POINT = 4
MIN_RESPAWN_SECONDS = 5   # floor so config reborn_time=1 doesn't respawn instantly


def _iter(tbl):
    """Yield (key, value) for a lupa Lua table with integer keys."""
    if tbl is None:
        return
    for k, v in tbl.items():
        yield k, v


def build_map_aoi(proto, mapid: int) -> list[dict]:
    mm = proto.lua.require("public.staticdata.tb_mapmonster")
    npc = proto.lua.require("public.staticdata.tb_npc")
    tp = proto.lua.require("public.staticdata.tb_teleport")
    mi = proto.lua.require("public.staticdata.tb_monsterinfo")

    events: list[dict] = []
    reborn: dict[str, int] = {}   # monster uid -> respawn seconds
    uid = 900000

    # --- monsters: each tb_mapmonster spawn point on this map ---
    for key, e in _iter(mm):
        if int(e["mapid"]) != mapid:
            continue
        info = mi[int(e["monsterid"])]
        if info is None:
            continue
        bx, bz = int(e["born_x"]), int(e["born_z"])
        radius = int(e["born_radius"] or 1)
        count = min(int(e["count"] or 1), MONSTERS_PER_POINT)
        rb = max(int(e["reborn_time"] or 1), MIN_RESPAWN_SECONDS)
        attr = [int(info["hp"]), int(info["hp"]), int(info["level"]),
                int(info["speed"]), 0]  # MAXHP, HP, LEVEL, SPEED, CAMP
        step = max(1, radius // 2)
        for j in range(count):
            ox = ((j % 3) - 1) * step
            oz = ((j // 3) - 1) * step
            uid += 1
            events.append({
                "type": 2,  # ADD_MONSTER
                "m_nUID": str(uid), "m_nMonsterID": int(key),
                "x": (bx + ox) * 10, "y": (bz + oz) * 10, "m_vecAttr": attr,
            })
            reborn[str(uid)] = rb

    # --- NPCs: client positions them from config.npc[npcid] ---
    for npcid, e in _iter(npc):
        if int(e["mapid"]) != mapid:
            continue
        uid += 1
        events.append({"type": 3, "m_nUID": str(uid), "m_nNPCID": int(npcid)})

    # --- teleport portals on this map ---
    tlist = tp[mapid]
    if tlist is not None:
        i = 1
        while tlist[i] is not None:
            e = tlist[i]
            uid += 1
            events.append({
                "type": 4,  # ADD_TELEPORT
                "m_nUID": str(uid), "m_nTeleportID": i,
                "x": int(e["born_x"]) * 10, "y": int(e["born_z"]) * 10,
            })
            i += 1

    return events, reborn


if __name__ == "__main__":
    from luaproto import LuaProto
    p = LuaProto()
    evs, reborn = build_map_aoi(p, 1)
    by_type = {}
    for e in evs:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
    print("map 1 AOI events:", len(evs),
          "(monsters=%d npcs=%d teleports=%d)" % (by_type.get(2, 0), by_type.get(3, 0), by_type.get(4, 0)))
    body = p.encode_aoi(evs)
    print("encoded CL_AOI body bytes:", len(body))
