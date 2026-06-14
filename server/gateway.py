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
import storage
import items

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
SKILLS_CFG = proto.lua.require("public.staticdata.tb_skills")  # target_num_k = max monster targets
TASK_CFG = proto.lua.require("public.staticdata.tb_task")      # quest defs: next_task, award, ...
TB_ITEM = proto.lua.require("public.staticdata.tb_item")       # item defs: item_type, ...

# quest state values (business.txt TASK_STATUS) and update types (UPDATE_TASK)
TS_CAN_ACCEPT, TS_ACCEPTED, TS_CAN_COMMIT, TS_COMMIT = 2, 3, 4, 5
UT_STATE, UT_PROGRESS, UT_ADD, UT_DEL = 0x01, 0x02, 0x04, 0x08
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


def player_attrs(s: Session) -> list:
    """Base attributes with the character's own sex/profession/camp."""
    return [(aid, v) for (aid, v) in PLAYER_BASE_ATTRS
            if aid not in (102, 108, 109)] + [(102, s.sex), (108, s.profession), (109, s.camp)]


def total_power(s: Session) -> int:
    """战力 from base attributes plus equipped gear's power score."""
    p = compute_power(player_attrs(s))
    c = storage.find_by_pid(s.player_id)
    for itemid in (c.get("equipped", {}).values() if c else []):
        row = TB_ITEM[int(itemid)]
        if row is not None and row["effect_value"] is not None:
            p += int(row["effect_value"]["score"] or 0)
    return p


def monster_exp(mapmonster_key: int) -> int:
    """Real exp reward for killing a monster (tb_monsterinfo[monsterid].exp)."""
    mm = MAPMONSTER_CFG[mapmonster_key]
    if mm is None:
        return 0
    info = MONSTERINFO_CFG[int(mm["monsterid"])]
    return int(info["exp"]) if info is not None else 0


def monster_drop(mapmonster_key: int):
    """Pick one item id this monster drops (tb_mapmonster.map_drop = {{itemid,count,prob},..})."""
    mm = MAPMONSTER_CFG[mapmonster_key]
    if mm is None:
        return None
    md = mm["map_drop"]
    if md is None:
        return None
    n = 0
    while md[n + 1] is not None:
        n += 1
    if n == 0:
        return None
    return int(md[random.randint(1, n)][1])   # element[1] = itemid (Lua 1-based)

DEMO_PID = "0"   # fallback id only; real characters come from storage


def _players_info(c: dict) -> dict:
    """A character row -> GBM PLAYERS_INFO struct."""
    return {
        "m_nPlayerID": str(c["playerid"]), "m_nLevel": c["level"], "m_nSex": c["sex"],
        "m_nProfession": c["profession"], "m_nCamp": c["camp"],
        "m_strNickname": c["nickname"], "m_nStatus": c.get("status", 0),
    }


def skills_for(profession: int) -> list:
    """[skillId, level, ...]: basic attack + the profession's default bar skills
    (tb_paramter default_skill: prof p -> {p*100+10..+13}, basic = p*100+1)."""
    s = [profession * 100 + 1, 1]
    for i in range(10, 14):
        s += [profession * 100 + i, 1]
    return s


def save_session(s: "Session"):
    """Persist the live character state (level/exp/map/pos)."""
    if not s.player_id or not s.in_world:    # never overwrite from a non-world session
        return
    c = storage.find_by_pid(s.player_id)
    if c:
        c.update(level=s.level, exp=s.exp, mapid=s.mapid, x=s.x, y=s.y,
                 gold=s.gold, diamond=s.diamond)
        storage.save()


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
        self.in_world = False          # set once enter-world loads the character
        self.time_task = None
        self.spawn_task = None
        self.atk = 200                 # player attack (from enter-world attrs)
        self.profession = 2
        self.sex = 1
        self.camp = 1
        self.nickname = "Hero"
        self.gold = 0
        self.diamond = 0
        self.item_uid = 7000000
        # progression (loaded from storage on enter-world)
        self.level = 1
        self.exp = 0
        self.power = 5000
        self.maxhp = 10000
        self.monsters: dict[str, int] = {}        # monster uid -> current hp
        self.monster_pos: dict[str, tuple] = {}   # monster uid -> (x, y) network coords
        self.dropbags: dict[str, int] = {}        # dropbag uid -> itemid (on the ground)
        self.dropbag_uid = 8000000
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
        s.monster_pos = {e["m_nUID"]: (e["x"], e["y"]) for e in events if e["type"] == 2}
        body = proto.encode_aoi(events)
        s.writer.write(HEADER.pack(0, op.CL_AOI, len(body)) + body)
        await s.writer.drain()
        n = {2: 0, 3: 0, 4: 0}
        for e in events:
            n[e["type"]] = n.get(e["type"], 0) + 1
        log.info("pushed CL_AOI map %d: %d monsters, %d npcs, %d teleports",
                 s.mapid, n[2], n[3], n[4])
        # restore the player's saved inventory (the bag was just initialized empty)
        c = storage.find_by_pid(s.player_id)
        for it in (c.get("items", []) if c else []):
            row = TB_ITEM[it["itemid"]]
            if row is None:
                continue
            is_equip = int(row["item_type"]) == 3
            pos = it.get("bag", items.pos_for(int(row["item_type"]), is_equip))
            body = items.encode_add(it["uid"], it["itemid"], it["count"], pos, is_equip)
            s.writer.write(HEADER.pack(0, op.CL_UPDATE_ITEMS, len(body)) + body)
        await s.writer.drain()
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
    s.session_id = req.get("m_strSessionID") or "default"
    chars = storage.get_characters(s.session_id)
    log.info("LOGIN_GAME account=%s -> %d character(s)", s.session_id, len(chars))
    return [(op.GBM_LOGIN_GAME, {
        "m_nRetCode": 0,
        "m_nLastLoginPlayerID": str(chars[-1]["playerid"]) if chars else "0",
        "m_vecPlayers": [_players_info(c) for c in chars],   # empty -> client shows create screen
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
    account = s.session_id or "default"
    char = {
        "playerid": storage.next_playerid(),
        "nickname": req.get("m_strNickname") or "Hero",
        "sex": int(req.get("m_nSex", 1)),
        "profession": int(req.get("m_nProfession", 2)),
        "camp": 1, "level": 1, "exp": 0,
        "gold": 1000, "diamond": 100,    # starting funds
        "mapid": 1, "x": 440, "y": 1360, "status": 0,
        "tasks": {"1": TS_CAN_ACCEPT},   # start with the first quest available
    }
    storage.add_character(account, char)
    log.info("CREATE_PLAYER account=%s '%s' pid=%s prof=%d",
             account, char["nickname"], char["playerid"], char["profession"])
    return [(op.GBM_CREATE_PLAYER, {"m_nRetCode": 0, "m_infoPayer": _players_info(char)})]


@handler(op.GW_LOGIN_GATEWAY)
async def on_login_gateway(s: Session, req: dict):
    s.player_id = str(req.get("m_nPlayerID", DEMO_PID))
    s.in_world = True
    char = storage.find_by_pid(s.player_id)
    if char:   # load the saved character
        s.level, s.exp = char["level"], char["exp"]
        s.mapid, s.x, s.y = char["mapid"], char["x"], char["y"]
        s.profession, s.sex, s.camp = char["profession"], char["sex"], char["camp"]
        s.nickname = char["nickname"]
        s.gold, s.diamond = char.get("gold", 0), char.get("diamond", 0)
    log.info("LOGIN_GATEWAY enter-world pid=%s '%s' lvl=%d map=%d",
             s.player_id, s.nickname, s.level, s.mapid)
    if s.time_task is None:
        s.time_task = asyncio.create_task(time_pusher(s))
    _start_spawn(s)                       # populate the map a few seconds after entry
    return [(op.GW_LOGIN_GATEWAY, {
        "m_nRetCode": 0,
        "playerid": s.player_id,
        "nickname": s.nickname,
        "mapid": s.mapid, "x": s.x, "y": s.y,    # spawn where the character logged out
        "m_vecAttr": _flatten(player_attrs(s)) + [101, s.level, 104, total_power(s)],
        "skills": skills_for(s.profession),       # the character's profession skills
        "m_strGuildName": "", "m_nGuildID": 0, "m_nGuildJob": 0,
        "m_nExp": 0, "m_nAOISetting": 0, "m_nCreatedTime": 0,
        "m_vecTalent": [], "m_nServerOpen": 0, "m_nUserType": 0,
    }), (op.CL_PLAYER_ITEMS, {
        # empty inventory — initializes the bag so the main-view (and skill bar /
        # fight buttons) can finish setting up instead of crashing on pairs(nil)
        "m_vecItem": [], "m_vecEquip": [], "m_vecTreasure": [], "m_vecWarSoul": [],
    }), (op.CL_UPDATE_INFO, {
        # level/exp for the HUD bar (it reads UPDATE_INFO, not just the attribute)
        "m_vecInfo": [1, s.level, 2, s.exp],       # UPDATE_INFO.LEVEL, EXP
    }), (op.CL_PLAYER_MONEY, {
        "m_vecMoney": [1, s.diamond, 2, s.gold],   # DIAMOND, GOLD
    }), (op.CL_PLAYER_TASKS, {
        "m_nRetCode": 0,
        # only send active quests (omit COMMIT/finished ones so they don't reappear)
        "m_vecTask": [{"m_nTaskId": int(tid), "m_nState": st,
                       "m_nProgress": _task_prog(s, tid), "m_nTime": 0}
                      for tid, st in _char_tasks(s).items() if st != TS_COMMIT],
    })]


# --- per-system handlers (built out slowly; minimal valid responses) ---
@handler(op.GW_FUNCTION_NOTICE)
async def on_function_notice(s: Session, req: dict):
    return [(op.GW_FUNCTION_NOTICE, {"m_nRetCode": 0, "m_nNoticeID": req.get("m_nNoticeID", 0)})]


@handler(op.GW_EQUIP_PANEL)
async def on_equip_panel(s: Session, req: dict):
    return [(op.GW_EQUIP_PANEL, {"m_nAtk": 200, "m_nDef": 200, "m_nHp": 10000,
                                 "m_nIndex": req.get("m_nIndex", 0)})]


@handler(op.GW_EQUIP_INFO)
async def on_equip_info(s: Session, req: dict):
    # Enhancement panel (strengthen/refine/gem/enchant/polish/star) data per part.
    # Base gear has no enhancements, so every vector is empty — this just populates
    # the panel's source table so it doesn't crash on nil (biz_equipment_main:297).
    return [(op.GW_EQUIP_INFO, {})]


@handler(op.GW_OFFICE)
async def on_office(s: Session, req: dict):
    return [(op.GW_OFFICE, {"m_nRetCode": 0, "m_nOpt": req.get("m_nOpt", 1),
                            "m_nLevel": 1, "m_nUsed": 0, "m_nMax": 100})]


AOE_RADIUS = 60   # network units (~6m) for picking nearby monsters in a skill's area


def _skill_targets(s: Session, target: str) -> int:
    """Max monsters a skill hits at once (tb_skills.target_num_k); 1 if unknown."""
    row = SKILLS_CFG[target]
    try:
        return max(1, int(row["target_num_k"]))
    except Exception:
        return 1


def _pick_victims(s: Session, target: str, cap: int) -> list:
    """Pick up to `cap` live monsters. If a monster is targeted, center on it;
    otherwise (a skill fired with no lock) center on the player and hit nearby
    monsters — that's why skills now deal damage, not just basic attacks."""
    if target in s.monsters and target in s.monster_pos:
        cx, cy = s.monster_pos[target]
        victims = [target]
    else:
        cx, cy = s.x, s.y                      # no locked target -> AoE around the player
        victims = []
    near = []
    for uid, (x, y) in s.monster_pos.items():
        if uid in victims or uid not in s.monsters:
            continue
        d2 = (x - cx) ** 2 + (y - cy) ** 2
        if d2 <= AOE_RADIUS ** 2:
            near.append((d2, uid))
    near.sort()
    for _, uid in near:
        if len(victims) >= cap:
            break
        victims.append(uid)
    return victims


@handler(op.GW_ATTACK)
async def on_attack(s: Session, req: dict):
    target = str(req.get("m_nTarget", 0))
    skill = int(req.get("m_nSkill", 0))
    pid = s.player_id or DEMO_PID
    cap = _skill_targets(s, skill)                   # area skills hit several monsters
    victims = _pick_victims(s, target, cap)
    if not victims:
        return [(op.GW_ATTACK, {"m_nRetCode": 0})]   # nothing in range, just ack
    anim_target = target if target in s.monsters else victims[0]
    events = [{"type": 9, "m_nAvatarID": pid, "m_nTargetID": anim_target, "m_nSkillID": skill}]  # ATTACK anim
    total_exp = 0
    for uid in victims:
        hp = s.monsters.get(uid)
        if hp is None:
            continue
        crit = random.random() < 0.25
        dmg = random.randint(int(s.atk * 0.8), int(s.atk * 1.2)) * (2 if crit else 1)
        hp -= dmg
        dead = hp <= 0
        effect = 0x01 | (0x02 if crit else 0) | (0x04 if dead else 0)
        events.append({"type": 10, "m_nAvatarID": uid, "m_nAttackerID": pid,
                       "m_nEffect": effect, "m_nDamage": dmg, "m_nSkillID": skill})  # DAMAGE
        if dead:
            tmpl = s.monster_spawns.get(uid)
            if tmpl:
                total_exp += monster_exp(int(tmpl["m_nMonsterID"]))
                advance_kill_quests(s, int(tmpl["m_nMonsterID"]))   # quest kill progress
                itemid = monster_drop(int(tmpl["m_nMonsterID"]))
                if itemid:
                    s.dropbag_uid += 1
                    duid = str(s.dropbag_uid)
                    dx, dy = s.monster_pos.get(uid, (s.x, s.y))
                    s.dropbags[duid] = itemid
                    events.append({"type": 5, "m_nUID": duid, "m_nItemID": itemid,
                                   "x": dx, "y": dy})   # ADD_DROPBAG
            s.monsters.pop(uid, None)
            asyncio.create_task(kill_and_respawn(s, uid))
        else:
            s.monsters[uid] = hp
            events.append({"type": 11, "m_nAvatarID": uid, "m_vecAttr": [15, max(0, hp)]})  # HP bar

    s.push_aoi(events)
    if total_exp:
        grant_exp(s, total_exp)
    log.info("ATTACK skill=%d hit %d monster(s)", skill, len(victims))
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
    save_session(s)


@handler(op.GW_PLAYER_MOVE)
async def on_move(s: Session, req: dict):
    s.x, s.y = req.get("x", 0), req.get("y", 0)
    return [(op.GW_PLAYER_MOVE, {"m_nRetCode": 0})]


def _char_tasks(s: Session) -> dict:
    """The character's quest map {taskId(str): state}, defaulting to the first quest."""
    c = storage.find_by_pid(s.player_id)
    if c is None:
        return {}
    return c.setdefault("tasks", {"1": TS_CAN_ACCEPT})


def _task_prog(s: Session, tid) -> int:
    c = storage.find_by_pid(s.player_id)
    return int(c.get("task_prog", {}).get(str(tid), 0)) if c else 0


def _kill_objective(task_id: int):
    """If a quest requires killing monsters, return (mapmonster_key, count); else None."""
    row = TASK_CFG[task_id]
    if row is None:
        return None
    c = row["commit_conds"]
    try:
        if c is not None and int(c["type"]) == 1:        # TASK_EVENT.KILL_MON
            return int(c["mid"]), int(c["count"])
    except Exception:
        pass
    return None


def advance_kill_quests(s: Session, killed_key: int):
    """On a monster kill, progress any accepted kill-quest targeting that monster."""
    c = storage.find_by_pid(s.player_id)
    if not c:
        return
    tasks = c.get("tasks", {})
    prog = c.setdefault("task_prog", {})
    for tid_str, state in list(tasks.items()):
        if state != TS_ACCEPTED:
            continue
        obj = _kill_objective(int(tid_str))
        if not obj or obj[0] != killed_key:
            continue
        need = obj[1]
        cur = min(prog.get(tid_str, 0) + 1, need)
        prog[tid_str] = cur
        updates = [{"m_nTaskId": int(tid_str), "m_nUpdateType": UT_PROGRESS, "m_nValue": cur}]
        if cur >= need:
            tasks[tid_str] = TS_CAN_COMMIT
            updates.append({"m_nTaskId": int(tid_str), "m_nUpdateType": UT_STATE, "m_nValue": TS_CAN_COMMIT})
            log.info("QUEST %s objective done (%d/%d)", tid_str, cur, need)
        s.push(op.CL_UPDATE_TASK, {"m_nRetCode": 0, "m_vecUpdateInfo": updates})
    storage.save()


def _give_award(s: Session, task_id: int):
    """Grant a quest's rewards (tb_task.award = {{type,id,count},..})."""
    row = TASK_CFG[task_id]
    if row is None or row["award"] is None:
        return
    aw = row["award"]
    i = 1
    while aw[i] is not None:
        a = aw[i]
        atype, aid, count = int(a["type"]), int(a["id"]), int(a["count"])
        if atype == 3 and aid == 51:          # OTHER / EXP
            grant_exp(s, count)
        elif atype == 2:                       # MONEY (aid = MONEY_TYPE: 1 diamond, 2 gold)
            grant_money(s, aid, count)
        elif atype == 1:                       # ITEM -> add to bag
            give_item(s, aid, count)
        i += 1


def give_item(s: Session, itemid: int, count: int = 1):
    """Add an item to the player (persisted) and push it. Equipment auto-equips to
    the worn slot (so it shows on the gear panel, not just the bag)."""
    row = TB_ITEM[itemid]
    if row is None:
        return
    itype = int(row["item_type"])
    is_equip = itype == 3
    worn = is_equip                              # gear goes straight onto the character
    pos = items.pos_for(itype, worn)             # ITEM_POS routing value
    s.item_uid += 1
    uid = s.item_uid
    c = storage.find_by_pid(s.player_id)
    if c is not None:
        c.setdefault("items", []).append(
            {"uid": uid, "itemid": int(itemid), "count": int(count), "bag": pos})
        if worn:
            c.setdefault("equipped", {})[str(uid)] = int(itemid)
        storage.save()
    body = items.encode_add(uid, int(itemid), int(count), pos, is_equip)
    s.writer.write(HEADER.pack(0, op.CL_UPDATE_ITEMS, len(body)) + body)
    if worn:
        s.power = total_power(s)
        s.push_aoi([{"type": 11, "m_nAvatarID": s.player_id or DEMO_PID, "m_vecAttr": [104, s.power]}])
    log.info("ITEM +%d x%d (pos %d%s)", itemid, count, pos, " worn" if worn else "")


def grant_money(s: Session, money_type: int, amount: int):
    """Add currency and update the client wallet."""
    if money_type == 1:
        s.diamond += amount
    elif money_type == 2:
        s.gold += amount
    else:
        return
    s.push(op.CL_PLAYER_MONEY, {"m_vecMoney": [money_type,
            s.diamond if money_type == 1 else s.gold]})
    save_session(s)
    log.info("MONEY +%d (type %d) -> gold=%d diamond=%d", amount, money_type, s.gold, s.diamond)


@handler(op.GW_TASK_OP)
async def on_task_op(s: Session, req: dict):
    op_type = int(req.get("m_nOpType", 0))
    tid = int(req.get("m_nTaskId", 0))
    tasks = _char_tasks(s)
    key = str(tid)
    if op_type == 1:                                   # accept
        obj = _kill_objective(tid)
        if obj:                                        # kill quest -> stay ACCEPTED until done
            tasks[key] = TS_ACCEPTED
            c = storage.find_by_pid(s.player_id)
            if c:
                c.setdefault("task_prog", {})[key] = 0
            new_state = TS_ACCEPTED
        else:                                          # talk quest -> completable now
            tasks[key] = TS_CAN_COMMIT
            new_state = TS_CAN_COMMIT
        s.push(op.CL_UPDATE_TASK, {"m_nRetCode": 0, "m_vecUpdateInfo":
                [{"m_nTaskId": tid, "m_nUpdateType": UT_STATE, "m_nValue": new_state}]})
        log.info("QUEST accept %d (state=%d)", tid, new_state)
    elif op_type == 2:                                 # submit -> reward + advance chain
        if tasks.get(key) != TS_CAN_COMMIT:            # objective not met yet
            log.info("QUEST submit %d rejected (state=%s)", tid, tasks.get(key))
            return [(op.GW_TASK_OP, {"m_nRetCode": 0})]
        tasks[key] = TS_COMMIT
        # mark complete, then DEL it so it's removed from the client's quest list
        s.push(op.CL_UPDATE_TASK, {"m_nRetCode": 0, "m_vecUpdateInfo": [
            {"m_nTaskId": tid, "m_nUpdateType": UT_STATE, "m_nValue": TS_COMMIT},
            {"m_nTaskId": tid, "m_nUpdateType": UT_DEL, "m_nValue": 0},
        ]})
        _give_award(s, tid)
        row = TASK_CFG[tid]
        nxt = int(row["next_task"]) if row is not None and row["next_task"] else 0
        if nxt > 0 and str(nxt) not in tasks:
            tasks[str(nxt)] = TS_CAN_ACCEPT
            # the client ignores UPDATE_TASK.ADD; a new quest must arrive via CL_PLAYER_TASKS
            s.push(op.CL_PLAYER_TASKS, {"m_nRetCode": 0, "m_vecTask":
                    [{"m_nTaskId": nxt, "m_nState": TS_CAN_ACCEPT, "m_nProgress": 0, "m_nTime": 0}]})
        log.info("QUEST submit %d -> next %d", tid, nxt)
    storage.save()
    return [(op.GW_TASK_OP, {"m_nRetCode": 0})]


@handler(op.GW_EQUIP_WEAR)
async def on_equip_wear(s: Session, req: dict):
    opt = int(req.get("m_nOpt", 1))                # 1 wear, 2 remove
    uid = str(req.get("m_nUniqueID", 0))
    c = storage.find_by_pid(s.player_id)
    if c is not None:
        equipped = c.setdefault("equipped", {})
        if opt == 1:
            itemid = next((it["itemid"] for it in c.get("items", []) if str(it["uid"]) == uid), None)
            if itemid is not None:
                equipped[uid] = int(itemid)
        else:
            equipped.pop(uid, None)
        s.power = total_power(s)
        storage.save()
        s.push_aoi([{"type": 11, "m_nAvatarID": s.player_id or DEMO_PID,
                     "m_vecAttr": [104, s.power]}])   # refresh POWER on the HUD
        log.info("EQUIP %s uid=%s -> power=%d", "wear" if opt == 1 else "remove", uid, s.power)
    return [(op.GW_EQUIP_WEAR, {"m_nRetCode": 0})]


@handler(op.GW_PICKUP)
async def on_pickup(s: Session, req: dict):
    duid = str(req.get("m_nDropbagID", 0))
    if duid in s.dropbags:
        s.dropbags.pop(duid, None)
        s.push_aoi([{"type": 13, "m_nUID": duid, "m_nVestID": "0"}])  # DEL_DROPBAG
        log.info("PICKUP dropbag %s", duid)
    return [(op.GW_PICKUP, {"m_nRetCode": 0})]


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
    s.monsters.clear(); s.monster_spawns.clear(); s.monster_pos.clear()
    save_session(s)                                     # persist the new map/position
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
        save_session(s)               # persist level/exp/map/position on disconnect
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
