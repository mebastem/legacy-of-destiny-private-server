# Legacy of Destiny — Wire Protocol

Reverse-engineered from the decompiled client. Sources:
- `ExportedProject/Assets/Scripts/Assembly-CSharp/CSocket.cs` (receive framing)
- `.../CNetMonitor.cs` (send framing, dispatch, heartbeat)
- `.../CByteBuffer.cs` (serialization primitives)
- `.../CDataPackage.cs` (packet struct)
- `ExportedProject/Assets/Resources/luascript/network.txt` (Lua type aliases)
- `.../luascript/main.txt` (opcode dispatch, request/response registration)
- `.../luascript/public/netimpl/netdefine.txt` (full opcode → servant map)
- `.../luascript/public/netimpl/**` (per-message field layouts)

Transport: **TCP**, persistent connection to a "gateway". No TLS, no
compression, no encryption, no checksum on the wire (the `magic`/`checksum`/
`flag`/`version` fields in `CDataPackage` are never written to the socket —
see `CNetMonitor.SendMessage`, which writes only the 10-byte header below).

## Packet framing

Every packet is a 10-byte header followed by a payload.

| Offset | Field        | Type          | Notes |
|-------:|--------------|---------------|-------|
| 0      | `serialno`   | uint32 LE     | sequence no. Client always sends `0`. |
| 4      | `servantname`| uint32 LE     | the opcode / RPC route (see `netdefine.txt`). |
| 8      | `size`       | uint16 LE     | payload length in bytes. |
| 10     | `payload`    | `size` bytes  | message body (see per-message layouts). |

Receive logic (`CSocket.BeginReadData`): reads the 10-byte header, then waits
until `size` payload bytes are buffered, then delivers `(servantname, payload)`
to Lua.

### Large / fragmented packets (`size == 65535`)

If `size` reads as `0xFFFF`, the message is **fragmented**: the client keeps
reading additional `[serialno][servantname][size]` chunks and concatenating
their payloads until it sees a chunk whose `size < 65535` (the final chunk).
See `CSocket.BeginReadData` lines 193–241. A server only needs this when a
single response would exceed 65534 bytes (e.g. big inventory / mail dumps).
For early milestones, keep every response under 65535 bytes and you can ignore
fragmentation entirely.

> Note the send path writes `size` as a **signed** int16 (`WriteShort((short)size)`
> in `CNetMonitor.SendMessage`), but the receive path reads it **unsigned**.
> For payloads < 32768 this is identical. Match the unsigned reader: write `size`
> as uint16.

## Serialization primitives (`CByteBuffer`)

Ported byte-exact in `server/codec.py`. Key rules:

| Lua alias (`network.txt`) | CByteBuffer method | Wire format |
|---------------------------|--------------------|-------------|
| `uint8` / `int8`          | WriteByte/WriteSByte | 1 byte |
| `uint16` / `int16`        | WriteUShort/WriteShort | 2 bytes LE |
| `uint32` / `int32`        | WriteUInt/WriteInt   | 4 bytes LE |
| `uint64` / `int64`        | WriteULong/WriteLong | 8 bytes LE, **value passed as a string** |
| `float`                   | WriteFloat           | 4 bytes **big-endian** |
| `double`                  | WriteDouble          | 8 bytes **big-endian** |
| `string`                  | WriteString          | length prefix + UTF-8 |
| `size`                    | WriteSize            | varint (see below) |
| array                     | WriteArray / `writesize` + loop | count then elements |

**Strings**: length prefix is 1 byte if `len < 255`, otherwise a `0xFF` marker
byte followed by a uint16 length, then the UTF-8 bytes.

**Size (varint)**:
- `n < 255`            → 1 byte: `n`
- `255 <= n < 65535`   → `0xFF`, then uint16 `n`
- `n >= 65535`         → `0xFF 0xFF 0xFF`, then uint32 `n`

**64-bit ints are strings.** Lua 5.1 numbers are doubles, so the client marshals
`uint64`/`int64` via `Convert.ToInt64(string)`. Player IDs, money, etc. cross the
Lua boundary as decimal strings. The Python codec preserves this contract.

**Arrays / vectors** are encoded as `writesize(count)` followed by `count`
serialized elements (see any `m_vec*` field, e.g. `gbm_login_game.txt`
`response.m_vecPlayers`).

## Opcodes and the high-bit convention

Opcodes live in `public/netimpl/netdefine.txt` as `NAME = { 0xAAAABBBB, "servant.path" }`:
- the high 16 bits (`0xAAAA`) are the **servant/app prefix** (`0x0001` globalmgr,
  `0x0002` dbmgr, `0x0003` cellapp, `0x0005`+ gateway, …),
- the low 16 bits are the command within that app,
- the string is the Lua module under `public.netimpl.` that defines the message.

The dispatcher (`main.txt` `GetProtocol`) normalizes opcodes two ways:
- `sname0 = key & 0x0FFFFFFF`  (top nibble cleared)
- `sname8 = key | 0x80000000`  (bit 31 set)

and registers the handler under **both**. Practical consequence for the server:
**you may reply with the same opcode the client sent, with or without bit 31
set — the client's listener fires either way.** Simplest correct choice: echo
the request's `servantname` unchanged on the response.

## Message structure source of truth

Each message file under `public/netimpl/` defines up to three structs via
factory functions, each with `serial(buffer)` (encode) and `unserial(buffer)`
(decode):

- `create_request()`  — what the **client sends**.
- `create_response()` — what the **client expects back** (the server must produce this).
- nested `create_<NAME>()` — reusable sub-structs (e.g. `PLAYERS_INFO`).

Because the field order in `serial`/`unserial` *is* the wire format, these files
are the authoritative, complete spec for every message. The server reuses them
directly (run unchanged under an embedded Lua 5.1 runtime — see
`docs/02-ARCHITECTURE.md` and `server/luaproto.py`) so encoding can never drift
from the client.

### Worked example — `GBM_LOGIN_GAME` (0x00010001)

From `public/netimpl/globalmgr/gbm_login_game.txt`:

Request (client → server):
```
m_strSessionID : string
m_nServerID    : uint16
```
Response (server → client):
```
m_nRetCode           : uint32        (0 == SUCCESS; see errcode.txt)
m_nLastLoginPlayerID : uint64 (str)
m_vecPlayers[]       : size-prefixed array of PLAYERS_INFO {
    m_nPlayerID   : uint64 (str)
    m_nLevel      : uint16
    m_nSex        : uint8
    m_nProfession : uint8
    m_nCamp       : uint8
    m_strNickname : string
    m_nStatus     : uint8
}
```

## Dispatch & lifecycle (client side)

- Incoming bytes → `CNetMonitor` thread → `CByteBufferPool` queue → main thread
  calls Lua `HandleSocketData(cdp)` (`main.txt`).
- `recode == 101`: real message → `HandleSocketBuffer(servantname, buffer)` →
  `response.unserial()` → registered listener callbacks fire.
- `recode == 100`: synthetic connection event (`connFlag`: 0 ok / 1 fail / 2 timeout).
- `recode == 102`: synthetic disconnect event (carries ip/port).
- `m_nRetCode != SUCCESS` short-circuits dispatch and shows an error toast
  (unless the opcode is in the `ignoreProtocolList`). **So the first uint32 of
  almost every response is a return code the server must set to 0 on success.**

## Heartbeat

Once in-game, the client sends opcode **`327701`** (`0x00050015`) with an empty
payload every 10 seconds (`CNetMonitor.OnUpdate` → `EnableHeartBeatsTimer`).
The server should accept it and is expected to keep the connection alive /
reply with server time (see `CL_SERVER_TIME` listener in `servermgr.txt`).
Missing server-time updates for >2 cycles makes the client drop the socket
(`CNetMonitor.UpdateServerTime`).
