#!/usr/bin/env python3
"""
Rewind File Dump Tool (Earlier Version — Superseded)

This is an earlier, simpler version of the .rewind binary parser.
It has been superseded by 'rewind_dump - fully working.py' which adds:
  - ZDO variable name resolution via rewind.hexpat
  - Prefab name resolution via hexpat enums (in addition to CSV)
  - Proper byte array consumption (fixing parser desync)
  - Signed integer reads for ints and longs
  - Base64 encoding for byte array fields
  - Error handling with offset reporting

Kept for reference and development history.

Usage:
    python3 "rewind_dump - workingish.py" world.rewind prefabs.csv output.json
"""

import csv
import json
import struct
import sys


# Unsigned 32-bit hash of the string "items" — used as a ZDO variable key.
# Not actively used in this script, but defined for reference/future use.
ITEMS_HASH = 3356102854


class Reader:
    """Sequential binary reader wrapping a file pointer.

    Provides typed read methods for all primitive types used in the .rewind
    format. All reads use little-endian byte order (Unity/C# convention).
    """

    def __init__(self, fp):
        self.fp = fp

    def read(self, n):
        return self.fp.read(n)

    def skip(self, n):
        """Skip n bytes forward (relative seek)."""
        self.fp.seek(n, 1)

    def u8(self):
        return struct.unpack("<B", self.read(1))[0]

    def s8(self):
        return struct.unpack("<b", self.read(1))[0]

    def u16(self):
        return struct.unpack("<H", self.read(2))[0]

    def u32(self):
        return struct.unpack("<I", self.read(4))[0]

    def s32(self):
        return struct.unpack("<i", self.read(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.read(8))[0]

    def s64(self):
        return struct.unpack("<q", self.read(8))[0]

    def f32(self):
        return struct.unpack("<f", self.read(4))[0]


def read_leb128(r):
    """Reads a LEB128 variable-length integer (used for string length prefixes)."""
    value = 0
    shift = 0

    while True:
        b = r.u8()

        value |= (b & 0x7F) << shift  # Take lower 7 bits

        if not (b & 0x80):  # High bit clear = last byte
            break

        shift += 7

    return value


def load_prefabs(csv_file):
    """Loads prefab hash -> name mappings from prefabs.csv."""
    prefabs = {}

    with open(csv_file, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            try:
                signed = int(row["prefab_hash_signed"])
                prefabs[signed] = row["prefab_name"]
            except Exception:
                pass

    return prefabs


def u32_to_signed(v):
    """Reinterprets an unsigned 32-bit integer as signed (matching C# int32)."""
    return struct.unpack("<i", struct.pack("<I", v))[0]


def read_vector3(r):
    return {
        "x": r.f32(),
        "y": r.f32(),
        "z": r.f32()
    }


def read_quaternion(r):
    return {
        "x": r.f32(),
        "y": r.f32(),
        "z": r.f32(),
        "w": r.f32()
    }


def read_vector2i(r):
    return {
        "x": r.s32(),
        "y": r.s32()
    }


def read_string_entry(r):
    var_hash = r.u32()

    length = read_leb128(r)

    raw = r.read(length)

    try:
        value = raw.decode("utf-8")
    except:
        value = raw.decode("utf-8", errors="replace")

    return var_hash, value


def dump_rewind(rewind_file, prefab_csv, output_json):
    """Parses a .rewind file and outputs a flat JSON list of ZDO records.

    NOTE: This version does NOT resolve ZDO variable names (keys are raw hashes)
    and does NOT consume byte array data, which can cause parser desync on
    files containing byte array properties. Use the 'fully working' version instead.
    """

    prefab_map = load_prefabs(prefab_csv)

    results = []

    with open(rewind_file, "rb") as f:

        r = Reader(f)

        header = {
            "magic": r.u32(),
            "count": r.u32(),
            "offset": read_vector3(r)
        }

        print("ZDO count:", header["count"])

        for index in range(header["count"]):

            zdo = {}

            zdo["userID"] = r.u64()
            zdo["zdoID"] = r.u32()

            r.skip(6)

            zdo["ownerRevision"] = r.u16()
            zdo["dataRevision"] = r.u32()

            zdo["persistent"] = bool(r.u8())

            zdo["userKey"] = r.s64()
            zdo["timeCreated"] = r.s64()

            zdo["zero"] = r.u32()

            zdo["type"] = r.s8()
            zdo["distant"] = bool(r.u8())

            prefab_u32 = r.u32()
            prefab_signed = u32_to_signed(prefab_u32)

            zdo["prefabHash"] = prefab_signed
            zdo["prefabName"] = prefab_map.get(prefab_signed)

            # Spatial data
            zdo["sector"] = read_vector2i(r)
            zdo["position"] = read_vector3(r)

            zdo["rotation"] = read_quaternion(r)

            # --- Property arrays ---
            # Keys are stored as raw hash strings (no name resolution in this version)
            floats = {}
            ints = {}
            longs = {}
            strings = {}

            # Floats
            count = r.u8()
            for _ in range(count):
                h = r.u32()
                v = r.f32()
                floats[str(u32_to_signed(h))] = v

            # Vector3s (stored under strings with "vec3:" prefix for legacy reasons)
            count = r.u8()
            for _ in range(count):
                h = r.u32()
                v = read_vector3(r)
                strings[f"vec3:{u32_to_signed(h)}"] = v

            # Quaternions (stored under strings with "quat:" prefix for legacy reasons)
            count = r.u8()
            for _ in range(count):
                h = r.u32()
                v = read_quaternion(r)
                strings[f"quat:{u32_to_signed(h)}"] = v

            # Integers (BUG: reads as unsigned u32 instead of signed s32)
            count = r.u8()
            for _ in range(count):
                h = r.u32()
                v = r.u32()
                ints[str(u32_to_signed(h))] = v

            # Longs (BUG: reads as unsigned u64 instead of signed s64)
            count = r.u8()
            for _ in range(count):
                h = r.u32()
                v = r.u64()
                longs[str(u32_to_signed(h))] = v

            # Strings
            count = r.u8()
            for _ in range(count):
                h, v = read_string_entry(r)
                strings[str(u32_to_signed(h))] = v

            # BUG: Byte array count is read but data is NOT consumed.
            # If any ZDO has byte arrays, the file pointer will be wrong
            # and all subsequent ZDOs will fail to parse correctly.
            # This is fixed in the 'fully working' version.
            byte_count = r.u8()

            if byte_count:
                zdo["byteCount"] = byte_count

            zdo["floats"] = floats
            zdo["ints"] = ints
            zdo["longs"] = longs
            zdo["strings"] = strings

            results.append(zdo)

    with open(output_json, "w", encoding="utf-8") as out:
        json.dump(results, out, indent=2, ensure_ascii=False)

    print("Wrote", len(results), "ZDOs")


if __name__ == "__main__":

    if len(sys.argv) != 4:
        print(
            "Usage: python rewind_dump.py "
            "world.rewind prefabs.csv output.json"
        )
        sys.exit(1)

    dump_rewind(
        sys.argv[1],
        sys.argv[2],
        sys.argv[3]
    )