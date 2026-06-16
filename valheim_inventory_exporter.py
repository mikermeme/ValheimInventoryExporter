#!/usr/bin/env python3
"""
Valheim Inventory Exporter

Extracts all item inventories from container objects (chests, barrels, tombstones,
etc.) in a Valheim world save and writes them to a flat CSV file.

Supports two input formats:
  - .db   : Native Valheim world save (requires Java + valheim-save-tools.jar
            to convert to JSON first)
  - .json : Pre-exported JSON from valheim-save-tools

Designed for memory efficiency: large JSON files (10GB+) are streamed line-by-line
rather than loaded entirely into RAM.

Usage:
    python3 valheim_inventory_exporter.py myworld.db
    python3 valheim_inventory_exporter.py myworld.json -o output.csv

All dependencies are from the Python standard library — no pip install needed.
"""

import argparse
import base64
import csv
import json
import os
import shutil
import struct
import subprocess
import sys
from io import BytesIO
from pathlib import Path

# --- VALHEIM STABLE HASHES & UTILITIES ---
# Valheim uses a custom deterministic hashing function (a djb2 variant) to convert
# string property names into 32-bit signed integers. These hashes serve as keys in
# the ZDO property maps. We pre-compute common hashes below so we can look up
# properties by hash when the JSON exporter hasn't resolved names.

def get_stable_hash_code(s: str) -> int:
    """Simulates Valheim's 32-bit signed integer GetStableHashCode algorithm.

    This is a djb2 variant: hash = ((hash << 5) + hash) ^ char
    The result is clamped to 32 bits and then reinterpreted as a signed integer,
    matching C#'s int32 overflow behavior.
    """
    hash_val = 5381  # djb2 initial seed value
    for char in s:
        # ((hash << 5) + hash) is equivalent to hash * 33, then XOR with char
        hash_val = ((hash_val << 5) + hash_val) ^ ord(char)
        hash_val = (hash_val & 0xFFFFFFFF)  # Mask to 32 bits (simulate uint32 overflow)
    # Convert from unsigned 32-bit to signed 32-bit (Python doesn't overflow natively)
    if hash_val >= 0x80000000:
        hash_val -= 0x100000000
    return hash_val

# Pre-calculate common property hashes as strings, since JSON keys are always strings.
# valheim-save-tools may output either resolved names ("items") or raw hash integers
# ("179721187") depending on version/config, so we need both forms for lookups.
HASH_ITEMS = str(get_stable_hash_code("items"))          # '179721187'
HASH_CREATOR = str(get_stable_hash_code("creator"))      # '-374753447'
HASH_HEALTH = str(get_stable_hash_code("health"))        # '1581283705'
HASH_TAG = str(get_stable_hash_code("tag"))              # '193421815'
HASH_TEXT = str(get_stable_hash_code("text"))            # '2087956376'
HASH_NAME = str(get_stable_hash_code("name"))            # '2087876002'
HASH_CUSTOM_NAME = str(get_stable_hash_code("custom_name")) # '-250281458'


def get_zdo_value(zdo, category, field_name, hash_str):
    """Retrieves a value from a ZDO dictionary, supporting both resolved names

    and unresolved stable hash integers/strings.

    valheim-save-tools can output ZDO properties in two different layouts:
      1. Resolved:   {"stringsByName": {"items": "..."}}   — uses human-readable keys
      2. Unresolved: {"strings": {"179721187": "..."}}     — uses hash integer keys

    This function checks both layouts, trying the resolved name first for speed,
    then falling back to hash-based lookup (as both string and int keys, since
    JSON parsers may deserialize numeric keys as either type).
    """
    # First: try the resolved "<category>ByName" dict (e.g., "stringsByName")
    by_name = zdo.get(f"{category}ByName")
    if by_name and field_name in by_name:
        return by_name[field_name]
    
    # Fallback: try the raw "<category>" dict with hash-based keys
    normal = zdo.get(category)
    if normal:
        # Try hash as a string key (most common in JSON)
        if hash_str in normal:
            return normal[hash_str]
        # Try hash as an integer key (some JSON parsers auto-convert numeric keys)
        try:
            hash_int = int(hash_str)
            if hash_int in normal:
                return normal[hash_int]
        except ValueError:
            pass
            
    return None


# --- BINARY INVENTORY BLOB PARSERS ---
# These functions decode the base64 inventory blob stored in the "items" string
# property of container ZDOs. The blob uses .NET's BinaryWriter/BinaryReader
# serialization conventions (little-endian, 7-bit encoded string lengths).

def read_7bit_int(f):
    """Reads a LEB128 (7-bit encoded) integer from a binary stream.

    This matches .NET's BinaryReader.Read7BitEncodedInt() format, which is used
    to prefix string lengths. Each byte contributes 7 bits of the value; the
    high bit (0x80) indicates whether more bytes follow.
    """
    result = 0
    shift = 0
    while True:
        b = f.read(1)
        if not b:
            raise EOFError("Unexpected end of file while reading 7-bit encoded integer")
        b = b[0]
        result |= (b & 0x7F) << shift  # Take lower 7 bits and shift into position
        if (b & 0x80) == 0:            # High bit clear = this is the last byte
            return result
        shift += 7


def read_string(f):
    """Reads a .NET BinaryReader-style length-prefixed string.

    Format: LEB128-encoded byte length, followed by that many bytes of UTF-8 text.
    """
    length = read_7bit_int(f)
    if length == 0:
        return ""
    return f.read(length).decode("utf-8", errors="replace")


def read_bool(f):
    return struct.unpack("<?", f.read(1))[0]


def read_int(f):
    return struct.unpack("<i", f.read(4))[0]


def read_long(f):
    return struct.unpack("<q", f.read(8))[0]


def read_float(f):
    return struct.unpack("<f", f.read(4))[0]


def read_vector2i(f):
    return (read_int(f), read_int(f))


def parse_inventory(items_field):
    """Parses a base64-encoded inventory blob from a container ZDO.

    The blob format (after base64 decoding) is:
      - s32 version       : Inventory serialization version
      - s32 itemCount     : Number of items
      - Item[itemCount]   : Sequential item records (see field order below)

    Each item record contains: prefab name, stack, durability, grid position,
    equipped flag, quality, variant, crafter info, custom mod data, world level,
    and picked-up flag — matching Valheim's Inventory.Save() serialization order.
    """
    try:
        data = base64.b64decode(items_field)
    except Exception:
        return []

    f = BytesIO(data)
    try:
        version = read_int(f)       # Inventory format version (for forward compatibility)
        item_count = read_int(f)    # How many items are stored in this container
    except (EOFError, struct.error):
        return []

    items = []
    for _ in range(item_count):
        try:
            # Fields are read in the exact order Valheim's Inventory.Save() writes them
            prefab = read_string(f)         # Item type identifier (e.g., "SwordIron")
            stack = read_int(f)             # How many in the stack (1 for non-stackable)
            durability = read_float(f)      # Current durability (100.0 = full for most items)
            x, y = read_vector2i(f)         # Grid slot position within the container
            equipped = read_bool(f)         # Equipped flag (relevant for player inventories)
            quality = read_int(f)           # Upgrade level (1 = base, higher = upgraded)
            variant = read_int(f)           # Visual variant index (e.g., shield paint)
            crafter_id = read_long(f)       # Steam ID of whoever crafted this item
            crafter_name = read_string(f)   # Display name of the crafter
            custom_count = read_int(f)      # Number of custom key-value data pairs

            # Custom data stores mod-specific info (e.g., Epic Loot enchantments,
            # engravings, or other mod-injected item properties)
            custom_data = {}
            for _ in range(custom_count):
                key = read_string(f)
                value = read_string(f)
                custom_data[key] = value

            world_level = read_int(f)       # World difficulty level when item was created
            picked_up = read_bool(f)        # Whether the item has been picked up before

            items.append({
                "prefab": prefab,
                "stack": stack,
                "durability": durability,
                "x": x,
                "y": y,
                "equipped": equipped,
                "quality": quality,
                "variant": variant,
                "crafter_id": crafter_id,
                "crafter_name": crafter_name,
                "world_level": world_level,
                "picked_up": picked_up,
                "custom_data_count": custom_count,
                "custom_data": custom_data,
            })
        except (EOFError, struct.error):
            # Gracefully handle truncated or corrupt item data — return what we got
            break

    return items


# --- MEMORY-EFFICIENT LOW-LEVEL STREAMING ENGINE ---
# Large Valheim worlds can produce JSON files exceeding 10GB. Loading these
# entirely into memory with json.load() would require enormous RAM. Instead,
# the streaming engine below reads the JSON line-by-line, detecting ZDO object
# boundaries by looking for the '"persistent"' property that starts every ZDO.
# This allows processing millions of ZDOs with roughly constant memory usage.

def is_pretty_printed(file_path: Path) -> bool:
    """Checks the first 64KB of the JSON to see if it is formatted/pretty-printed

    with newlines, which allows line-by-line streaming.

    Pretty-printed (indented) JSON has one property per line, making it possible
    to detect ZDO boundaries without a full JSON parser. Compact (minified) JSON
    has everything on one line and must be loaded entirely into memory.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            chunk = f.read(65536)  # Sample the first 64KB
            return chunk.count("\n") > 50  # Heuristic: >50 newlines = pretty-printed
    except Exception:
        return False


def stream_zdos_file(file_path: Path):
    """Memory-efficient streaming parser that reads line-by-line.

    Detects boundaries of ZDO objects and filters for inventory elements
    without loading the 10GB dataset into memory.

    Strategy:
      1. Skip lines until we find the "zdoList" key (the array of ZDO objects)
      2. Accumulate lines into a buffer for the current ZDO object
      3. When we see '"persistent"' (always the first property of each ZDO),
         we know a new ZDO is starting — finalize and yield the previous one
      4. Only parse (json.loads) ZDOs that contain '"items"' to avoid
         wasting CPU on the 99%+ of ZDOs that aren't containers
    """
    current_zdo_lines = []  # Accumulator: lines belonging to the current ZDO
    in_zdo_list = False     # Flag: have we passed the "zdoList" key yet?
    
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            # Phase 1: Skip everything before the zdoList array begins
            if not in_zdo_list:
                if "zdoList" in line:
                    in_zdo_list = True
                continue
                
            # Phase 2: Detect ZDO boundaries using the "persistent" property.
            # Every ZDO starts with the "persistent" property, so seeing it means
            # we've crossed from one ZDO to the next.
            if '"persistent"' in line:
                if current_zdo_lines:
                    # The last accumulated line is the boundary between ZDOs
                    # (typically "  {" or "  }, {")
                    boundary_line = current_zdo_lines.pop()
                    
                    # Handle combined close/open on one line: "  }, {"
                    # Split it so the closing brace goes to the previous ZDO
                    # and the opening brace starts the next one
                    if "}" in boundary_line and "{" in boundary_line:
                        current_zdo_lines.append("  }")
                        next_zdo_start = "  {"
                    else:
                        next_zdo_start = boundary_line
                    
                    # Reassemble the complete ZDO as a standalone JSON object
                    prev_zdo_str = "\n".join(current_zdo_lines).strip()
                    
                    # Clean up JSON boundaries: remove trailing commas,
                    # ensure we have matching braces for json.loads()
                    if prev_zdo_str.endswith(","):
                        prev_zdo_str = prev_zdo_str[:-1].strip()
                    if not prev_zdo_str.startswith("{"):
                        prev_zdo_str = "{" + prev_zdo_str
                    if not prev_zdo_str.endswith("}"):
                        prev_zdo_str = prev_zdo_str + "}"
                    
                    # Fast pre-filter: only parse ZDOs that mention "items" (by name
                    # or hash). This is a cheap string search that avoids json.loads()
                    # on the vast majority of ZDOs that aren't containers.
                    if '"items"' in prev_zdo_str or HASH_ITEMS in prev_zdo_str:
                        try:
                            yield json.loads(prev_zdo_str)
                        except Exception:
                            pass # Silently skip malformed blocks
                    
                    # Start accumulating lines for the next ZDO
                    current_zdo_lines = [next_zdo_start]
                    
            current_zdo_lines.append(line)
            
        # Process the final ZDO remaining in the stream after EOF
        if current_zdo_lines:
            prev_zdo_str = "\n".join(current_zdo_lines).strip()
            # Strip the JSON array footer (closing ] and }) that follows the last ZDO
            if "]" in prev_zdo_str:
                prev_zdo_str = prev_zdo_str.split("]")[0].strip()
            if prev_zdo_str.endswith(","):
                prev_zdo_str = prev_zdo_str[:-1].strip()
            if not prev_zdo_str.startswith("{"):
                prev_zdo_str = "{" + prev_zdo_str
            if not prev_zdo_str.endswith("}"):
                prev_zdo_str = prev_zdo_str + "}"
                
            if '"items"' in prev_zdo_str or HASH_ITEMS in prev_zdo_str:
                try:
                    yield json.loads(prev_zdo_str)
                except Exception:
                    pass


def iterate_zdos(file_path: Path):
    """Determines whether to stream or load the JSON file in memory.

    Pretty-printed JSON can be streamed line-by-line for low memory usage.
    Compact (minified) JSON must be loaded entirely via json.load(), which
    may require significant RAM for large worlds.
    """
    if is_pretty_printed(file_path):
        print("-> Pretty-printed JSON detected. Streaming game objects line-by-line to protect RAM...")
        yield from stream_zdos_file(file_path)
    else:
        print("-> Compact JSON detected. Loading the entire file into memory (Warning: High RAM usage)...")
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
            for zdo in data.get("zdoList", []):
                yield zdo


# --- MAIN PIPELINE EXECUTIVE ---
# Pipeline: Input (.db or .json) → Convert (if .db) → Stream ZDOs → Filter
# containers → Decode inventory blobs → Write CSV

def main():
    parser = argparse.ArgumentParser(
        description="Extract and catalog all items inside Valheim save file containers into a single CSV."
    )
    parser.add_argument(
        "input", 
        help="Path to the Valheim world save file (.db) OR its JSON export from valheim-save-tools (.json)"
    )
    parser.add_argument(
        "-o", "--output", 
        help="Output CSV path. Defaults to <input_name>_items.csv"
    )
    parser.add_argument(
        "--jar", 
        default="valheim-save-tools.jar",
        help="Path to the 'valheim-save-tools.jar' CLI utility. Defaults to search in current directory."
    )
    parser.add_argument(
        "--keep-json", 
        action="store_true",
        help="If converting a .db, keep the intermediate generated .json file."
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: Input file '{input_path}' does not exist.", file=sys.stderr)
        sys.exit(1)

    json_path = None
    is_temp_json = False

    # --- Step 1: Convert .db → .json if needed ---
    # Valheim .db files are a proprietary binary format. We use the third-party
    # Java tool "valheim-save-tools" to convert them to JSON first.
    if input_path.suffix.lower() == ".db":
        if not shutil.which("java"):
            print("Error: 'java' is not installed or not in system PATH. Required to parse .db archives.", file=sys.stderr)
            sys.exit(1)

        jar_path = Path(args.jar)
        if not jar_path.exists():
            print(f"Error: '{jar_path}' tool was not found.", file=sys.stderr)
            print("Please download it from: https://github.com/Kakoen/valheim-save-tools/releases", file=sys.stderr)
            print("and place it in this folder, or specify its location with --jar <path>.", file=sys.stderr)
            sys.exit(1)

        temp_json_path = input_path.with_suffix(".json")
        print(f"-> Converting '{input_path.name}' to raw objects JSON via valheim-save-tools...")
        cmd = ["java", "-jar", str(jar_path), str(input_path), str(temp_json_path)]
        
        try:
            subprocess.run(cmd, check=True)
            print("-> Successfully generated intermediate JSON.")
            json_path = temp_json_path
            is_temp_json = True
        except subprocess.CalledProcessError as e:
            print(f"Error: Failed converting .db file: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        json_path = input_path

    # Determine Output Name
    output_csv = Path(args.output) if args.output else input_path.with_name(f"{input_path.stem}_items.csv")

    # --- Step 2: Extract and catalog items from all containers ---
    # Stream through every ZDO, find ones with inventory blobs, decode them,
    # and write each item as a row in the output CSV.
    print("-> Streaming JSON and processing container objects...")
    
    csv_headers = [
        "container_prefab",
        "container_prefab_name",
        "container_x",
        "container_y",
        "container_z",
        "container_sector_x",
        "container_sector_y",
        "container_creator_id",
        "container_custom_name",
        "item_prefab",
        "item_stack",
        "item_durability",
        "item_grid_x",
        "item_grid_y",
        "item_quality",
        "item_variant",
        "item_crafter_id",
        "item_crafter_name",
        "item_custom_data"
    ]

    total_containers = 0
    total_items = 0

    try:
        with open(output_csv, "w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=csv_headers)
            writer.writeheader()

            for zdo in iterate_zdos(json_path):
                # Look for the base64 inventory blob in the ZDO's string properties.
                # This is stored under the key "items" (or its hash 179721187).
                items_blob = get_zdo_value(zdo, "strings", "items", HASH_ITEMS)
                if not items_blob:
                    continue  # Not a container — skip this ZDO

                parsed_items = parse_inventory(items_blob)
                if not parsed_items:
                    continue

                total_containers += 1
                total_items += len(parsed_items)

                # Extract container-level metadata from the ZDO
                # (using `or {}` as fallback since these keys may be absent)
                pos = zdo.get("position") or {}   # World coordinates {x, y, z}
                sec = zdo.get("sector") or {}      # World sector {x, y}
                creator = get_zdo_value(zdo, "longs", "creator", HASH_CREATOR)  # Steam ID of placer
                
                # Try multiple string properties to find a custom name for the container.
                # Different container types and mods use different property keys for naming.
                custom_name = (
                    get_zdo_value(zdo, "strings", "tag", HASH_TAG) or           # Standard tag
                    get_zdo_value(zdo, "strings", "text", HASH_TEXT) or         # Sign text
                    get_zdo_value(zdo, "strings", "name", HASH_NAME) or         # Name property
                    get_zdo_value(zdo, "strings", "custom_name", HASH_CUSTOM_NAME) or  # Mod custom name
                    ""
                )

                # Write one CSV row per item in this container
                for item in parsed_items:
                    # Flatten mod-injected custom data (e.g., Epic Loot enchantments,
                    # engravings plugin data) into a single semicolon-delimited string
                    flat_custom_data = "; ".join(f"{k}={v}" for k, v in item["custom_data"].items())

                    writer.writerow({
                        "container_prefab": zdo.get("prefab", ""),
                        "container_prefab_name": zdo.get("prefabName", ""),
                        "container_x": pos.get("x", ""),
                        "container_y": pos.get("y", ""),
                        "container_z": pos.get("z", ""),
                        "container_sector_x": sec.get("x", ""),
                        "container_sector_y": sec.get("y", ""),
                        "container_creator_id": creator if creator is not None else "",
                        "container_custom_name": custom_name,
                        "item_prefab": item["prefab"],
                        "item_stack": item["stack"],
                        "item_durability": item["durability"],
                        "item_grid_x": item["x"],
                        "item_grid_y": item["y"],
                        "item_quality": item["quality"],
                        "item_variant": item["variant"],
                        "item_crafter_id": item["crafter_id"],
                        "item_crafter_name": item["crafter_name"],
                        "item_custom_data": flat_custom_data
                    })

        print(f"-> Cataloged {total_items} items across {total_containers} containers!")
        print(f"-> Master table saved to: '{output_csv}'")

    finally:
        # Clean up the intermediate JSON file if we generated it from a .db,
        # unless the user explicitly asked to keep it with --keep-json
        if is_temp_json and json_path.exists():
            os.remove(json_path)
            print("-> Cleaned up temporary JSON file.")

if __name__ == "__main__":
    main()