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
        self.atk = 200                 # player attack (from enter-world attrs)
        self.monsters: dict[str, int] = {}   # monster uid -> current hp (server-side)

    def push(self, opcode: int, fields: dict):
        """Send an unsolicited message to the client (e.g. server-time)."""
        body = proto.encode(opcode, fields, kind="response")
        self.writer.write(HEADER.pack(0, opcode, len(body)) + body)

    def push_aoi(self, events: list[dict]):
        """Send a CL_AOI push (attack/damage/spawn/del events)."""
        body = proto.encode_aoi(events)
        self.writer.write(HEADER.pack(0, op.CL_AOI, len(body)) + body)


async def spawn_world(s: Session):
    """After the scene loads, push the map's full AOI population (monsters, NPCs,
    teleports) at their real coordinates, built from the client's static data."""
    await asyncio.sleep(3)            # let the client finish loading the scene
    if not s.alive:
        return
    try:
        events = content.build_map_aoi(proto, s.mapid)
        # remember each monster's current HP so we can resolve attacks server-side
        s.monsters = {e["m_nUID"]: e["m_vecAttr"][1] for e in events if e["type"] == 2}
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
    asyncio.create_task(spawn_world(s))   # populate the map a few seconds after entry
    # minimal valid enter-world snapshot; spawn at map 1, tile (100,100).
    return [(op.GW_LOGIN_GATEWAY, {
        "m_nRetCode": 0,
        "playerid": s.player_id,
        "nickname": "Aragorn",
        # network coord = world coord * 10 (GetTransPosUp). Map 1 "Sunrise Village"
        # spawn (tb_map.enter) is world (x=44, z=136) -> network (440, 1360).
        "mapid": 1, "x": 440, "y": 1360,
        # flat [attrId, value, ...] (ids from business.txt ATTR_TYPE). PROFESSION is
        # required or SetBattleSkill crashes (default_skill[profession] == nil).
        "m_vecAttr": [
            1, 200,      # ATK
            2, 200,      # DEF
            3, 10000,    # MAXHP
            13, 600,     # SPEED
            15, 10000,   # HP (current)
            101, 12,     # LEVEL
            102, 1,      # SEX
            103, 0,      # VIP
            104, 5000,   # POWER
            108, 2,      # PROFESSION (matches char list)
            109, 1,      # CAMP
        ],
        # [skillId, level, ...] active skills for profession 2 (201 = basic attack).
        # Needed or the skill bar never inits and the attack button binds to nothing.
        "skills": [201, 1, 202, 1, 203, 1, 204, 1, 210, 1, 211, 1],
        "m_strGuildName": "", "m_nGuildID": 0, "m_nGuildJob": 0,
        "m_nExp": 0, "m_nAOISetting": 0, "m_nCreatedTime": 0,
        "m_vecTalent": [], "m_nServerOpen": 0, "m_nUserType": 0,
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
    effect = 0x01 | (0x02 if crit else 0)            # HIT (+CRITICAL)
    dead = hp <= 0
    if dead:
        effect |= 0x04                               # DIE
        s.monsters.pop(target, None)
    else:
        s.monsters[target] = hp

    pid = s.player_id or DEMO_PID
    s.push_aoi([
        {"type": 9, "m_nAvatarID": pid, "m_nTargetID": target, "m_nSkillID": skill},      # ATTACK
        {"type": 10, "m_nAvatarID": target, "m_nAttackerID": pid,                          # DAMAGE
         "m_nEffect": effect, "m_nDamage": dmg, "m_nSkillID": skill},
    ])
    log.info("ATTACK target=%s dmg=%d%s hp_left=%d", target, dmg,
             " CRIT" if crit else "", max(0, hp))
    return [(op.GW_ATTACK, {"m_nRetCode": 0})]


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
    log.info("TELEPORT id=%d -> map %d at world(%d,%d)", tid, to_map, to_x, to_z)
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
