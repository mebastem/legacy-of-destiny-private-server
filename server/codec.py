"""
codec.py — byte-exact Python port of the client's CByteBuffer.

This mirrors ExportedProject/Assets/Scripts/Assembly-CSharp/CByteBuffer.cs
method-for-method, including its quirks:

  * integers are little-endian (C# BinaryWriter default)
  * float / double are written BIG-endian (the client reverses the bytes)
  * strings are length-prefixed UTF-8: 1 length byte, or 0xFF + uint16 if len >= 255
  * "size" is a varint: 1 byte (<255), else 0xFF + uint16 (<65535), else 0xFF 0xFF 0xFF + uint32
  * 64-bit values are passed as *strings* (the Lua side is Lua 5.1, whose numbers
    are doubles and cannot hold 64-bit ints — the client marshals them via
    Convert.ToInt64(string)). We keep the string contract so the original Lua
    protocol files run unchanged.

The method names intentionally match the C# class (ReadByte/WriteByte/...) so a
single instance can be handed straight to the embedded Lua runtime and the
unmodified client protocol scripts can call it.
"""

from __future__ import annotations

import struct


class CByteBuffer:
    def __init__(self, data: bytes | None = None):
        self._buf = bytearray(data) if data is not None else bytearray()
        self._pos = 0  # read cursor

    # ---- lifecycle (match C# names used by the protocol/runtime) ----
    def Clear(self):
        self._buf = bytearray()
        self._pos = 0

    def Reset(self):
        self._pos = 0

    def ToBytes(self) -> bytes:
        return bytes(self._buf)

    def GetSize(self) -> int:
        return len(self._buf)

    @property
    def remaining(self) -> int:
        return len(self._buf) - self._pos

    # ---- low-level read helpers ----
    def _read(self, n: int) -> bytes:
        if self._pos + n > len(self._buf):
            raise EOFError(f"need {n} bytes, have {self.remaining}")
        b = bytes(self._buf[self._pos:self._pos + n])
        self._pos += n
        return b

    # ---- writes ----
    def WriteByte(self, v):       self._buf += struct.pack("<B", int(v) & 0xFF)
    def WriteSByte(self, v):      self._buf += struct.pack("<b", int(v))
    def WriteShort(self, v):      self._buf += struct.pack("<h", int(v))
    def WriteUShort(self, v):     self._buf += struct.pack("<H", int(v) & 0xFFFF)
    def WriteInt(self, v):        self._buf += struct.pack("<i", int(v))
    def WriteUInt(self, v):       self._buf += struct.pack("<I", int(v) & 0xFFFFFFFF)
    def WriteLong(self, v):       self._buf += struct.pack("<q", int(v))   # v is a string from Lua
    def WriteULong(self, v):      self._buf += struct.pack("<Q", int(v))   # v is a string from Lua

    def WriteFloat(self, v):
        # client does BitConverter.GetBytes(v) then Array.Reverse -> big-endian on the wire
        self._buf += struct.pack(">f", float(v))

    def WriteDouble(self, v):
        self._buf += struct.pack(">d", float(v))

    def WriteString(self, v):
        data = (v if isinstance(v, str) else str(v)).encode("utf-8")
        if len(data) < 255:
            self.WriteByte(len(data))
        else:
            self.WriteByte(255)
            self.WriteUShort(len(data))
        self._buf += data

    def WriteBytes(self, v: bytes):
        self._buf += bytes(v)

    def WriteArray(self, pType, tbl):
        """Mirror of C# CByteBuffer.WriteArray: size-prefixed typed array. `tbl`
        is a 1-based Lua table (from the protocol scripts). pType: 1=ulong 2=uint
        3=ushort 4=byte 5=string 6=long 7=int 8=short 9=sbyte 10=size."""
        items = []
        i = 1
        while True:
            v = tbl[i]
            if v is None:
                break
            items.append(v)
            i += 1
        self.WriteSize(len(items))
        writers = {
            1: lambda v: self._buf.__iadd__(struct.pack("<Q", int(v))),
            2: lambda v: self.WriteUInt(int(v)),
            3: lambda v: self.WriteUShort(int(v)),
            4: lambda v: self.WriteByte(int(v)),
            5: lambda v: self.WriteString(v),
            6: lambda v: self._buf.__iadd__(struct.pack("<q", int(v))),
            7: lambda v: self.WriteInt(int(v)),
            8: lambda v: self.WriteShort(int(v)),
            9: lambda v: self.WriteSByte(int(v)),
            10: lambda v: self.WriteSize(int(v)),
        }
        w = writers[int(pType)]
        for v in items:
            w(v)

    def WriteSize(self, n) -> int:
        n = int(n)
        if n < 255:
            self.WriteByte(n)
            return 1
        if n < 65535:
            self.WriteByte(255)
            self.WriteUShort(n)
            return 3
        self.WriteBytes(b"\xff\xff\xff")
        self.WriteUInt(n)
        return 7

    # ---- reads ----
    def ReadByte(self) -> int:    return struct.unpack("<B", self._read(1))[0]
    def ReadSByte(self) -> int:   return struct.unpack("<b", self._read(1))[0]
    def ReadShort(self) -> int:   return struct.unpack("<h", self._read(2))[0]
    def ReadUShort(self) -> int:  return struct.unpack("<H", self._read(2))[0]
    def ReadInt(self) -> int:     return struct.unpack("<i", self._read(4))[0]
    def ReadUInt(self) -> int:    return struct.unpack("<I", self._read(4))[0]
    def ReadLong(self) -> str:    return str(struct.unpack("<q", self._read(8))[0])   # client returns string
    def ReadULong(self) -> str:   return str(struct.unpack("<Q", self._read(8))[0])

    def ReadFloat(self) -> float:  return struct.unpack(">f", self._read(4))[0]
    def ReadDouble(self) -> float: return struct.unpack(">d", self._read(8))[0]

    def ReadString(self) -> str:
        n = self.ReadByte()
        if n == 255:
            n = self.ReadUShort()
        return self._read(n).decode("utf-8")

    def ReadBytes(self, n: int) -> bytes:
        return self._read(n)

    def ReadSize(self) -> int:
        n = self.ReadByte()
        if n == 255:
            n = self.ReadUShort()
            if n == 65535:
                n = self.ReadUInt()
        return n


# --- self test: run `python codec.py` ---
if __name__ == "__main__":
    b = CByteBuffer()
    b.WriteUInt(0)            # serialno
    b.WriteUInt(0x00010001)   # servantname GBM_LOGIN_GAME
    b.WriteShort(7)           # size (i16)
    assert b.ToBytes() == bytes([0,0,0,0, 1,0,1,0, 7,0]), b.ToBytes().hex()

    b = CByteBuffer()
    b.WriteFloat(1.0)
    assert b.ToBytes() == bytes([0x3f, 0x80, 0x00, 0x00]), "float must be big-endian"

    b = CByteBuffer()
    b.WriteString("hi"); b.WriteSize(300); b.WriteLong("-5"); b.WriteULong("18446744073709551615")
    r = CByteBuffer(b.ToBytes())
    assert r.ReadString() == "hi"
    assert r.ReadSize() == 300
    assert r.ReadLong() == "-5"
    assert r.ReadULong() == "18446744073709551615"

    # round-trip the real login-response struct shape
    b = CByteBuffer()
    b.WriteUInt(0)                 # m_nRetCode
    b.WriteULong("123456789012")   # m_nLastLoginPlayerID (u64 as string)
    b.WriteSize(1)                 # one player
    b.WriteULong("999"); b.WriteUShort(40); b.WriteByte(1); b.WriteByte(2); b.WriteByte(0)
    b.WriteString("Hero"); b.WriteByte(0)
    r = CByteBuffer(b.ToBytes())
    assert r.ReadUInt() == 0 and r.ReadULong() == "123456789012" and r.ReadSize() == 1
    assert r.ReadULong() == "999" and r.ReadUShort() == 40
    assert r.ReadByte() == 1 and r.ReadByte() == 2 and r.ReadByte() == 0
    assert r.ReadString() == "Hero" and r.ReadByte() == 0
    assert r.remaining == 0
    print("codec self-test OK")
