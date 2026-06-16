#!/usr/bin/env python3
"""
Rewind File Dump Tool (Fully Working Version)

Parses Valheim .rewind binary files created by the Rewind mod and outputs
structured JSON containing all ZDO (Zone Data Object) records with resolved
human-readable names for prefabs and variables.

Name resolution sources:
  - rewind.hexpat  : ImHex pattern file containing ZDOVar and Prefab enums
  - prefabs.csv    : CSV lookup table mapping prefab hashes to names

Usage:
    python3 "rewind_dump - fully working.py" world.rewind prefabs.csv output.json

Output JSON follows the valheim-save-tools schema for cross-tool compatibility.

All dependencies are from the Python standard library — no pip install needed.
"""

import csv
import json
import struct
import sys
import os
import re
import base64

# Unsigned 32-bit hash of the string "items" — used as a ZDO variable key
# for container inventories in the Rewind binary format
ITEMS_HASH = 3356102854

class Reader:
    """Sequential binary reader wrapping a file pointer.

    Provides typed read methods for all primitive types used in the .rewind
    format. All reads are little-endian to match Unity/C# serialization.
    The 'skip' method allows jumping over padding bytes without reading.
    """

    def __init__(self, fp):
        self.fp = fp

    def read(self, n):
        return self.fp.read(n)

    def skip(self, n):
        """Skip n bytes forward (used to jump over padding/reserved fields)."""
        self.fp.seek(n, 1)  # 1 = SEEK_CUR (relative to current position)

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

    def tell(self):
        return self.fp.tell()


def read_leb128(r):
    """Reads a LEB128 (Little-Endian Base 128) variable-length integer.

    Used for string length prefixes in the .rewind format, matching .NET's
    BinaryWriter convention. Each byte contributes 7 data bits; the high
    bit (0x80) signals whether more bytes follow.
    """
    value = 0
    shift = 0
    while True:
        b = r.u8()
        value |= (b & 0x7F) << shift  # Extract 7 data bits, shift into position
        if not (b & 0x80):            # High bit clear = final byte
            break
        shift += 7
    return value


def u32_to_signed(v):
    """Converts an unsigned 32-bit integer to its signed representation.

    The .rewind binary stores hashes as unsigned u32, but Valheim's C# code
    and prefabs.csv use signed int32. This conversion matches C#'s unchecked
    cast behavior: values >= 0x80000000 become negative.
    """
    return struct.unpack("<i", struct.pack("<I", v))[0]


def strip_comments(text):
    """Removes C-style comments from text before parsing enum definitions.

    The rewind.hexpat file is an ImHex pattern file that uses C-style comments.
    We need to strip these before regex-matching enum entries.
    """
    # Strip multi-line comments: /* ... */
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    # Strip single-line comments: // ...
    text = re.sub(r'//.*', '', text)
    return text


def load_hexpat_mappings(hexpat_file):
    """Parses enum definitions from the rewind.hexpat ImHex pattern file.

    Extracts two enum types:
      - ZDOVar: Maps variable hash (u32) → human-readable name (e.g., 's_items')
      - Prefab: Maps prefab hash (u32) → prefab name (e.g., 'piece_chest')

    All hash values are converted from unsigned to signed 32-bit to match
    the convention used by valheim-save-tools and prefabs.csv.

    When duplicate ZDOVar entries exist (e.g., 's_items' and 'items'), the
    version without the 's_' prefix is preferred for cleaner output.
    """
    zdo_vars = {}
    prefabs = {}
    
    if not os.path.exists(hexpat_file):
        return zdo_vars, prefabs

    try:
        with open(hexpat_file, "r", encoding="utf-8") as f:
            content = strip_comments(f.read())
            
        # Match enum blocks like: enum ZDOVar : u32 { ... }
        enum_blocks = re.findall(r'enum\s+(\w+)\s*:\s*\w+\s*\{([^}]+)\}', content)
        for enum_name, enum_body in enum_blocks:
            for line in enum_body.split('\n'):
                line = line.strip()
                if not line:
                    continue
                match = re.match(r'(\w+)\s*=\s*(0x[0-9a-fA-F]+|\d+)', line)
                if match:
                    var_name = match.group(1)
                    var_val_str = match.group(2)
                    var_val = int(var_val_str, 16) if var_val_str.startswith('0x') else int(var_val_str, 10)
                    signed_val = u32_to_signed(var_val)
                    
                    if enum_name == "ZDOVar":
                        # Prefer names without 's_' prefix if we have duplicates (e.g. s_items vs items)
                        existing = zdo_vars.get(signed_val)
                        if not existing or (existing.startswith('s_') and not var_name.startswith('s_')):
                            zdo_vars[signed_val] = var_name
                    elif enum_name == "Prefab":
                        prefabs[signed_val] = var_name
        print(f"Loaded {len(zdo_vars)} variables and {len(prefabs)} prefabs from {hexpat_file}")
    except Exception as e:
        print(f"Warning: Failed to load hexpat enums: {e}")
        
    return zdo_vars, prefabs


def load_prefabs(csv_file):
    """Loads prefab hash → name mappings from a CSV file.

    Expected CSV columns: prefab_hash_signed, prefab_hash_hex, prefab_name
    Returns a dict mapping signed int32 hash → prefab name string.
    """
    prefabs = {}
    if not os.path.exists(csv_file):
        return prefabs
    with open(csv_file, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                signed = int(row["prefab_hash_signed"])
                prefabs[signed] = row["prefab_name"]
            except Exception:
                pass
    return prefabs


def read_vector3(r):
    return {"x": r.f32(), "y": r.f32(), "z": r.f32()}


def read_quaternion(r):
    return {"x": r.f32(), "y": r.f32(), "z": r.f32(), "w": r.f32()}


def read_vector2i(r):
    return {"x": r.s32(), "y": r.s32()}


def read_string_entry(r):
    var_hash = r.u32()
    length = read_leb128(r)
    raw = r.read(length)
    try:
        value = raw.decode("utf-8")
    except:
        value = raw.decode("utf-8", errors="replace")
    return var_hash, value


def dump_rewind(rewind_file, prefab_csv, output_json, hexpat_file="rewind.hexpat"):
    """Main entry point: parses a .rewind binary file into structured JSON.

    Args:
        rewind_file: Path to the .rewind binary file
        prefab_csv:  Path to prefabs.csv for prefab name resolution
        output_json: Path for the output JSON file
        hexpat_file: Path to rewind.hexpat for variable name resolution
    """
    # Build name resolution tables from both sources
    # hexpat provides both ZDO variable names and prefab names
    zdo_vars, hexpat_prefabs = load_hexpat_mappings(hexpat_file)

    # CSV prefabs override hexpat prefabs (CSV is typically more up-to-date)
    prefab_map = hexpat_prefabs.copy()
    csv_prefabs = load_prefabs(prefab_csv)
    prefab_map.update(csv_prefabs)

    results = []

    with open(rewind_file, "rb") as f:
        r = Reader(f)

        # --- File Header ---
        # 20 bytes: magic(u32) + count(u32) + offset(Vector3: 3×float)
        header = {
            "magic": r.u32(),       # Format identifier (observed: 0x1D = 29)
            "count": r.u32(),       # Total number of ZDO records in file
            "offset": read_vector3(r)  # World-space origin offset for relocation
        }

        print("ZDO count:", header["count"])

        for index in range(header["count"]):
            zdo = {}
            current_offset = r.tell()

            try:
                # --- ZDO Fixed Header (87 bytes) ---
                zdo["userID"] = r.u64()          # Steam ID of the owner
                zdo["zdoID"] = r.u32()           # Unique ZDO identifier

                r.skip(6)                         # 6 bytes of reserved padding

                zdo["ownerRevision"] = r.u16()    # Ownership change counter
                zdo["dataRevision"] = r.u32()     # Data change counter
                zdo["persistent"] = bool(r.u8())  # Survives world saves?

                zdo["userKey"] = r.s64()          # Secondary user identifier
                zdo["timeCreated"] = r.s64()      # Creation timestamp (game ticks)
                zdo["zero"] = r.u32()             # Reserved (always 0)

                zdo["type"] = r.s8()              # ZDO type category
                zdo["distant"] = bool(r.u8())     # Rendered at distance?

                # Prefab hash identifies the game object type (chest, wall, etc.)
                # Stored as unsigned in binary, but needs signed conversion for lookups
                prefab_u32 = r.u32()
                prefab_signed = u32_to_signed(prefab_u32)

                zdo["prefabHash"] = prefab_signed
                zdo["prefabName"] = prefab_map.get(prefab_signed, str(prefab_signed))

                zdo["sector"] = read_vector2i(r)    # World sector coordinates
                zdo["position"] = read_vector3(r)   # World position (x, y, z)
                zdo["rotation"] = read_quaternion(r) # Rotation quaternion (x, y, z, w)

                # --- Variable-Length Property Arrays ---
                # Each array is prefixed by a u8 count, followed by that many entries.
                # Each entry has a u32 variable hash key + typed value.
                # The hash key is resolved to a name via zdo_vars lookup.
                floats = {}
                vec3s = {}
                quats = {}
                ints = {}
                longs = {}
                strings = {}
                bytes_dict = {}

                # 1. Floats: u8 count, then [u32 hash, float value] × count
                count = r.u8()
                for _ in range(count):
                    h = r.u32()
                    v = r.f32()
                    key_signed = u32_to_signed(h)
                    key_str = zdo_vars.get(key_signed, str(key_signed))
                    floats[key_str] = v

                # 2. Vector3s: u8 count, then [u32 hash, Vector3(3×float)] × count
                count = r.u8()
                for _ in range(count):
                    h = r.u32()
                    v = read_vector3(r)
                    key_signed = u32_to_signed(h)
                    key_str = zdo_vars.get(key_signed, str(key_signed))
                    vec3s[key_str] = v
                    # Legacy compatibility fallback for scripts expecting it inside strings
                    strings[f"vec3:{key_signed}"] = v

                # 3. Quaternions: u8 count, then [u32 hash, Quaternion(4×float)] × count
                count = r.u8()
                for _ in range(count):
                    h = r.u32()
                    v = read_quaternion(r)
                    key_signed = u32_to_signed(h)
                    key_str = zdo_vars.get(key_signed, str(key_signed))
                    quats[key_str] = v
                    # Legacy compatibility fallback for scripts expecting it inside strings
                    strings[f"quat:{key_signed}"] = v

                # 4. Integers: u8 count, then [u32 hash, s32 value] × count
                count = r.u8()
                for _ in range(count):
                    h = r.u32()
                    v = r.s32()  # Read as signed 32-bit int
                    key_signed = u32_to_signed(h)
                    key_str = zdo_vars.get(key_signed, str(key_signed))
                    ints[key_str] = v

                # 5. Longs: u8 count, then [u32 hash, s64 value] × count
                count = r.u8()
                for _ in range(count):
                    h = r.u32()
                    v = r.s64()  # Read as signed 64-bit int
                    key_signed = u32_to_signed(h)
                    key_str = zdo_vars.get(key_signed, str(key_signed))
                    longs[key_str] = v

                # 6. Strings: u8 count, then [u32 hash, LEB128 length, char[] value] × count
                count = r.u8()
                for _ in range(count):
                    h, v = read_string_entry(r)
                    key_signed = u32_to_signed(h)
                    key_str = zdo_vars.get(key_signed, str(key_signed))
                    strings[key_str] = v

                # 7. Byte arrays: u8 count, then [u32 hash, u32 length, byte[] data] × count
                # CRITICAL: These must be read even if we don't use them, otherwise the
                # file position will be wrong and all subsequent ZDOs will be corrupted.
                # (This was the bug in the "workingish" version that caused it to fail.)
                byte_count = r.u8()
                for _ in range(byte_count):
                    h = r.u32()
                    length = r.u32()
                    v = r.read(length)
                    key_signed = u32_to_signed(h)
                    key_str = zdo_vars.get(key_signed, str(key_signed))
                    # Serialize raw bytes as Base64 for JSON compatibility
                    bytes_dict[key_str] = base64.b64encode(v).decode('utf-8')

                zdo["floats"] = floats
                zdo["vec3s"] = vec3s
                zdo["quats"] = quats
                zdo["ints"] = ints
                zdo["longs"] = longs
                zdo["strings"] = strings
                if bytes_dict:
                    zdo["bytes"] = bytes_dict

                results.append(zdo)

            except Exception as e:
                print(f"Error parsing ZDO index {index} at offset {hex(current_offset)}: {e}")
                raise e

    # Format output to match the valheim-save-tools JSON schema.
    # This ensures compatibility with other tools that expect that format,
    # including this repo's valheim_inventory_exporter.py.
    output_data = {
        "type": "DB",
        "zdoList": {
            "zdos": results
        }
    }

    with open(output_json, "w", encoding="utf-8") as out:
        json.dump(output_data, out, indent=2, ensure_ascii=False)

    print(f"Parsed {len(results)} ZDOs successfully.")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python rewind_dump.py world.rewind prefabs.csv output.json")
        sys.exit(1)

    # Look for rewind.hexpat alongside the script first, then fall back to CWD.
    # This allows the script to find its companion file regardless of where
    # the user runs it from.
    script_dir = os.path.dirname(os.path.abspath(__file__))
    hexpat_path = os.path.join(script_dir, "rewind.hexpat")
    if not os.path.exists(hexpat_path):
        hexpat_path = "rewind.hexpat"

    dump_rewind(
        sys.argv[1],
        sys.argv[2],
        sys.argv[3],
        hexpat_file=hexpat_path
    )