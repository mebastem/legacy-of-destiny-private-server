"""
storage.py — persistent character data (JSON file), so a created character and its
level/exp/position survive game restarts. No pre-made accounts: characters only
exist once the player creates them.

Layout: { account(sessionID): [ {playerid, nickname, sex, profession, camp,
                                 level, exp, mapid, x, y, status}, ... ] }
"""

from __future__ import annotations

import json
import os
import threading

PATH = os.path.join(os.path.dirname(__file__), "accounts.json")
_lock = threading.RLock()


def _load() -> dict:
    if os.path.exists(PATH):
        try:
            with open(PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


_data = _load()


def save():
    with _lock:
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, PATH)


def get_characters(account: str) -> list:
    return _data.get(account, [])


def find_by_pid(pid) -> dict | None:
    pid = str(pid)
    for chars in _data.values():
        for c in chars:
            if str(c["playerid"]) == pid:
                return c
    return None


def add_character(account: str, char: dict):
    with _lock:
        _data.setdefault(account, []).append(char)
        save()


def next_playerid() -> str:
    mx = 100000000000000
    for chars in _data.values():
        for c in chars:
            mx = max(mx, int(c["playerid"]))
    return str(mx + 1)
