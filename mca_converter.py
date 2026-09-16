#!/usr/bin/env python3
"""
Minecraft .mca (Anvil Region) -> simple local game-server map format.

Python: 3.13+
Dependencies:
    pip install nbtlib

Usage:
    python mca_converter.py "C:\\path\\to\\region" -o "C:\\path\\to\\map"

Output:
    map/
      metadata.json
      chunks/
        chunk_<x>_<z>.json
      heightmaps/
        chunk_<x>_<z>.bin
      blocks/
        chunk_<x>_<z>.bin

The output is deliberately a neutral intermediate format. It is NOT claimed
to be the native STALZONE map format. It gives the local server a clean,
streamable representation that can later be adapted to the actual client
protocol.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

try:
    import nbtlib
except ImportError:
    print("Missing dependency: nbtlib")
    print("Install with: py -3.13 -m pip install nbtlib")
    raise SystemExit(1)


REGION_CHUNK_BYTES = 32 * 32 * 32
CHUNK_SIZE = 16


def plain(value: Any) -> Any:
    """Convert nbtlib values recursively to normal Python values."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    try:
        return value.item()
    except AttributeError:
        return value


def parse_region_name(path: Path) -> tuple[int, int]:
    # r.<region_x>.<region_z>.mca
    parts = path.stem.split(".")
    if len(parts) != 3 or parts[0] != "r":
        raise ValueError(f"Invalid region filename: {path.name}")
    return int(parts[1]), int(parts[2])


def read_region_chunks(region_path: Path):
    """
    Read the 8 KiB Anvil header and yield existing chunk records.
    Returns: (local_chunk_x, local_chunk_z, compression, payload)
    """
    data = region_path.read_bytes()

    if len(data) < 8192:
        raise ValueError(f"{region_path.name}: file is smaller than Anvil header")

    locations = data[:4096]

    for index in range(1024):
        entry = locations[index * 4:index * 4 + 4]
        offset = int.from_bytes(entry[:3], "big")
        sector_count = entry[3]

        if offset == 0 or sector_count == 0:
            continue

        start = offset * 4096
        end = start + sector_count * 4096

        if end > len(data):
            print(f"WARNING: chunk index {index} points outside file; skipped")
            continue

        if start + 5 > len(data):
            continue

        length = int.from_bytes(data[start:start + 4], "big")
        compression = data[start + 4]

        if length < 1 or start + 4 + length > end:
            print(f"WARNING: invalid chunk length at index {index}; skipped")
            continue

        compressed = data[start + 5:start + 4 + length]

        local_x = index % 32
        local_z = index // 32

        yield local_x, local_z, compression, compressed


def decompress_chunk(compression: int, payload: bytes) -> bytes:
    if compression == 1:
        # GZip
        import gzip
        return gzip.decompress(payload)

    if compression == 2:
        # Zlib
        return zlib.decompress(payload)

    if compression == 3:
        # Uncompressed
        return payload

    if compression == 4:
        # LZ4 is possible in newer/variant formats, but vanilla Anvil
        # normally uses 1 or 2. Fail explicitly rather than corrupting data.
        raise ValueError("LZ4-compressed chunk is not supported by this converter")

    raise ValueError(f"Unknown Anvil compression type: {compression}")


def load_chunk_nbt(raw: bytes) -> dict:
    """
    nbtlib can parse an NBT file-like stream. We keep the API isolated here so
    it can be replaced later if a particular Minecraft version needs special
    handling.
    """
    from io import BytesIO

    nbt = nbtlib.File.parse(BytesIO(raw))
    return plain(nbt)


def find_sections(root: dict) -> list[dict]:
    level = root.get("Level", root)

    sections = level.get("sections", level.get("Sections", []))
    return [s for s in sections if isinstance(s, dict)]


def palette_name(entry: Any) -> str:
    if isinstance(entry, dict):
        name = entry.get("Name", entry.get("name"))
        if name is not None:
            return str(name)
    return str(entry)


def decode_palette_indices(section: dict) -> tuple[list[str], list[int]]:
    """
    Supports the classic palette + block_states format.

    For modern Minecraft chunks:
      section["block_states"]["palette"]
      section["block_states"]["data"]

    For older variants:
      section["Palette"]
      section["BlockStates"]

    A complete Minecraft bit-packed palette decoder is included for the
    common modern representation.
    """
    bs = section.get("block_states") or section.get("BlockStates")

    if not isinstance(bs, dict):
        palette = section.get("Palette")
        states = section.get("BlockStates")
    else:
        palette = bs.get("palette") or bs.get("Palette")
        states = bs.get("data") or bs.get("Data")

    if not palette:
        return ["minecraft:air"], [0] * 4096

    palette = [palette_name(x) for x in palette]

    if states is None:
        # Single-palette section.
        return palette, [0] * 4096

    states = [int(x) for x in states]

    # Vanilla uses at least 4 bits per block for paletted block states.
    bits = max(4, (len(palette) - 1).bit_length())
    values_per_long = 64 // bits
    expected = (4096 + values_per_long - 1) // values_per_long

    if len(states) < expected:
        # Some old formats use a different packing scheme.
        # Don't silently produce a wrong map.
        raise ValueError(
            f"Unexpected block-state data length: {len(states)}, "
            f"expected at least {expected}"
        )

    mask = (1 << bits) - 1
    indices: list[int] = [0] * 4096

    for i in range(4096):
        long_index = i // values_per_long
        bit_index = (i % values_per_long) * bits
        value = (states[long_index] >> bit_index) & mask

        if value >= len(palette):
            value = 0

        indices[i] = value

    return palette, indices


def section_to_blocks(section: dict) -> tuple[int, dict[str, int], list[str]]:
    y = int(section.get("Y", section.get("y", 0)))

    palette, indices = decode_palette_indices(section)

    # Store only non-air blocks in the compact JSON representation.
    blocks: dict[str, int] = {}

    for i, palette_index in enumerate(indices):
        name = palette[palette_index]

        if name == "minecraft:air":
            continue

        x = i & 15
        z = (i >> 4) & 15
        local_y = (i >> 8) & 15

        world_y = y * 16 + local_y
        blocks[f"{x},{world_y},{z}"] = palette_index

    return y, {k: v for k, v in blocks.items()}, palette


def chunk_summary(root: dict) -> dict:
    level = root.get("Level", root)

    out = {
        "DataVersion": level.get("DataVersion", root.get("DataVersion")),
        "xPos": level.get("xPos", level.get("xPos")),
        "zPos": level.get("zPos", level.get("zPos")),
        "Status": level.get("Status", level.get("status")),
        "InhabitedTime": level.get("InhabitedTime", 0),
    }

    if "Heightmaps" in level:
        out["Heightmaps"] = plain(level["Heightmaps"])

    if "Biomes" in level:
        out["Biomes"] = plain(level["Biomes"])

    return out


def convert_region(region_path: Path, output: Path) -> int:
    rx, rz = parse_region_name(region_path)

    chunks_dir = output / "chunks"
    blocks_dir = output / "blocks"
    heights_dir = output / "heightmaps"

    chunks_dir.mkdir(parents=True, exist_ok=True)
    blocks_dir.mkdir(parents=True, exist_ok=True)
    heights_dir.mkdir(parents=True, exist_ok=True)

    converted = 0

    for local_x, local_z, compression, payload in read_region_chunks(region_path):
        cx = rx * 32 + local_x
        cz = rz * 32 + local_z

        try:
            raw = decompress_chunk(compression, payload)
            root = load_chunk_nbt(raw)
        except Exception as exc:
            print(f"[SKIP] chunk {cx},{cz}: {exc}")
            continue

        section_records = []
        palettes: list[str] = []
        blocks: dict[str, int] = {}

        for section in find_sections(root):
            try:
                section_y, section_blocks, palette = section_to_blocks(section)
            except Exception as exc:
                print(f"[WARN] chunk {cx},{cz}, section: {exc}")
                continue

            palette_offset = len(palettes)
            palettes.extend(palette)

            # Remap palette index to global chunk palette index.
            for pos, index in section_blocks.items():
                blocks[pos] = palette_offset + index

            section_records.append({
                "section_y": section_y,
                "palette_start": palette_offset,
                "palette_size": len(palette),
            })

        summary = chunk_summary(root)
        summary.update({
            "chunk_x": cx,
            "chunk_z": cz,
            "region_x": rx,
            "region_z": rz,
            "blocks_non_air": len(blocks),
            "palette": palettes,
            "sections": section_records,
        })

        # Metadata / palette / entity information.
        (chunks_dir / f"chunk_{cx}_{cz}.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        # Compact block records:
        # uint16 x, int16 y, uint16 z, uint32 palette_index
        with (blocks_dir / f"chunk_{cx}_{cz}.bin").open("wb") as f:
            f.write(b"STBL")
            f.write(struct.pack("<I", len(blocks)))

            for pos, palette_index in blocks.items():
                x, y, z = map(int, pos.split(","))
                f.write(struct.pack("<HhHI", x, y, z, palette_index))

        # Preserve heightmaps as JSON if present. This is deliberately generic.
        level = root.get("Level", root)
        hm = level.get("Heightmaps")
        if hm:
            (heights_dir / f"chunk_{cx}_{cz}.json").write_text(
                json.dumps(plain(hm), ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

        converted += 1
        print(f"[OK] {region_path.name}: chunk {cx},{cz} -> {len(blocks):,} blocks")

    return converted


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert Minecraft .mca Anvil regions into a local-server map format."
    )
    parser.add_argument("input", type=Path, help="Directory containing .mca files OR one .mca file")
    parser.add_argument("-o", "--output", type=Path, default=Path("converted_map"))
    args = parser.parse_args()

    source = args.input

    if source.is_file():
        regions = [source]
    elif source.is_dir():
        regions = sorted(source.glob("r.*.*.mca"))
    else:
        print(f"Input does not exist: {source}")
        return 2

    if not regions:
        print("No .mca files found.")
        return 2

    args.output.mkdir(parents=True, exist_ok=True)

    total = 0
    failed = 0

    for region in regions:
        try:
            total += convert_region(region, args.output)
        except Exception as exc:
            failed += 1
            print(f"[ERROR] {region}: {exc}")

    metadata = {
        "format": "local-world-v1",
        "source": "Minecraft Anvil .mca",
        "regions": len(regions),
        "chunks_converted": total,
        "regions_failed": failed,
        "chunk_size": 16,
        "block_record": "STBL + uint32 count + repeated <uint16 x, int16 y, uint16 z, uint32 palette_index>",
        "note": "Intermediate map format; adapt to the actual game client protocol before use.",
    }

    (args.output / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print()
    print(f"Finished. Regions: {len(regions)}, chunks: {total}, failed regions: {failed}")
    print(f"Output: {args.output.resolve()}")

    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
