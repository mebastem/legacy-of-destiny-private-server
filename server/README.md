# LoD private server

## Setup (once)
```
cd server
python -m pip install -r requirements.txt   # installs lupa (embedded Lua 5.1)
```

## Run it locally
Terminal 1 — start the gateway:
```
python gateway.py        # listens on 0.0.0.0:7001
```
Terminal 2 — run the stand-in client (proves the login flow):
```
python testclient.py
```
Expected: the test client logs in, prints the character list (Aragorn), and
enters the world at map 1 (100,100), ending with `LOGIN FLOW OK`.

## Verify the building blocks in isolation
```
python codec.py      # byte-exact CByteBuffer port -> "codec self-test OK"
python luaproto.py   # loads the client's Lua, round-trips a message
```

## Files
- `codec.py`     — byte-exact port of the client's `CByteBuffer`.
- `luaproto.py`  — embeds Lua 5.1, runs the client's own message `serial`/`unserial`.
- `opcodes.py`   — named opcode constants (subset).
- `gateway.py`   — the single TCP endpoint the game client connects to.
- `testclient.py`— a Python stand-in for the game client (no Android device needed yet).

## Notes
- If you get `address already in use` on port 7001, a previous `gateway.py` is
  still running — stop it first (on Windows PowerShell:
  `Get-NetTCPConnection -LocalPort 7001 -State Listen | Stop-Process -Id {$_.OwningProcess} -Force`).
- See `../docs/` for the protocol spec, architecture, and roadmap.
