"""
testclient.py — stands in for the game client to prove the gateway works locally.

It speaks the real wire protocol (same framing + the client's own Lua message
defs) so a successful run means the server would satisfy the real client too.

Usage:
    Terminal 1:  python gateway.py
    Terminal 2:  python testclient.py
"""

from __future__ import annotations

import socket
import struct

from luaproto import LuaProto
import opcodes as op

HEADER = struct.Struct("<IIH")
proto = LuaProto()


def send(sock, opcode: int, fields: dict):
    body = proto.encode(opcode, fields, kind="request")
    sock.sendall(HEADER.pack(0, opcode, len(body)) + body)


def recv(sock):
    head = _readn(sock, 10)
    serialno, servantname, size = HEADER.unpack(head)
    payload = _readn(sock, size) if size else b""
    return servantname, payload


def _readn(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("server closed")
        buf += chunk
    return buf


def main():
    with socket.create_connection(("127.0.0.1", 7001)) as sock:
        print("connected to gateway\n")

        # 1) login -> character list
        print(">> GBM_LOGIN_GAME {session=demo-session, server=1}")
        send(sock, op.GBM_LOGIN_GAME, {"m_strSessionID": "demo-session", "m_nServerID": 1})
        sname, payload = recv(sock)
        resp = proto.decode(op.GBM_LOGIN_GAME, payload, kind="response")
        print(f"<< 0x{sname:08X} retcode={resp['m_nRetCode']}")
        chars = resp["m_vecPlayers"]
        print(f"   character list ({len(chars)}):")
        for c in chars:
            print(f"     - {c['m_strNickname']} (pid={c['m_nPlayerID']}, "
                  f"lv{c['m_nLevel']}, prof={c['m_nProfession']})")
        assert resp["m_nRetCode"] == 0 and chars, "login should return characters"

        # 2) enter world with the first character
        pid = chars[0]["m_nPlayerID"]
        print(f"\n>> GW_LOGIN_GATEWAY {{pid={pid}}}")
        send(sock, op.GW_LOGIN_GATEWAY, {"m_nPlayerID": pid, "m_nChallengeID": 0})
        sname, payload = recv(sock)
        world = proto.decode(op.GW_LOGIN_GATEWAY, payload, kind="response")
        print(f"<< 0x{sname:08X} retcode={world['m_nRetCode']}")
        print(f"   entered world: {world['nickname']} on map {world['mapid']} "
              f"at ({world['x']},{world['y']})")
        assert world["m_nRetCode"] == 0, "enter-world should succeed"

        print("\nLOGIN FLOW OK — char-select + enter-world round-trip succeeded.")


if __name__ == "__main__":
    main()
