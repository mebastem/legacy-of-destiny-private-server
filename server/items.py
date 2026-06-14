"""
items.py — encode CL_UPDATE_ITEMS (ADD) bodies to put items in the player's bag.

The client's item struct (citem) is native, but the protocol Lua (public/netimpl/
item.txt) documents its wire layout, which we mirror here:
  item = update(u16) uniqueid(u64) itemid(u32) count(u32) bag(u8) status(u8)
         validtime(u32) [+ if EQUIP: xlattr, randxlattr, data as "{}" strings]
CL_UPDATE_ITEMS body = size-prefixed list of updateitem{ uniqueid(u64),
updateType(u16), value } ; for ADD the value is a full item struct.
"""

from __future__ import annotations

from codec import CByteBuffer

# ITEM_POS (where an item lives) — this is the item's `bag` field used for routing
POS_BAG, POS_EQUIP, POS_TREASURE, POS_WARSOUL = 1, 2, 4, 5
UPDATE_ITEM_ADD = 0x1000   # UPDATE_ITEM.ADD


def pos_for(item_type: int, worn: bool = False) -> int:
    """Where an item goes: equipment worn -> EQUIP slot, war-souls -> WARSOUL, else BAG."""
    if item_type == 3 and worn:
        return POS_EQUIP
    if item_type == 7:
        return POS_WARSOUL
    return POS_BAG


def _write_item(b: CByteBuffer, uid, itemid, count, bag, is_equip):
    b.WriteUShort(0)          # update flags
    b.WriteULong(str(uid))    # uniqueid
    b.WriteUInt(itemid)
    b.WriteUInt(count)
    b.WriteByte(bag)
    b.WriteByte(0)            # status
    b.WriteUInt(0)            # validtime
    if is_equip:              # equipment carries dynamic-attr tables (empty for base gear)
        b.WriteString("{}")
        b.WriteString("{}")
        b.WriteString("{}")


def encode_add(uid, itemid, count, bag, is_equip) -> bytes:
    """One CL_UPDATE_ITEMS body that adds a single item to the bag."""
    b = CByteBuffer()
    b.WriteSize(1)                       # m_vecUpdateInfo count
    b.WriteULong(str(uid))               # updateitem.m_nUniqueID
    b.WriteUShort(UPDATE_ITEM_ADD)       # updateitem.m_nUpdateType
    _write_item(b, uid, itemid, count, bag, is_equip)
    return b.ToBytes()
