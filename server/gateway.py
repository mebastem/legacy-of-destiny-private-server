"""
gateway.py — the single TCP endpoint the game client connects to.

Frames packets per docs/01-PROTOCOL.md, dispatches by servantname, and replies
using the client's own Lua message definitions (luaproto). This is Milestone 2:
enough of the login flow to reach the character-select screen and enter world.

Run:  python gateway.py           (listens on 0.0.0.0:7001)
Test: python testclient.py        (in another terminal)
"""

from __future__ import annotations

import asyncio
import os
import random
import struct
import time
import logging

from luaproto import LuaProto
import opcodes as op
import content

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gateway")

HEADER = struct.Struct("<IIH")  # serialno, servantname, size
HOST, PORT = "0.0.0.0", 7001
# address the client should use for the gateway (same process; reachable from the device)
PUBLIC_HOST = os.environ.get("LOD_GATEWAY_HOST", "192.168.1.5")
PUBLIC_PORT = int(os.environ.get("LOD_GATEWAY_PORT", "7001"))

proto = LuaProto()

# the client's own teleport config: tb_teleport[currentMapId][teleportIndex] -> {to_mapid,to_x,to_z}
TELEPORT_CFG = proto.lua.require("public.staticdata.tb_teleport")
# exp needed to advance from a given level: tb_lvexp[level].next_lv_exp
LVEXP_CFG = proto.lua.require("public.staticdata.tb_lvexp")
# real game data for exact exp/power
MAPMONSTER_CFG = proto.lua.require("public.staticdata.tb_mapmonster")
MONSTERINFO_CFG = proto.lua.require("public.staticdata.tb_monsterinfo")
# 战力 weights by attr id, exact from tb_paramter.power (tb_paramter has load-order
# deps that prevent standalone require). power = floor(sum(weight[id]*value)).
POWER_WEIGHTS = {1: 9, 2: 18, 3: 1, 4: 66, 5: 83, 6: 50, 7: 66, 8: 25,
                 9: 200, 10: 20, 11: 10, 12: 20}

# player base attributes [(attrId, value)] (ids from business.txt ATTR_TYPE).
# Per-level base-stat growth was server-side (not in the client data), so these are
# fixed starter stats; POWER is computed from them with the real formula.
PLAYER_BASE_ATTRS = [
    (1, 200),    # ATK
    (2, 200),    # DEF
    (3, 10000),  # MAXHP
    (13, 600),   # SPEED
    (15, 10000), # HP (current)
    (102, 1),    # SEX
    (103, 0),    # VIP
    (108, 2),    # PROFESSION
    (109, 1),    # CAMP
]


def exp_to_next(level: int) -> int:
    row = LVEXP_CFG[level]
    return int(row["next_lv_exp"]) if row is not None else 0


def _flatten(attr_pairs) -> list:
    out = []
    for aid, val in attr_pairs:
        out += [aid, val]
    return out


def compute_power(attr_pairs) -> int:
    """Exact 战力 formula: sum of attr_value * power_weight[attr_id] (ids 1-12)."""
    total = 0
    for aid, val in attr_pairs:
        total += POWER_WEIGHTS.get(aid, 0) * int(val)
    return int(total)


def monster_exp(mapmonster_key: int) -> int:
    """Real exp reward for killing a monster (tb_monsterinfo[monsterid].exp)."""
    mm = MAPMONSTER_CFG[mapmonster_key]
    if mm is None:
        return 0
    info = MONSTERINFO_CFG[int(mm["monsterid"])]
    return int(info["exp"]) if info is not None else 0

# ---- toy persistent-ish state (in-memory; M3+ swaps for real storage) ----
# one demo account -> its characters
DEMO_PID = "100000000000001"
ACCOUNTS: dict[str, list[dict]] = {
    "demo-session": [
        {
            "m_nPlayerID": DEMO_PID, "m_nLevel": 12, "m_nSex": 1,
            "m_nProfession": 2, "m_nCamp": 1, "m_strNickname": "Aragorn",
            "m_nStatus": 0,
        }
    ]
}


# ---- handler registry: opcode (normalized) -> async fn(session, request)->list[(opcode, fields)] ----
HANDLERS = {}


def handler(opcode):
    def deco(fn):
        HANDLERS[opcode & 0x0FFFFFFF] = fn
        return fn
    return deco


class Session:
    def __init__(self, peer, writer):
        self.peer = peer
        self.writer = writer
        self.session_id = None
        self.player_id = None
        self.mapid = 1
        self.x = 440
        self.y = 1360
        self.alive = True
        self.time_task = None
        self.spawn_task = None
        self.atk = 200                 # player attack (from enter-world attrs)
        # progression (mirrors the enter-world attrs)
        self.level = 12
        self.exp = 0
        self.power = 5000
        self.maxhp = 10000
        self.monsters: dict[str, int] = {}        # monster uid -> current hp
        self.monster_spawns: dict[str, dict] = {} # monster uid -> ADD_MONSTER template (for respawn)
        self.monster_reborn: dict[str, int] = {}  # monster uid -> respawn seconds

    def push(self, opcode: int, fields: dict):
        """Send an unsolicited message to the client (e.g. server-time)."""
        body = proto.encode(opcode, fields, kind="response")
        self.writer.write(HEADER.pack(0, opcode, len(body)) + body)

    def push_aoi(self, events: list[dict]):
        """Send a CL_AOI push (attack/damage/spawn/del events)."""
        body = proto.encode_aoi(events)
        self.writer.write(HEADER.pack(0, op.CL_AOI, len(body)) + body)


def _start_spawn(s: Session):
    """(Re)start map population, cancelling any pending one so monsters can't stack
    when login/teleport fire close together."""
    if s.spawn_task:
        s.spawn_task.cancel()
    s.spawn_task = asyncio.create_task(spawn_world(s))


async def spawn_world(s: Session):
    """After the scene loads, push the map's full AOI population (monsters, NPCs,
    teleports) at their real coordinates, built from the client's static data."""
    await asyncio.sleep(3)            # let the client finish loading the scene
    if not s.alive:
        return
    try:
        events = content.build_map_aoi(proto, s.mapid)
        # remember each monster's HP + spawn template so attacks/respawns work
        s.monsters = {e["m_nUID"]: e["m_vecAttr"][1] for e in events if e["type"] == 2}
        s.monster_spawns = {e["m_nUID"]: e for e in events if e["type"] == 2}
        body = proto.encode_aoi(events)
        s.writer.write(HEADER.pack(0, op.CL_AOI, len(body)) + body)
        await s.writer.drain()
        n = {2: 0, 3: 0, 4: 0}
        for e in events:
            n[e["type"]] = n.get(e["type"], 0) + 1
        log.info("pushed CL_AOI map %d: %d monsters, %d npcs, %d teleports",
                 s.mapid, n[2], n[3], n[4])
    except (ConnectionError, asyncio.CancelledError):
        pass
    except Exception as e:
        log.exception("spawn_world failed: %s", e)


async def kill_and_respawn(s: Session, uid: str):
    """Remove a dead monster after its death animation, then respawn it at its
    spawn point after the configured reborn time (cycle spawning)."""
    template = s.monster_spawns.get(uid)
    reborn = random.randint(3, 12)                      # cycle respawn speed for a lively feel
    home_map = s.mapid                                  # don't respawn onto a different map
    try:
        await asyncio.sleep(1.5)                       # let the death animation play
        if not s.alive or s.mapid != home_map:
            return
        s.push_aoi([{"type": 7, "m_nAvatarID": uid}])  # AOI_DEL -> corpse disappears
        await s.writer.drain()
        if template is None:
            return
        await asyncio.sleep(reborn)                    # respawn delay
        if not s.alive or s.mapid != home_map:
            return
        s.monsters[uid] = template["m_vecAttr"][1]      # restore full HP
        s.push_aoi([template])                          # re-spawn at its born point
        await s.writer.drain()
        log.info("respawned monster %s after %ds", uid, reborn)
    except (ConnectionError, asyncio.CancelledError):
        pass


async def time_pusher(s: Session):
    """Keep the socket alive: push CL_SERVER_TIME every few seconds. The client
    drops the connection if server time stops advancing (CNetMonitor.UpdateServerTime)."""
    try:
        while s.alive:
            s.push(op.CL_SERVER_TIME, {"m_nTime": int(time.time()) & 0xFFFFFFFF})
            await s.writer.drain()
            await asyncio.sleep(5)
    except (ConnectionError, asyncio.CancelledError):
        pass


@handler(op.GBM_REGIST_USER)
async def on_regist_user(s: Session, req: dict):
    s.session_id = req.get("m_strUserName") or "devplayer"
    log.info("REGIST_USER user=%s -> ok", s.session_id)
    return [(op.GBM_REGIST_USER, {"m_nRetCode": 0})]


@handler(op.GBM_LOGIN_GAME)
async def on_login_game(s: Session, req: dict):
    s.session_id = req.get("m_strSessionID")
    chars = ACCOUNTS.get(s.session_id) or ACCOUNTS["demo-session"]
    log.info("LOGIN_GAME session=%s -> %d character(s)", s.session_id, len(chars))
    return [(op.GBM_LOGIN_GAME, {
        "m_nRetCode": 0,
        "m_nLastLoginPlayerID": chars[0]["m_nPlayerID"] if chars else "0",
        "m_vecPlayers": chars,
    })]


@handler(op.GBM_LOGIN_PLAYER)
async def on_login_player(s: Session, req: dict):
    pid = str(req.get("m_nPlayerID", DEMO_PID))
    s.player_id = pid
    log.info("LOGIN_PLAYER pid=%s -> gateway %s:%d", pid, PUBLIC_HOST, PUBLIC_PORT)
    return [(op.GBM_LOGIN_PLAYER, {
        "m_nRetCode": 0,
        "m_strGateway": PUBLIC_HOST,
        "m_nPort": PUBLIC_PORT,
        "m_nPlayerID": pid,
        "m_nChallenge": 1,
    })]


@handler(op.GBM_CREATE_PLAYER)
async def on_create_player(s: Session, req: dict):
    # req fields vary; we mirror what the client sent into a new character.
    name = req.get("m_strNickname") or req.get("name") or "NewHero"
    chars = ACCOUNTS.setdefault(s.session_id or "demo-session", [])
    new_pid = str(int(DEMO_PID) + len(chars) + 1)
    chars.append({
        "m_nPlayerID": new_pid, "m_nLevel": 1,
        "m_nSex": req.get("m_nSex", 1), "m_nProfession": req.get("m_nProfession", 1),
        "m_nCamp": req.get("m_nCamp", 1), "m_strNickname": name, "m_nStatus": 0,
    })
    log.info("CREATE_PLAYER name=%s pid=%s", name, new_pid)
    # respond with the refreshed login/char list shape
    return [(op.GBM_LOGIN_GAME, {
        "m_nRetCode": 0, "m_nLastLoginPlayerID": new_pid, "m_vecPlayers": chars,
    })]


@handler(op.GW_LOGIN_GATEWAY)
async def on_login_gateway(s: Session, req: dict):
    s.player_id = str(req.get("m_nPlayerID", DEMO_PID))
    log.info("LOGIN_GATEWAY enter-world pid=%s", s.player_id)
    if s.time_task is None:
        s.time_task = asyncio.create_task(time_pusher(s))
    _start_spawn(s)                       # populate the map a few seconds after entry
    # minimal valid enter-world snapshot; spawn at map 1, tile (100,100).
    return [(op.GW_LOGIN_GATEWAY, {
        "m_nRetCode": 0,
        "playerid": s.player_id,
        "nickname": "Aragorn",
        # network coord = world coord * 10 (GetTransPosUp). Map 1 "Sunrise Village"
        # spawn (tb_map.enter) is world (x=44, z=136) -> network (440, 1360).
        "mapid": 1, "x": 440, "y": 1360,
        # flat [attrId, value, ...]: base attrs + LEVEL + the REAL computed POWER
        "m_vecAttr": _flatten(PLAYER_BASE_ATTRS)
                     + [101, s.level, 104, compute_power(PLAYER_BASE_ATTRS)],
        # [skillId, level, ...] active skills for profession 2 (201 = basic attack).
        # Needed or the skill bar never inits and the attack button binds to nothing.
        "skills": [201, 1, 202, 1, 203, 1, 204, 1, 210, 1, 211, 1],
        "m_strGuildName": "", "m_nGuildID": 0, "m_nGuildJob": 0,
        "m_nExp": 0, "m_nAOISetting": 0, "m_nCreatedTime": 0,
        "m_vecTalent": [], "m_nServerOpen": 0, "m_nUserType": 0,
    }), (op.CL_PLAYER_ITEMS, {
        # empty inventory — initializes the bag so the main-view (and skill bar /
        # fight buttons) can finish setting up instead of crashing on pairs(nil)
        "m_vecItem": [], "m_vecEquip": [], "m_vecTreasure": [], "m_vecWarSoul": [],
    })]


# --- per-system handlers (built out slowly; minimal valid responses) ---
@handler(op.GW_FUNCTION_NOTICE)
async def on_function_notice(s: Session, req: dict):
    return [(op.GW_FUNCTION_NOTICE, {"m_nRetCode": 0, "m_nNoticeID": req.get("m_nNoticeID", 0)})]


@handler(op.GW_EQUIP_PANEL)
async def on_equip_panel(s: Session, req: dict):
    return [(op.GW_EQUIP_PANEL, {"m_nAtk": 200, "m_nDef": 200, "m_nHp": 10000,
                                 "m_nIndex": req.get("m_nIndex", 0)})]


@handler(op.GW_OFFICE)
async def on_office(s: Session, req: dict):
    return [(op.GW_OFFICE, {"m_nRetCode": 0, "m_nOpt": req.get("m_nOpt", 1),
                            "m_nLevel": 1, "m_nUsed": 0, "m_nMax": 100})]


@handler(op.GW_ATTACK)
async def on_attack(s: Session, req: dict):
    target = str(req.get("m_nTarget", 0))
    skill = int(req.get("m_nSkill", 0))
    hp = s.monsters.get(target)
    if hp is None:
        return [(op.GW_ATTACK, {"m_nRetCode": 0})]   # unknown/dead target, just ack

    crit = random.random() < 0.25
    dmg = random.randint(int(s.atk * 0.8), int(s.atk * 1.2)) * (2 if crit else 1)
    hp -= dmg
    dead = hp <= 0
    effect = 0x01 | (0x02 if crit else 0) | (0x04 if dead else 0)   # HIT (+CRIT)(+DIE)
    pid = s.player_id or DEMO_PID

    events = [
        {"type": 9, "m_nAvatarID": pid, "m_nTargetID": target, "m_nSkillID": skill},      # ATTACK
        {"type": 10, "m_nAvatarID": target, "m_nAttackerID": pid,                          # DAMAGE
         "m_nEffect": effect, "m_nDamage": dmg, "m_nSkillID": skill},
    ]
    if dead:
        tmpl = s.monster_spawns.get(target)
        exp_reward = monster_exp(int(tmpl["m_nMonsterID"])) if tmpl else 0   # REAL exp
        s.monsters.pop(target, None)
        asyncio.create_task(kill_and_respawn(s, target))    # despawn + respawn cycle
    else:
        s.monsters[target] = hp
        # update the monster's HP bar (ATTR_CHANGE, HP attr = 15)
        events.append({"type": 11, "m_nAvatarID": target, "m_vecAttr": [15, max(0, hp)]})

    s.push_aoi(events)
    if dead:
        grant_exp(s, exp_reward)                            # real tb_monsterinfo.exp
    log.info("ATTACK target=%s dmg=%d%s hp_left=%d", target, dmg,
             " CRIT" if crit else "", max(0, hp))
    return [(op.GW_ATTACK, {"m_nRetCode": 0})]


def grant_exp(s: Session, amount: int):
    """Award the real monster exp and handle level-ups using the game's exp curve.
    NOTE: per-level base-stat growth was server-side (not in the client data), so
    leveling raises LEVEL/EXP only — power stays the real formula value over your
    (gear/buff-driven) attributes, which we don't simulate."""
    if amount <= 0:
        return
    s.exp += amount
    leveled = False
    while True:
        need = exp_to_next(s.level)
        if need and s.exp >= need:
            s.exp -= need
            s.level += 1
            leveled = True
        else:
            break
    info = [2, s.exp]                       # UPDATE_INFO.EXP
    if leveled:
        info += [1, s.level]               # UPDATE_INFO.LEVEL
    s.push(op.CL_UPDATE_INFO, {"m_vecInfo": info})
    if leveled:
        s.push_aoi([{"type": 11, "m_nAvatarID": s.player_id or DEMO_PID,
                     "m_vecAttr": [101, s.level]}])   # update LEVEL on the HUD
        log.info("LEVEL UP -> %d (exp left %d)", s.level, s.exp)


@handler(op.GW_PLAYER_MOVE)
async def on_move(s: Session, req: dict):
    s.x, s.y = req.get("x", 0), req.get("y", 0)
    return [(op.GW_PLAYER_MOVE, {"m_nRetCode": 0})]


@handler(op.GW_TELEPORT)
async def on_teleport(s: Session, req: dict):
    tid = int(req.get("m_nTeleportID", 0))
    try:
        entry = TELEPORT_CFG[s.mapid][tid]          # client's own tb_teleport
        to_map = int(entry["to_mapid"])
        to_x, to_z = int(entry["to_x"]), int(entry["to_z"])
    except Exception as e:
        log.warning("teleport %d on map %d not found: %s", tid, s.mapid, e)
        return [(op.GW_TELEPORT, {"m_nRetCode": 1, "m_nMapID": s.mapid, "x": s.x, "y": s.y})]
    s.mapid, s.x, s.y = to_map, to_x * 10, to_z * 10   # network coord = world*10
    # drop the old map's monster state; the new map gets repopulated after it loads
    s.monsters.clear(); s.monster_spawns.clear(); s.monster_reborn.clear()
    log.info("TELEPORT id=%d -> map %d at world(%d,%d)", tid, to_map, to_x, to_z)
    _start_spawn(s)                       # populate the destination map's monsters
    return [(op.GW_TELEPORT, {"m_nRetCode": 0, "m_nMapID": to_map, "x": s.x, "y": s.y})]


@handler(op.GW_TELEPORT_FINISH)
async def on_teleport_finish(s: Session, req: dict):
    return []  # cm_empty ack


@handler(op.GW_HEARTBEAT)
async def on_heartbeat(s: Session, req: dict):
    return [(op.CL_SERVER_TIME, {"m_nTime": int(time.time()) & 0xFFFFFFFF})]


@handler(op.GW_COMEBACK)
async def on_comeback(s: Session, req: dict):
    return []  # cm_empty "unstuck" signal; nothing to return


async def dispatch(s: Session, servantname: int, payload: bytes):
    key = servantname & 0x0FFFFFFF
    fn = HANDLERS.get(key)
    if not fn:
        log.warning("no handler for opcode 0x%08X (%s)", servantname,
                    proto.opcode_to_module.get(servantname, "?"))
        return []
    try:
        req = proto.decode(servantname, payload, kind="request")
    except Exception as e:
        log.warning("decode failed for 0x%08X: %s", servantname, e)
        req = {}
    outs = await fn(s, req)
    frames = []
    for out_op, fields in outs:
        body = proto.encode(out_op, fields, kind="response")
        frames.append((out_op, body))
    return frames


async def handle_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername")
    s = Session(peer, writer)
    log.info("client connected: %s", peer)
    try:
        while True:
            head = await reader.readexactly(10)
            serialno, servantname, size = HEADER.unpack(head)
            payload = await reader.readexactly(size) if size else b""
            log.info("recv op=0x%08X size=%d", servantname, size)
            for out_op, body in await dispatch(s, servantname, payload):
                writer.write(HEADER.pack(0, out_op, len(body)) + body)
            await writer.drain()
    except asyncio.IncompleteReadError:
        log.info("client disconnected: %s", peer)
    except Exception as e:
        log.exception("connection error: %s", e)
    finally:
        s.alive = False
        if s.time_task:
            s.time_task.cancel()
        writer.close()


async def main():
    server = await asyncio.start_server(handle_conn, HOST, PORT)
    log.info("gateway listening on %s:%d", HOST, PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
