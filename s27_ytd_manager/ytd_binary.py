from __future__ import annotations

import os
import struct
import zlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


RSC7_IDENT = 0x37435352
SYSTEM_BASE = 0x50000000
GRAPHICS_BASE = 0x60000000
ALIGN_SIZE = 16
BASE_PAGE_SIZE = 0x2000
YTD_VERSION_LEGACY = 13


TEXTURE_FORMATS = {
    "D3DFMT_A8R8G8B8": 21,
    "D3DFMT_X8R8G8B8": 22,
    "D3DFMT_A1R5G5B5": 25,
    "D3DFMT_A8": 28,
    "D3DFMT_A8B8G8R8": 32,
    "D3DFMT_L8": 50,
    "D3DFMT_DXT1": 0x31545844,
    "D3DFMT_DXT3": 0x33545844,
    "D3DFMT_DXT5": 0x35545844,
    "D3DFMT_ATI1": 0x31495441,
    "D3DFMT_ATI2": 0x32495441,
    "D3DFMT_BC7": 0x20374342,
}

DXGI_TO_LEGACY = {
    28: TEXTURE_FORMATS["D3DFMT_A8B8G8R8"],  # R8G8B8A8_UNORM
    61: TEXTURE_FORMATS["D3DFMT_A8"],        # A8_UNORM
    71: TEXTURE_FORMATS["D3DFMT_DXT1"],      # BC1_UNORM
    74: TEXTURE_FORMATS["D3DFMT_DXT3"],      # BC2_UNORM
    77: TEXTURE_FORMATS["D3DFMT_DXT5"],      # BC3_UNORM
    80: TEXTURE_FORMATS["D3DFMT_ATI1"],      # BC4_UNORM
    83: TEXTURE_FORMATS["D3DFMT_ATI2"],      # BC5_UNORM
    98: TEXTURE_FORMATS["D3DFMT_BC7"],       # BC7_UNORM
}

USAGE_VALUES = {
    "UNKNOWN": 0,
    "DEFAULT": 1,
    "TERRAIN": 2,
    "CLOUDDENSITY": 3,
    "CLOUDNORMAL": 4,
    "CABLE": 5,
    "FENCE": 6,
    "SCRIPT": 8,
    "WATERFLOW": 9,
    "WATERFOAM": 10,
    "WATERFOG": 11,
    "WATEROCEAN": 12,
    "FOAMOPACITY": 14,
    "DIFFUSEMIPSHARPEN": 16,
    "DIFFUSEDARK": 18,
    "DIFFUSEALPHAOPAQUE": 19,
    "DIFFUSE": 20,
    "DETAIL": 21,
    "NORMAL": 22,
    "SPECULAR": 23,
    "EMISSIVE": 24,
    "TINTPALETTE": 25,
    "SKIPPROCESSING": 26,
}

USAGE_FLAG_VALUES = {
    "NOT_HALF": 1,
    "HD_SPLIT": 1 << 1,
    "X2": 1 << 2,
    "X4": 1 << 3,
    "Y4": 1 << 4,
    "X8": 1 << 5,
    "X16": 1 << 6,
    "X32": 1 << 7,
    "X64": 1 << 8,
    "Y64": 1 << 9,
    "X128": 1 << 10,
    "X256": 1 << 11,
    "X512": 1 << 12,
    "Y512": 1 << 13,
    "X1024": 1 << 14,
    "Y1024": 1 << 15,
    "X2048": 1 << 16,
    "Y2048": 1 << 17,
    "EMBEDDEDSCRIPTRT": 1 << 18,
    "UNK19": 1 << 19,
    "UNK20": 1 << 20,
    "UNK21": 1 << 21,
    "FLAG_FULL": 1 << 22,
    "MAPS_HALF": 1 << 23,
    "UNK24": 1 << 24,
}


def _u32(value: int) -> bytes:
    return struct.pack("<I", value & 0xFFFFFFFF)


def _u64(value: int) -> bytes:
    return struct.pack("<Q", value & 0xFFFFFFFFFFFFFFFF)


def _align(value: int, alignment: int = ALIGN_SIZE) -> int:
    return value + ((alignment - (value % alignment)) % alignment)


def jenk_hash(text: str | None) -> int:
    if text is None:
        return 0
    h = 0
    for ch in text:
        h = (h + (ord(ch) & 0xFF)) & 0xFFFFFFFF
        h = (h + ((h << 10) & 0xFFFFFFFF)) & 0xFFFFFFFF
        h = (h ^ (h >> 6)) & 0xFFFFFFFF
    h = (h + ((h << 3) & 0xFFFFFFFF)) & 0xFFFFFFFF
    h = (h ^ (h >> 11)) & 0xFFFFFFFF
    h = (h + ((h << 15) & 0xFFFFFFFF)) & 0xFFFFFFFF
    return h


def _enum_value(value: str | None, lookup: dict[str, int], default: int = 0) -> int:
    if not value:
        return default
    return lookup.get(value.strip().upper(), default)


def _flags_value(value: str | None, lookup: dict[str, int]) -> int:
    if not value:
        return 0
    result = 0
    for part in value.replace("|", ",").split(","):
        token = part.strip().upper()
        if token:
            result |= lookup.get(token, 0)
    return result


def _raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(level=6, wbits=-15)
    return compressor.compress(data) + compressor.flush()


class Block:
    is_graphics = False
    file_position = -1

    @property
    def block_length(self) -> int:
        raise NotImplementedError

    def get_references(self) -> list["Block"]:
        return []

    def get_parts(self) -> list[tuple[int, "Block"]]:
        return []

    def set_position(self, position: int):
        self.file_position = position
        for offset, part in self.get_parts():
            part.set_position(position + offset)

    def write(self, writer: "ResourceWriter"):
        raise NotImplementedError


class ResourceWriter:
    def __init__(self):
        self.system = bytearray()
        self.graphics = bytearray()

    def write_at(self, position: int, data: bytes):
        if position & SYSTEM_BASE == SYSTEM_BASE:
            stream = self.system
            offset = position & ~SYSTEM_BASE
        elif position & GRAPHICS_BASE == GRAPHICS_BASE:
            stream = self.graphics
            offset = position & ~GRAPHICS_BASE
        else:
            raise ValueError("Illegal resource position.")

        end = offset + len(data)
        if end > len(stream):
            stream.extend(b"\x00" * (end - len(stream)))
        stream[offset:end] = data


@dataclass(eq=False)
class StringBlock(Block):
    value: str

    @property
    def block_length(self) -> int:
        return len(self.value) + 1

    def write(self, writer: ResourceWriter):
        writer.write_at(self.file_position, self.value.encode("latin-1", "replace") + b"\x00")


@dataclass(eq=False)
class TextureDataBlock(Block):
    is_graphics = True
    data: bytes

    @property
    def block_length(self) -> int:
        return len(self.data)

    def write(self, writer: ResourceWriter):
        writer.write_at(self.file_position, self.data)


@dataclass(eq=False)
class UIntArrayBlock(Block):
    values: list[int]

    @property
    def block_length(self) -> int:
        return len(self.values) * 4

    def write(self, writer: ResourceWriter):
        writer.write_at(self.file_position, b"".join(_u32(value) for value in self.values))


@dataclass(eq=False)
class PointerArrayBlock(Block):
    values: list[Block]

    @property
    def block_length(self) -> int:
        return len(self.values) * 8

    def get_references(self) -> list[Block]:
        return list(self.values)

    def write(self, writer: ResourceWriter):
        data = b"".join(_u64(value.file_position if value is not None else 0) for value in self.values)
        writer.write_at(self.file_position, data)


@dataclass(eq=False)
class UIntList64Block(Block):
    values: list[int]
    data_block: UIntArrayBlock = field(init=False)

    def __post_init__(self):
        self.data_block = UIntArrayBlock(self.values)

    @property
    def block_length(self) -> int:
        return 16

    def get_references(self) -> list[Block]:
        return [self.data_block] if self.values else []

    def write(self, writer: ResourceWriter):
        count = len(self.values)
        data = _u64(self.data_block.file_position if count else 0)
        data += struct.pack("<HHI", count, count, 0)
        writer.write_at(self.file_position, data)


@dataclass(eq=False)
class PointerList64Block(Block):
    values: list[Block]
    data_block: PointerArrayBlock = field(init=False)

    def __post_init__(self):
        self.data_block = PointerArrayBlock(self.values)

    @property
    def block_length(self) -> int:
        return 16

    def get_references(self) -> list[Block]:
        return [self.data_block] if self.values else []

    def write(self, writer: ResourceWriter):
        count = len(self.values)
        data = _u64(self.data_block.file_position if count else 0)
        data += struct.pack("<HHI", count, count, 0)
        writer.write_at(self.file_position, data)


@dataclass(eq=False)
class ResourcePagesInfoBlock(Block):
    system_pages_count: int = 128
    graphics_pages_count: int = 0

    @property
    def block_length(self) -> int:
        return 16 + (8 * (self.system_pages_count + self.graphics_pages_count))

    def write(self, writer: ResourceWriter):
        data = struct.pack("<IIBBHI", 0, 0, self.system_pages_count, self.graphics_pages_count, 0, 0)
        data += b"\x00" * (8 * (self.system_pages_count + self.graphics_pages_count))
        writer.write_at(self.file_position, data)


@dataclass(eq=False)
class TextureBlock(Block):
    name: str
    width: int
    height: int
    levels: int
    fmt: int
    stride: int
    texture_data: TextureDataBlock
    usage: int = 1
    usage_flags: int = 1
    unknown_32h: int = 0
    extra_flags: int = 0
    name_block: StringBlock = field(init=False)

    def __post_init__(self):
        self.name_block = StringBlock(self.name)

    @property
    def block_length(self) -> int:
        return 144

    def get_references(self) -> list[Block]:
        return [self.name_block, self.texture_data]

    def write(self, writer: ResourceWriter):
        usage_data = (self.usage & 0x1F) | ((self.usage_flags & 0xFFFFFFFF) << 5)
        data = struct.pack(
            "<IIIIIIIIIIQHHIIIIIII",
            0,
            1,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            self.name_block.file_position,
            1,
            self.unknown_32h & 0xFFFF,
            0,
            0,
            0,
            usage_data,
            0,
            self.extra_flags,
            0,
        )
        data += struct.pack(
            "<HHHHIBBHIIIIQIIIIII",
            self.width,
            self.height,
            1,
            self.stride,
            self.fmt,
            0,
            self.levels,
            0,
            0,
            0,
            0,
            0,
            self.texture_data.file_position,
            0,
            0,
            0,
            0,
            0,
            0,
        )
        writer.write_at(self.file_position, data)


@dataclass(eq=False)
class TextureDictionaryBlock(Block):
    textures: list[TextureBlock]
    pages_info: ResourcePagesInfoBlock = field(default_factory=ResourcePagesInfoBlock)
    name_hashes: UIntList64Block = field(init=False)
    texture_list: PointerList64Block = field(init=False)

    def __post_init__(self):
        self.textures.sort(key=lambda texture: jenk_hash(texture.name.lower()))
        self.name_hashes = UIntList64Block([jenk_hash(texture.name.lower()) for texture in self.textures])
        self.texture_list = PointerList64Block(self.textures)

    @property
    def block_length(self) -> int:
        return 64

    def get_references(self) -> list[Block]:
        return [self.pages_info]

    def get_parts(self) -> list[tuple[int, Block]]:
        return [(0x20, self.name_hashes), (0x30, self.texture_list)]

    def write(self, writer: ResourceWriter):
        data = struct.pack("<IIQ", 0, 1, self.pages_info.file_position)
        data += struct.pack("<IIII", 0, 0, 1, 0)

        count = len(self.name_hashes.values)
        data += _u64(self.name_hashes.data_block.file_position if count else 0)
        data += struct.pack("<HHI", count, count, 0)

        count = len(self.texture_list.values)
        data += _u64(self.texture_list.data_block.file_position if count else 0)
        data += struct.pack("<HHI", count, count, 0)
        writer.write_at(self.file_position, data)


def _parse_dds(dds_path: str) -> tuple[int, int, int, int, int, bytes]:
    with open(dds_path, "rb") as handle:
        data = handle.read()

    if len(data) < 128 or data[:4] != b"DDS ":
        raise ValueError(f"Invalid DDS file: {dds_path}")

    height = struct.unpack_from("<I", data, 12)[0]
    width = struct.unpack_from("<I", data, 16)[0]
    mip_count = struct.unpack_from("<I", data, 28)[0] or 1
    pf_flags = struct.unpack_from("<I", data, 80)[0]
    fourcc = data[84:88]
    rgb_bit_count = struct.unpack_from("<I", data, 88)[0]
    r_mask = struct.unpack_from("<I", data, 92)[0]
    g_mask = struct.unpack_from("<I", data, 96)[0]
    b_mask = struct.unpack_from("<I", data, 100)[0]
    a_mask = struct.unpack_from("<I", data, 104)[0]
    data_offset = 128

    if pf_flags & 0x4:
        if fourcc == b"DXT1":
            fmt = TEXTURE_FORMATS["D3DFMT_DXT1"]
        elif fourcc == b"DXT3":
            fmt = TEXTURE_FORMATS["D3DFMT_DXT3"]
        elif fourcc == b"DXT5":
            fmt = TEXTURE_FORMATS["D3DFMT_DXT5"]
        elif fourcc == b"ATI1":
            fmt = TEXTURE_FORMATS["D3DFMT_ATI1"]
        elif fourcc == b"ATI2":
            fmt = TEXTURE_FORMATS["D3DFMT_ATI2"]
        elif fourcc == b"DX10":
            if len(data) < 148:
                raise ValueError(f"Invalid DDS DX10 header: {dds_path}")
            dxgi_format = struct.unpack_from("<I", data, 128)[0]
            fmt = DXGI_TO_LEGACY.get(dxgi_format)
            if fmt is None:
                raise ValueError(f"Unsupported DDS DXGI format {dxgi_format}: {dds_path}")
            data_offset = 148
        else:
            raise ValueError(f"Unsupported DDS FourCC {fourcc!r}: {dds_path}")
    elif rgb_bit_count == 32 and r_mask == 0x000000FF and g_mask == 0x0000FF00 and b_mask == 0x00FF0000:
        fmt = TEXTURE_FORMATS["D3DFMT_A8B8G8R8"]
    elif rgb_bit_count == 32 and r_mask == 0x00FF0000 and g_mask == 0x0000FF00 and b_mask == 0x000000FF:
        fmt = TEXTURE_FORMATS["D3DFMT_A8R8G8B8"] if a_mask else TEXTURE_FORMATS["D3DFMT_X8R8G8B8"]
    else:
        raise ValueError(f"Unsupported DDS pixel format: {dds_path}")

    stride = calculate_stride(fmt, width)
    return width, height, mip_count, fmt, stride, data[data_offset:]


def calculate_stride(fmt: int, width: int) -> int:
    if fmt == TEXTURE_FORMATS["D3DFMT_DXT1"] or fmt == TEXTURE_FORMATS["D3DFMT_ATI1"]:
        return max(1, (width + 3) // 4) * 8
    if fmt in {
        TEXTURE_FORMATS["D3DFMT_DXT3"],
        TEXTURE_FORMATS["D3DFMT_DXT5"],
        TEXTURE_FORMATS["D3DFMT_ATI2"],
        TEXTURE_FORMATS["D3DFMT_BC7"],
    }:
        return max(1, (width + 3) // 4) * 16
    if fmt in {
        TEXTURE_FORMATS["D3DFMT_A8R8G8B8"],
        TEXTURE_FORMATS["D3DFMT_X8R8G8B8"],
        TEXTURE_FORMATS["D3DFMT_A8B8G8R8"],
    }:
        return width * 4
    if fmt in {TEXTURE_FORMATS["D3DFMT_A8"], TEXTURE_FORMATS["D3DFMT_L8"]}:
        return width
    if fmt == TEXTURE_FORMATS["D3DFMT_A1R5G5B5"]:
        return width * 2
    raise ValueError(f"Unsupported texture format 0x{fmt:08X}")


def _collect_blocks(root: Block) -> tuple[list[Block], list[Block]]:
    system_blocks: list[Block] = []
    graphics_blocks: list[Block] = []
    seen_refs: set[int] = set()

    def add_block(block: Block):
        target = graphics_blocks if block.is_graphics else system_blocks
        if not any(existing is block for existing in target):
            target.append(block)

    def add_children(block: Block):
        for reference in block.get_references():
            key = id(reference)
            if key not in seen_refs:
                seen_refs.add(key)
                add_block(reference)
                add_children(reference)
        for _, part in block.get_parts():
            add_children(part)

    add_block(root)
    add_children(root)
    return system_blocks, graphics_blocks


def _assign_positions(blocks: list[Block], base_position: int, max_page_count: int) -> tuple[int, int]:
    if not blocks:
        return 0, 0

    is_system = base_position == SYSTEM_BASE
    max_page_size_mult = 16
    max_block_size = max(block.block_length for block in blocks)
    min_block_size = min(block.block_length for block in blocks)
    base_shift = 0
    base_size = BASE_PAGE_SIZE
    while ((base_size < min_block_size) or ((base_size * max_page_size_mult) < max_block_size)) and base_shift < 0xF:
        base_shift += 1
        base_size = BASE_PAGE_SIZE << base_shift

    if (base_size * max_page_size_mult) < max_block_size:
        raise ValueError("Unable to fit largest YTD resource block.")

    root_block = blocks[0] if is_system else None
    sorted_blocks = [block for block in blocks if block is not root_block]
    sorted_blocks.sort(key=lambda block: block.block_length, reverse=True)
    if root_block is not None:
        sorted_blocks.insert(0, root_block)

    while True:
        page_sizes: list[list[int] | None] = [None, None, None, None, None]
        block_pages: dict[Block, tuple[int, int, int]] = {}
        page_counts = [0, 0, 0, 0, 0]

        largest_page_size_i = 0
        largest_page_size = base_size
        while largest_page_size < max_block_size:
            largest_page_size_i += 1
            largest_page_size *= 2

        for index, block in enumerate(sorted_blocks):
            size = block.block_length
            if index == 0:
                page_sizes[largest_page_size_i] = [size]
                block_pages[block] = (largest_page_size_i, 0, 0)
                continue

            page_size_index = 0
            page_size = base_size
            while size > page_size and page_size_index < largest_page_size_i:
                page_size_index += 1
                page_size *= 2

            found = False
            test_i = page_size_index
            test_size = page_size
            while not found and test_i <= largest_page_size_i:
                pages = page_sizes[test_i]
                if pages is not None:
                    for page_index, used_size in enumerate(pages):
                        aligned = _align(used_size)
                        new_size = aligned + size
                        if new_size <= test_size:
                            pages[page_index] = new_size
                            block_pages[block] = (test_i, page_index, aligned)
                            found = True
                            break
                test_i += 1
                test_size *= 2

            if not found:
                pages = page_sizes[page_size_index]
                if pages is None:
                    pages = []
                    page_sizes[page_size_index] = pages
                page_index = len(pages)
                pages.append(size)
                block_pages[block] = (page_size_index, page_index, 0)

        total_page_count = 0
        for index, pages in enumerate(page_sizes):
            page_counts[index] = len(pages) if pages is not None else 0
            total_page_count += page_counts[index]

        valid = total_page_count <= max_page_count
        valid = valid and page_counts[0] <= 0x7F and page_counts[1] <= 0x3F
        valid = valid and page_counts[2] <= 0xF and page_counts[3] <= 0x3 and page_counts[4] <= 0x1
        if valid:
            break

        if base_shift >= 0xF:
            raise ValueError("Unable to pack YTD resource blocks.")
        base_shift += 1
        base_size = BASE_PAGE_SIZE << base_shift

    page_offset = 0
    page_offsets = [0, 0, 0, 0, 0]
    for index in range(4, -1, -1):
        page_offsets[index] = page_offset
        page_offset += (base_size * (1 << index)) * page_counts[index]

    for block, (page_size_index, page_index, offset) in block_pages.items():
        page_size = base_size * (1 << page_size_index)
        block_position = page_offsets[page_size_index] + (page_size * page_index) + offset
        block.set_position(base_position + block_position)

    flags = base_shift & 0xF
    flags |= (page_counts[4] & 0x1) << 4
    flags |= (page_counts[3] & 0x3) << 5
    flags |= (page_counts[2] & 0xF) << 7
    flags |= (page_counts[1] & 0x3F) << 11
    flags |= (page_counts[0] & 0x7F) << 17
    return flags, page_offset


def _write_blocks(system_blocks: list[Block], graphics_blocks: list[Block]) -> ResourceWriter:
    writer = ResourceWriter()
    for block in system_blocks:
        block.write(writer)
    for block in graphics_blocks:
        block.write(writer)
    return writer


def _xml_child_text(node: ET.Element, name: str, default: str = "") -> str:
    child = node.find(name)
    return (child.text or "").strip() if child is not None and child.text is not None else default


def _xml_child_value(node: ET.Element, name: str, default: int = 0) -> int:
    child = node.find(name)
    if child is None:
        return default
    value = child.attrib.get("value", "")
    try:
        return int(value, 0)
    except ValueError:
        return default


def build_ytd_bytes_from_xml(xml_text: str, dds_folder: str) -> bytes:
    root = ET.fromstring(xml_text)
    if root.tag != "TextureDictionary":
        raise ValueError("YTD XML root must be TextureDictionary.")

    textures: list[TextureBlock] = []
    for item in root.findall("Item"):
        name = _xml_child_text(item, "Name")
        file_name = _xml_child_text(item, "FileName")
        if not name:
            raise ValueError("Texture item is missing a Name.")
        if not file_name:
            raise ValueError(f"Texture '{name}' is missing a FileName.")

        dds_path = os.path.join(dds_folder, file_name)
        if not os.path.isfile(dds_path):
            raise FileNotFoundError(dds_path)

        width, height, levels, dds_format, stride, image_data = _parse_dds(dds_path)
        xml_format = _enum_value(_xml_child_text(item, "Format"), TEXTURE_FORMATS, dds_format)
        if xml_format and xml_format != dds_format:
            dds_format = xml_format

        textures.append(
            TextureBlock(
                name=name,
                width=width,
                height=height,
                levels=levels,
                fmt=dds_format,
                stride=stride,
                texture_data=TextureDataBlock(image_data),
                usage=_enum_value(_xml_child_text(item, "Usage"), USAGE_VALUES, 1),
                usage_flags=_flags_value(_xml_child_text(item, "UsageFlags"), USAGE_FLAG_VALUES) or 1,
                unknown_32h=_xml_child_value(item, "Unk32", 0),
                extra_flags=_xml_child_value(item, "ExtraFlags", 0),
            )
        )

    if not textures:
        raise ValueError("YTD XML has no texture items.")

    texture_dictionary = TextureDictionaryBlock(textures)
    system_blocks, graphics_blocks = _collect_blocks(texture_dictionary)
    system_flags, system_size = _assign_positions(system_blocks, SYSTEM_BASE, 128)
    graphics_flags, graphics_size = _assign_positions(graphics_blocks, GRAPHICS_BASE, 128 - _page_count_from_flags(system_flags))

    texture_dictionary.pages_info.system_pages_count = _page_count_from_flags(system_flags)
    texture_dictionary.pages_info.graphics_pages_count = _page_count_from_flags(graphics_flags)

    writer = _write_blocks(system_blocks, graphics_blocks)
    system_data = bytes(writer.system).ljust(system_size, b"\x00")
    graphics_data = bytes(writer.graphics).ljust(graphics_size, b"\x00")
    resource_data = system_data + graphics_data
    compressed = _raw_deflate(resource_data)

    system_version = (YTD_VERSION_LEGACY >> 4) & 0xF
    graphics_version = YTD_VERSION_LEGACY & 0xF
    header = struct.pack(
        "<IIII",
        RSC7_IDENT,
        YTD_VERSION_LEGACY,
        system_flags | (system_version << 28),
        graphics_flags | (graphics_version << 28),
    )
    return header + compressed


def _page_count_from_flags(flags: int) -> int:
    return (
        ((flags >> 17) & 0x7F)
        + ((flags >> 11) & 0x3F)
        + ((flags >> 7) & 0xF)
        + ((flags >> 5) & 0x3)
        + ((flags >> 4) & 0x1)
    )


def build_ytd_file_from_xml(xml_text: str, dds_folder: str, output_path: str) -> str:
    data = build_ytd_bytes_from_xml(xml_text, dds_folder)
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(output_path, "wb") as handle:
        handle.write(data)
    return output_path
