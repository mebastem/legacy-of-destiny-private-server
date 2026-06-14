"""
bootstrap.py — HTTP(S) stub for the client's startup flow.

Endpoints (paths confirmed empirically from the real client via logcat):
  GET  /game_apis/game_code      -> PLAIN base URL; client appends /server/... to it
  GET  <base>/server/load        -> package-info JSON {area,status,...,recom}
                                     (recom = DecodeSerInfo blob = array of servers)  [servermgr.OnHttpPackageInfo]
  GET  <base>/server/serverstype -> bare DecodeSerInfo blob {datas:[group,...]}        [servermgr.OnSerGroupResponse]
  GET  <base>/server/serverdetail? .. -> bare DecodeSerInfo blob = array of servers    [servermgr.OnSerDetailResponse]
  POST /game_api/game_api_ddlog  -> analytics, just 200
  POST /loading/                 -> loading log, just 200
  GET  /api?data=<blob>          -> SDK/account endpoint (TODO: decode + respond)

The client reaches us because the device hosts file maps the dead domains to this
PC and our CA is trusted (see docs/03). Serves HTTPS if cert.pem/key.pem exist.
"""

from __future__ import annotations

import json
import os
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from urllib.parse import urlparse, parse_qs
from serinfo import encode_serinfo, decode_serinfo

HERE = os.path.dirname(__file__)
GATEWAY_HOST = os.environ.get("LOD_GATEWAY_HOST", "192.168.1.5")
GATEWAY_PORT = int(os.environ.get("LOD_GATEWAY_PORT", "7001"))
AREA = os.environ.get("LOD_AREA", "1")
# base the client uses for /server/* — a domain we hold a cert for and redirect via hosts
API_BASE = os.environ.get("LOD_API_BASE", "https://data.ttus.noyagame.com")


def servers():
    """recom / serverdetail array: the actual servers shown + connected to."""
    return [{
        "serverid": "1", "servertypeid": "1",
        "value": "Local Dev", "name": "Local Dev",
        "domain": GATEWAY_HOST, "port": GATEWAY_PORT,
        "status": "1", "flag": "1", "id": "1",
    }]


def server_groups():
    """serverstype response: {datas:[group,...]} (SetServerGroupData reads .datas)."""
    return {"datas": [{
        "servertypeid": "1", "serverGroupName": "Local Dev",
        "value": "Local Dev", "name": "Local Dev", "status": "1",
    }]}


def api_login_response(req: dict) -> dict:
    """Best-effort success for the SDK account-login (protocol 10005). The native
    SDK minted `sessionId` (an sdk_token); we echo it back as the validated session.
    Field names are a guess from PlugInCode.dll strings; refine from logcat."""
    token = req.get("sessionId") or req.get("session") or ""
    return {
        "protocol": req.get("protocol", "10005"),
        "code": 0, "statusCode": 0, "ret": 0, "result": 0, "msg": "ok",
        "sessionId": token, "sessionID": token, "token": token,
        "gameId": req.get("gameId", "1"), "channelId": req.get("channelId", "5"),
    }


def package_info():
    return {
        "area": AREA, "status": 0, "avenger": 0, "star": 1, "fb": 0,
        "recom": encode_serinfo(servers()),
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype="text/plain"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        path = self.path
        print(f"[http] {self.command} {path}  body={body[:120]!r}", flush=True)

        if path.startswith("/api") and "data=" in path:
            try:
                blob = parse_qs(urlparse(path).query).get("data", [""])[0]
                req = decode_serinfo(blob)
                print(f"       /api decoded: {req}", flush=True)
                return self._send(encode_serinfo(api_login_response(req)).encode())
            except Exception as e:
                print(f"       /api decode failed: {e}", flush=True)
                return self._send(b"")
        if "game_code" in path:
            return self._send(API_BASE.encode())                       # plain base URL
        if "serverstype" in path:
            return self._send(encode_serinfo(server_groups()).encode()) # bare blob
        if "serverdetail" in path or "serverlist" in path:
            return self._send(encode_serinfo(servers()).encode())       # bare blob
        if "server/load" in path:
            return self._send(json.dumps(package_info()).encode(), "application/json")
        if "ddlog" in path or "loading" in path:
            return self._send(b"ok")
        # default: package info (also covers /api?data= for now)
        return self._send(json.dumps(package_info()).encode(), "application/json")

    do_GET = _route
    do_POST = _route

    def log_message(self, *a):
        pass


def main():
    cert, key = os.path.join(HERE, "cert.pem"), os.path.join(HERE, "key.pem")
    use_tls = os.path.exists(cert) and os.path.exists(key)
    port = 443 if use_tls else 8080
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    scheme = "http"
    if use_tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    print(f"bootstrap {scheme}://0.0.0.0:{port} -> gateway {GATEWAY_HOST}:{GATEWAY_PORT}, base {API_BASE}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
