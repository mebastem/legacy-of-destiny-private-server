# Server Architecture & Reconstruction Strategy

## Original server topology (inferred)

The opcode prefixes in `netdefine.txt` reveal a **KBEngine / BigWorld-style
distributed server**. Seven apps:

| Prefix   | App            | Role |
|----------|----------------|------|
| `0x0001` | **globalmgr**  | account login, player-list, registration, character create/delete, kicks |
| `0x0002` | **dbmgr**      | persistence (accounts, players, items, mail, guilds, market, …) |
| `0x0003` | **cellapp**    | world simulation — movement, combat, AOI, spawns, dungeons |
| `0x0005` | **gateway**    | **the only app the client connects to**; proxies to the others |
| `0x0006` | gateway-internal | inter-app gateway protocol |
| `0x8007` | **client (CL)**| server→client *push* messages (e.g. `CL_SERVER_TIME`, float text) |
| `0x0009` | centermgr      | cross-server / cross-zone features (KF = "kuafu") |

**Key simplification for us:** the client opens a single TCP connection to the
*gateway*. It never talks to globalmgr/dbmgr/cellapp directly — the gateway
routes to them server-side. So our private server is **one process that
implements the gateway endpoint** and internally fakes whatever globalmgr/
dbmgr/cellapp behavior a response needs. We don't have to reproduce the
multi-process architecture at all.

## Connection / login flow (client side)

Traced from `area_info.txt`, `common/servermgr.txt`, `game/login/biz_login_mgr.txt`:

1. **HTTP bootstrap.** Client reads `area_info.txt` (bundled) → picks an "area"
   → hits that area's HTTP API base (e.g. `https://api.ttus.noyagame.com/`,
   now dead) for *package info* + *server list* (JSON). This tells the client
   which gateway IP/port to connect to and assorted feature flags.
   → We replace this with a local HTTP stub (Milestone 1).
2. **Account login** (`gw_ac_login` / SDK) → yields a `sessionID` + `playerID` +
   `challengeID`.
3. **TCP connect** to the gateway IP/port from the server list. On
   `SOCKET_CONN_SUCCEED`, `servermgr` calls `loginMgr.CallLoginGW()`.
4. **`GBM_LOGIN_GAME` (0x00010001)** — sends `{sessionID, serverID}`; server
   replies with the **character list** (`m_vecPlayers`). → character-select screen.
5. **`GBM_CREATE_PLAYER` (0x00010003)** if the user makes a new character.
6. **`GW_LOGIN_GATEWAY` (0x00050001)** — sends `{playerID, challengeID}`; server
   replies with the **full enter-world snapshot**: `nickname, mapid, x, y,
   m_vecAttr[], skills[], guild, talents, …`. → world loads.
7. In-world: `GW_PLAYER_MOVE` (0x00050002), `GW_ATTACK` (0x00050006), etc., plus
   `CL_*` (0x8007) push messages and the 10s heartbeat (`0x00050015`).

## What we have vs. what must be rebuilt

| | Have? | Source |
|---|---|---|
| Wire format & framing | ✅ exact | C# + Lua |
| Every message's field layout | ✅ exact | `public/netimpl/**` (566 opcodes) |
| Opcode → meaning | ✅ | `netdefine.txt` comments (Chinese) |
| Game content (items/skills/monsters/maps/…) | ✅ | 202 `tb_*` static-data tables |
| Client-side system logic (UI, prediction, costs) | ✅ | 1551 `biz_*`/`game_*` Lua files |
| **Server-side behavior** (authoritative combat, AI, drops, validation, persistence) | ❌ | must be re-derived, partly from client `biz_*` mirrors |

The server behavior is the real work. The good news: in this engine a lot of
formula/cost/requirement logic is mirrored client-side for prediction, so much
of it is recoverable from the `biz_*` scripts rather than guessed.

## Our strategy: reuse the client's own Lua for serialization

Hand-porting 566 message layouts would be slow and error-prone. Instead the
server embeds a **Lua 5.1 runtime** (`lupa`, pinned to 5.1 to match SLua) and
loads the *unmodified* `public/netimpl/**` files. We provide:

- a Python `CByteBuffer` (`server/codec.py`) handed to Lua as the buffer object,
- a `network` module — we just load the client's own `network.txt` (it only
  forwards to buffer methods),
- a `require` searcher mapping module names (`public.netimpl.globalmgr.gbm_login_game`)
  to the corresponding `.txt` files.

Then encoding/decoding is *literally the client's code*, so it can never drift:
```
decode_request(opcode, payload_bytes)  -> Lua table of fields
encode_response(opcode, fields)        -> payload bytes
```
See `server/luaproto.py`. The Python layer only has to decide *what* the field
values are (the game logic) — never *how* to lay them out.

## Process layout (our server)

```
client ──TCP──> gateway.py (asyncio)        # framing, dispatch by servantname
                   │
                   ├── luaproto.py           # encode/decode via embedded Lua
                   ├── handlers/             # one module per system (login, world, …)
                   ├── content.py            # loads tb_* static data (also via Lua)
                   └── state/                # in-memory world + JSON/SQLite persistence

client ──HTTP─> bootstrap.py                 # server-list + package-info stub (M1)
```

## Milestones

- **M0** ✅ scaffold + protocol docs + byte-exact codec (`codec.py` self-test passes).
- **M1** HTTP bootstrap stub → client shows our server in its list and connects.
- **M2** Gateway TCP accept + framing + `GBM_LOGIN_GAME` → character-select screen.
- **M3** create character + `GW_LOGIN_GATEWAY` enter-world + `GW_PLAYER_MOVE` →
  a created character spawns on a map and moves. First full end-to-end loop.
- **M4+** per-system buildout (inventory, skills/combat, NPCs/quests, social, …),
  prioritized by what the client requires to not error out.
