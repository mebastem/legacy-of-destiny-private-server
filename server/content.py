"""
content.py — build the full AOI population for a map from the client's own static
data (tb_mapmonster / tb_npc / tb_teleport / tb_monsterinfo), so every monster
spawn, NPC, and teleport appears at its real coordinates.

network coord = world coord * 10 (GetTransPosUp). Monster attrs are POSITIONAL per
MONSTER_AOI_ATTR = [MAXHP, HP, LEVEL, SPEED, CAMP].
"""

from __future__ import annotations

import random

# NPCs and teleports are created CLIENT-SIDE from config on scene enter, so we must
# NOT also push them via AOI (that double-spawns them). We only push MONSTERS.
MONSTERS_PER_POINT_MIN = 10   # dense packs per spawn point
MONSTERS_PER_POINT_MAX = 15
MAX_MONSTERS_PER_MAP = 120    # hard cap so big dungeon/PK maps don't flood + lag the client


def _iter(tbl):
    """Yield (key, value) for a lupa Lua table with integer keys."""
    if tbl is None:
        return
    for k, v in tbl.items():
        yield k, v


def build_map_aoi(proto, mapid: int) -> list[dict]:
    mm = proto.lua.require("public.staticdata.tb_mapmonster")
    mi = proto.lua.require("public.staticdata.tb_monsterinfo")

    events: list[dict] = []
    uid = 900000

    # --- monsters only (NPCs + teleports are created client-side from config) ---
    for key, e in _iter(mm):
        if int(e["mapid"]) != mapid:
            continue
        if len(events) >= MAX_MONSTERS_PER_MAP:
            break
        info = mi[int(e["monsterid"])]
        if info is None:
            continue
        bx, bz = int(e["born_x"]), int(e["born_z"])
        radius = max(int(e["born_radius"] or 1), 3)
        # 10-15 per pack, but never more than the config's intended count (keeps bosses at 1)
        count = min(random.randint(MONSTERS_PER_POINT_MIN, MONSTERS_PER_POINT_MAX),
                    int(e["count"] or 1))
        attr = [int(info["hp"]), int(info["hp"]), int(info["level"]),
                int(info["speed"]), 0]  # MAXHP, HP, LEVEL, SPEED, CAMP
        for _ in range(count):
            if len(events) >= MAX_MONSTERS_PER_MAP:
                break
            ox = random.randint(-radius, radius)
            oz = random.randint(-radius, radius)
            uid += 1
            events.append({
                "type": 2,  # ADD_MONSTER
                "m_nUID": str(uid), "m_nMonsterID": int(key),
                "x": (bx + ox) * 10, "y": (bz + oz) * 10, "m_vecAttr": attr,
            })

    return events


if __name__ == "__main__":
    from luaproto import LuaProto
    p = LuaProto()
    evs = build_map_aoi(p, 1)
    print("map 1 monsters:", len(evs), "| encoded CL_AOI body bytes:", len(p.encode_aoi(evs)))
