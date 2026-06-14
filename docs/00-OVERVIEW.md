# Legacy of Destiny — Private Server Project

Goal: stand up a private server for the (now-deleted) Android MMORPG *Legacy of
Destiny*, using the decompiled client in this repo. Build incrementally,
document every step.

## What's in this repo

```
AuxiliaryFiles/
  GameAssemblies/        # original Mono DLLs (Assembly-CSharp.dll, etc.)
  path_id_map.json
ExportedProject/         # AssetRipper export of the Unity client
  Assets/
    Scripts/Assembly-CSharp/   # 446 decompiled C# files (engine framework)
    Resources/luascript/       # 1551 Lua game-logic files (saved as .txt)
      public/netimpl/          # 566 network message definitions  <-- protocol spec
      public/staticdata/       # 202 tb_* game-content tables
      main.txt, network.txt    # net dispatch + serialization aliases
    Resources/{atlas,ui,sce,texture,font,...}  # art / scenes
  ProjectSettings/, Packages/
docs/                    # <-- this documentation
server/                  # <-- our private-server implementation
```

## Feasibility (short version)

**Feasible, and unusually so**, because the parts that are normally lost survived:

- The client is **Mono + SLua**, so it decompiled cleanly and the game logic is
  **plaintext Lua**, not compiled bytecode.
- The **entire wire protocol is self-documenting**: every one of 566 messages
  has field-by-field `serial`/`unserial` Lua in `public/netimpl/`.
- All **game content** (202 `tb_*` tables) is present.

The one thing we *don't* have is **server-side behavior** (authoritative combat,
AI, drops, validation, persistence) — that was never shipped to the client and
must be re-derived. That's the bulk of the effort, but much of it is mirrored in
the client's `biz_*` prediction logic.

See `01-PROTOCOL.md` for the wire format and `02-ARCHITECTURE.md` for the server
topology, login flow, and reconstruction strategy.

## Status / progress log

| Date       | Milestone | Notes |
|------------|-----------|-------|
| 2026-06-14 | M0 start  | Reverse-engineered transport, framing, serialization, dispatch, opcode map, server topology. Ported `CByteBuffer` → `server/codec.py` (byte-exact, self-test passes). Wrote protocol + architecture docs. |
| 2026-06-14 | **M0 done** | Built `server/luaproto.py`: embeds Lua 5.1 (lupa), loads the client's own `public/netimpl/**` files, encode/decode any message via the client's `serial`/`unserial`. Verified: loads all 565 opcodes; round-trips `GBM_LOGIN_GAME` request+response to correct bytes. The "reuse client Lua" strategy is proven — no message layout will be hand-transcribed. |
| 2026-06-14 | **M2 done** | Built `server/gateway.py` (asyncio TCP, 10-byte framing, dispatch-by-servantname, handler registry) + `server/opcodes.py` + `server/testclient.py`. Encoder now builds nested protocol sub-structs (e.g. `m_vecPlayers`). **Verified running locally:** test client logs in (`GBM_LOGIN_GAME` → character list) and enters world (`GW_LOGIN_GATEWAY` → map/pos). M3 partially done (enter-world + create-player handlers exist). |
| 2026-06-14 | **Client/redirect plan** | Inspected the `.xapk` (pkg `com.legacy.titans...blade`, v1.0.16, targetSDK 28; single APK + 293MB OBB), extracted `client/base.apk` + OBB. Chose **Android Studio AVD, API 28 x86_64 Google APIs (rootable)** for the real-client test; PC reachable at `10.0.2.2`. Found server-list HTTP responses are **double-ZZBase64-obfuscated** (`DecodeSerInfo`). Full plan in `docs/03-CLIENT-REDIRECT.md`. |
| 2026-06-14 | **Emulator pivot** | Booted Google AVDs: Play image = not rootable; Google APIs (Pixel_3a_XL, API28 userdebug) = **rootable** (`uid=0`, reaches host, `adb root` OK). BUT the APK is **armeabi-v7a only** → won't install on x86_64 (`NO_MATCHING_ABIS`). Decision: run on a **rooted ARM emulator (LDPlayer/MEmu, Android 9)**. Prepped: PC LAN IP `192.168.1.5`; `server/gencert.py` → CA (`acfd89f6.0`) + leaf cert w/ SANs; `server/redirect.sh` (root→remount→hosts→system-CA). Waiting on user to install LDPlayer. |
| 2026-06-14 | **🎉 Real client connected to our stack** | LDPlayer (rooted, Android 9, armeabi-v7a OK). Redirect via **bind-mounts** (system-as-root remount hangs): `/data/local/tmp/hosts` over `/system/etc/hosts` (incl. `127.0.0.1 localhost` to fix `GetAddressIP`), and a cacerts dir bind-mount for our CA. Cracked the HTTP startup contract via logcat: `/game_apis/game_code`→base URL, `/server/load`→package JSON, `/server/serverstype`→`{datas:[...]}` blob; `bootstrap.py` now routes these. **Result: the "Local Dev" server shows on the real client** (redirect+TLS+CA+bootstrap all proven). Bind-mounts don't survive instance reboot — re-apply each time. |
| 2026-06-14 | **🏆 CHARACTER SELECT REACHED on real client** | Bypassed the Variable SDK via the Lua **hot-patch lever**: `CLuaManager.LuaLoader` loads `persistentDataPath/lua/<module>.lua` BEFORE the bundled copy. Pushed a patched `game/login/biz_login_mgr.lua` (`local strSessionID="devplayer"`) to `/sdcard/Android/data/<pkg>/files/lua/...` → skips SDK login, `OnConnectServer` connects to our gateway. Full live flow: TCP connect → `GBM_REGIST_USER`(0x10004, ret 0) → `GBM_LOGIN_GAME` → char list. **Screenshot confirms char-select showing "Aragorn" + Enter Game.** Patch saved in `server/patches/`. Next: "Enter Game" → `GBM_LOGIN_PLAYER`(0x10002) → `GW_LOGIN_GATEWAY` enter-world. (Variable SDK reverse-engineering now optional — hot-patch bypass works.) |
| 2026-06-14 | **🌍 IN THE WORLD (M3 complete)** | Implemented `GBM_LOGIN_PLAYER`(0x10002, returns our gateway addr+challenge → client reconnects as S_T_GW), enriched `GW_LOGIN_GATEWAY` enter-world `m_vecAttr` (flat [id,val,…]; **PROFESSION=108 required or SetBattleSkill crashes**; ids from business.txt). Character spawned in "Sunrise Village" with full HUD (HP 10000/10000, level, skills) — screenshot confirmed. Fixed reconnect-loop by pushing `CL_SERVER_TIME`(0x70053 {m_nTime}) every 5s + on heartbeat (`GW_HEARTBEAT` 0x50015) — connection now stable >1min. In-world the client requests per-feature opcodes (gw_equip_panel 0x50024, gw_office 0x5002E, gw_function_notice 0x500C6) — unhandled = empty panels but no disconnect. **From dead game → standing in the world on our server.** Next (M4+): per-system handlers + movement (GW_PLAYER_MOVE) + AOI. |
| 2026-06-14 | **Blocker: Variable SDK login** | "Start Game" needs a session from account login = the **Variable SDK** (`com.variable.sdk`, bundled native/Java + PlugInCode.dll). Client sends `GET /api?data=<blob>` protocol 10005 `{sessionId=sdk_token, gameId, channelId}`; we decode it, but our guessed success response is rejected (SDK retries every ~8s, never sets `strSessionID`). NEXT: decompile APK java (jadx, `com.variable.sdk.core`) for the exact 10005 response schema+signing, OR force `curPlatform==INT` to use the manual username login bypass (`OnBtnLoginHandler`). |
| 2026-06-14 | **M1 core done** | `server/serinfo.py`: faithful port of `DecodeSerInfo` + its inverse encoder. `ZZBase64` confirmed = standard base64 (cross-checked against the client's own Lua via lupa); encode→decode round-trips. `server/bootstrap.py`: catch-all HTTP(S) stub returning package-info JSON whose `recom` decodes to our gateway (`10.0.2.2:7001`); **verified locally** (fetch + decode round-trip). Remaining for M1 (empirical, needs emulator): exact request path(s), full entry/field schema, TLS cert + system-CA install. |

## How to work on this

- Protocol is the source of truth: when implementing a message, open its file in
  `ExportedProject/Assets/Resources/luascript/public/netimpl/` and read the
  `serial`/`unserial` order. The Chinese comment on its line in `netdefine.txt`
  says what it's for.
- The server **reuses those Lua files directly** (see `server/luaproto.py`) so
  we never hand-transcribe a layout.
- Build order is driven by "what does the client need next to not throw an
  error and advance the UI" — follow the login flow in `02-ARCHITECTURE.md`.

## Note on legality

This targets a game that has been shut down and removed, using assets already on
disk, for preservation/educational/personal use — the same category as
hobbyist server emulators for dead MMOs. Keep it private and non-commercial;
don't redistribute the original game's copyrighted assets.
