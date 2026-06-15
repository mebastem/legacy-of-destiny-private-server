"""
ranking.py — encode GW_QUERY_RANKING responses.

The client picks the per-entry struct from the ranking name (gw_query_ranking.txt
create_RANKING_INFO): most boards use RANKING_NORMAL_INFO, a few use a wider struct.
We mirror that exactly so any tab decodes cleanly. Response layout (gw_query_ranking):
  u32 retcode, str name, u8 page, u8 index, <my_info struct>,
  size + [<info struct>...], u8 maxPage
"""

from __future__ import annotations

from codec import CByteBuffer

# ranking name -> per-entry struct kind. Anything not listed uses NORMAL.
_KIND = {
    "arena_ranking": "ARENA",
    "pet_ranking": "PET",
    "charm_ranking": "CHARM",
    "recharge_ranking": "ACT", "consume_ranking": "ACT",
    "sevendaysconsume_ranking": "ACT", "kaifu_recharge_ranking": "ACT",
    "guildscore_ranking": "GUILD_SCORE",
    "guildkill_ranking": "GUILD_KILL",
}


def kind_of(name: str) -> str:
    return _KIND.get(name, "NORMAL")


def _write_info(b: CByteBuffer, kind: str, d: dict):
    """Write one RANKING_*_INFO struct, pulling fields from d (zeros/empties default)."""
    pid = str(d.get("playerid", 0))
    power = int(d.get("power", 0))
    name = d.get("name", "")
    level = int(d.get("level", 0))
    vip = int(d.get("vip", 0))
    guild = d.get("guild", "")
    if kind == "ARENA":
        b.WriteULong(pid); b.WriteUInt(power); b.WriteString(name)
        b.WriteByte(int(d.get("profession", 0))); b.WriteUShort(level)
        b.WriteUInt(int(d.get("figure1", 0))); b.WriteUInt(int(d.get("figure2", 0)))
    elif kind == "PET":
        b.WriteULong(pid); b.WriteUInt(power); b.WriteString(name)
        b.WriteUShort(level); b.WriteByte(vip); b.WriteString(guild)
        b.WriteString(d.get("pet", ""))
    elif kind == "CHARM":
        b.WriteULong(pid); b.WriteUInt(power); b.WriteString(name)
        b.WriteUShort(level); b.WriteByte(vip); b.WriteString(guild)
        b.WriteByte(int(d.get("marry", 2)))           # 1 married / 2 single
    elif kind == "ACT":
        b.WriteULong(pid); b.WriteUInt(int(d.get("value", 0))); b.WriteString(name)
        b.WriteUShort(level); b.WriteByte(vip); b.WriteString(guild)
        b.WriteByte(int(d.get("profession", 0)))
    elif kind == "GUILD_SCORE":
        b.WriteULong(str(d.get("guildid", 0))); b.WriteUInt(int(d.get("score", 0)))
        b.WriteString(guild); b.WriteString(name); b.WriteUShort(level)
    elif kind == "GUILD_KILL":
        b.WriteULong(pid); b.WriteUInt(power); b.WriteString(name)
        b.WriteUShort(level); b.WriteByte(vip); b.WriteString(guild)
        b.WriteUInt(int(d.get("score", 0)))
    else:  # NORMAL
        b.WriteULong(pid); b.WriteUInt(power); b.WriteString(name)
        b.WriteUShort(level); b.WriteByte(vip); b.WriteString(guild)


def encode(name: str, page: int, index: int, my_info: dict,
           infos: list[dict], max_page: int) -> bytes:
    kind = kind_of(name)
    b = CByteBuffer()
    b.WriteUInt(0)                 # m_nRetCode
    b.WriteString(name)            # m_strRankingName
    b.WriteByte(page)              # m_nPage
    b.WriteByte(index)             # m_nIndex (requester's own rank)
    _write_info(b, kind, my_info)  # m_info (requester's entry)
    b.WriteSize(len(infos))        # m_vecInfos
    for d in infos:
        _write_info(b, kind, d)
    b.WriteByte(max_page)          # m_nMaxPage
    return b.ToBytes()
