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

def get_stable_hash_code(s: str) -> int:
    """Simulates Valheim's 32-bit signed integer GetStableHashCode algorithm."""
    hash_val = 5381
    for char in s:
        hash_val = ((hash_val << 5) + hash_val) ^ ord(char)
        hash_val = (hash_val & 0xFFFFFFFF)
    if hash_val >= 0x80000000:
        hash_val -= 0x100000000
    return hash_val

# Pre-calculate common property hashes in case names are unresolved in the JSON
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
    """
    by_name = zdo.get(f"{category}ByName")
    if by_name and field_name in by_name:
        return by_name[field_name]
    
    normal = zdo.get(category)
    if normal:
        if hash_str in normal:
            return normal[hash_str]
        try:
            hash_int = int(hash_str)
            if hash_int in normal:
                return normal[hash_int]
        except ValueError:
            pass
            
    return None


# --- BINARY INVENTORY BLOB PARSERS ---

def read_7bit_int(f):
    result = 0
    shift = 0
    while True:
        b = f.read(1)
        if not b:
            raise EOFError("Unexpected end of file while reading 7-bit encoded integer")
        b = b[0]
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return result
        shift += 7


def read_string(f):
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
    """Parses base64-encoded inventory blob."""
    try:
        data = base64.b64decode(items_field)
    except Exception:
        return []

    f = BytesIO(data)
    try:
        version = read_int(f)
        item_count = read_int(f)
    except (EOFError, struct.error):
        return []

    items = []
    for _ in range(item_count):
        try:
            prefab = read_string(f)
            stack = read_int(f)
            durability = read_float(f)
            x, y = read_vector2i(f)
            equipped = read_bool(f)
            quality = read_int(f)
            variant = read_int(f)
            crafter_id = read_long(f)
            crafter_name = read_string(f)
            custom_count = read_int(f)

            custom_data = {}
            for _ in range(custom_count):
                key = read_string(f)
                value = read_string(f)
                custom_data[key] = value

            world_level = read_int(f)
            picked_up = read_bool(f)

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
            break

    return items


# --- MEMORY-EFFICIENT LOW-LEVEL STREAMING ENGINE ---

def is_pretty_printed(file_path: Path) -> bool:
    """Checks the first 64KB of the JSON to see if it is formatted/pretty-printed

    with newlines, which allows line-by-line streaming.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            chunk = f.read(65536)
            return chunk.count("\n") > 50
    except Exception:
        return False


def stream_zdos_file(file_path: Path):
    """Memory-efficient streaming parser that reads line-by-line.

    Detects boundaries of ZDO objects and filters for inventory elements
    without loading the 10GB dataset into memory.
    """
    current_zdo_lines = []
    in_zdo_list = False
    
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not in_zdo_list:
                if "zdoList" in line:
                    in_zdo_list = True
                continue
                
            # Every ZDO starts with the "persistent" property.
            # When we see it, we finalize the previous ZDO stored in our accumulator.
            if '"persistent"' in line:
                if current_zdo_lines:
                    # The line before '"persistent"' is the object boundary (e.g. "  {", "  }, {")
                    boundary_line = current_zdo_lines.pop()
                    
                    # Split combined close/open blocks like "  }, {"
                    if "}" in boundary_line and "{" in boundary_line:
                        current_zdo_lines.append("  }")
                        next_zdo_start = "  {"
                    else:
                        next_zdo_start = boundary_line
                    
                    prev_zdo_str = "\n".join(current_zdo_lines).strip()
                    
                    # Ensure standalone formatting
                    if prev_zdo_str.endswith(","):
                        prev_zdo_str = prev_zdo_str[:-1].strip()
                    if not prev_zdo_str.startswith("{"):
                        prev_zdo_str = "{" + prev_zdo_str
                    if not prev_zdo_str.endswith("}"):
                        prev_zdo_str = prev_zdo_str + "}"
                    
                    # Fast Pre-filter: Only deserialize JSON if this ZDO contains inventory data
                    if '"items"' in prev_zdo_str or HASH_ITEMS in prev_zdo_str:
                        try:
                            yield json.loads(prev_zdo_str)
                        except Exception:
                            pass # Silently skip malformed blocks
                    
                    # Set up the accumulator for the next ZDO object
                    current_zdo_lines = [next_zdo_start]
                    
            current_zdo_lines.append(line)
            
        # Process the final ZDO remaining in the stream
        if current_zdo_lines:
            prev_zdo_str = "\n".join(current_zdo_lines).strip()
            # Strip trailing JSON footer elements like ] and }
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
    """Determines whether to stream or load the JSON file in memory."""
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

    # 1. Process World Save .db file if provided
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

    # 2. Extract and Catalog Items from Containers
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
                # Search for the base64 items data blob in both resolved and unresolved ZDO fields
                items_blob = get_zdo_value(zdo, "strings", "items", HASH_ITEMS)
                if not items_blob:
                    continue

                parsed_items = parse_inventory(items_blob)
                if not parsed_items:
                    continue

                total_containers += 1
                total_items += len(parsed_items)

                # Extract container attributes (with safety fallbacks in case of null values)
                pos = zdo.get("position") or {}
                sec = zdo.get("sector") or {}
                creator = get_zdo_value(zdo, "longs", "creator", HASH_CREATOR)
                
                # Try to extract any custom tags/names given to the container (like via mods or signs)
                custom_name = (
                    get_zdo_value(zdo, "strings", "tag", HASH_TAG) or
                    get_zdo_value(zdo, "strings", "text", HASH_TEXT) or
                    get_zdo_value(zdo, "strings", "name", HASH_NAME) or
                    get_zdo_value(zdo, "strings", "custom_name", HASH_CUSTOM_NAME) or
                    ""
                )

                # Write item rows
                for item in parsed_items:
                    # Flatten any custom item components (such as epic loot stats or engravings)
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
        # Cleanup temporary JSON if we generated it from a .db file
        if is_temp_json and json_path.exists():
            os.remove(json_path)
            print("-> Cleaned up temporary JSON file.")

if __name__ == "__main__":
    main()