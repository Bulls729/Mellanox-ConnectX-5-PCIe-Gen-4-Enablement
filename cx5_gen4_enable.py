#!/usr/bin/env python3
"""
ConnectX-5 PCIe Gen4 Enabler — Universal Edition

Patches user-supplied ConnectX-5 firmware images to enable PCIe Gen4 (16 GT/s).
Supports all CX5 variants: EN, VPI, SFP28, QSFP28, single-port, dual-port.
Locates config fields via FS4 ITOC parsing (firmware-version-independent).

Optionally upgrades OEM firmware to a newer stock Mellanox LTS base while
preserving vendor-specific board tuning (Dell OEM profile built-in).

No external dependencies — uses native Mellanox CRC-16 (poly 0x100B) from the
open-source mstflint project (https://github.com/Mellanox/mstflint).

IMPORTANT: Back up your firmware BEFORE using this tool!
    flint -d mt4119_pciconf0 ri backup_fw.bin
    Note: flint/mstflint commands require root/admin privileges (sudo on Linux).

Usage:
    # Patch existing firmware in-place:
    python3 cx5_gen4_enable.py -i firmware.bin -o patched.bin

    # Upgrade Dell OEM to latest LTS base + Gen4:
    python3 cx5_gen4_enable.py -i dell_oem.bin -o patched.bin --upgrade-base acat_8002.bin

    # Analyze firmware without patching:
    python3 cx5_gen4_enable.py -i firmware.bin --analyze
"""

import argparse
import struct
import sys
import os
from dataclasses import dataclass, field
from typing import Optional

VERSION = "1.1.0"

# ─── FS4 ITOC Constants ───────────────────────────────────────────────────────

ITOC_OFFSET = 0x5000
ITOC_HEADER_SIZE = 0x20
ITOC_ENTRY_SIZE = 32

SECTION_TYPES = {
    0x02: "PCI_CODE", 0x03: "MAIN_CODE", 0x04: "PCIE_LINK_CODE",
    0x05: "IRON_PREP_CODE", 0x06: "POST_IRON_BOOT_CODE",
    0x07: "UPGRADE_CODE", 0x08: "HW_BOOT_CFG", 0x09: "HW_MAIN_CFG",
    0x0A: "PHY_UC_CODE", 0x0B: "PHY_UC_CONSTS", 0x0C: "PCIE_PHY_UC_CODE",
    0x10: "IMAGE_INFO", 0x11: "FW_BOOT_CFG", 0x12: "FW_MAIN_CFG",
    0x18: "ROM_CODE", 0x20: "RESET_INFO", 0x30: "DBG_FW_INI",
    0x32: "DBG_FW_PARAMS", 0xA0: "IMAGE_SIGNATURE_256",
    0xA1: "PUBLIC_KEYS_2048", 0xA2: "FORBIDDEN_VERSIONS",
    0xA3: "IMAGE_SIGNATURE_512", 0xA4: "PUBLIC_KEYS_4096",
    0xE9: "CRDUMP_MASK_DATA", 0xEB: "PROGRAMMABLE_HW_FW",
}

SEC_HW_BOOT_CFG = 0x08
SEC_HW_MAIN_CFG = 0x09
SEC_IMAGE_INFO  = 0x10
SEC_FW_BOOT_CFG = 0x11
SEC_FW_MAIN_CFG = 0x12


# ─── Gen4 Patch Definitions ──────────────────────────────────────────────────
#
# Derived from binary-diffing firmware images across the CX5 product line:
#   - MCX512F-ACAT (EN 2×SFP28 Gen3)  vs  MCX512A-ADAT (Ex 2×SFP28 Gen4)
#   - MCX555A-ECAT (VPI 1×QSFP28 Gen3) — analyzed for QSFP28/single-port
#   - MCX516A-CDA  (Ex 2×QSFP28 Gen4) — confirmed QSFP28 Gen4 values
#
# Key finding: QSFP28 cards ship with port_gen=0x06 and speed tables already
# invalidated (0x0FFF) for BOTH Gen3 and Gen4 variants. Gen4 on QSFP28 is
# controlled entirely by cap_index and max_speed. SFP28 cards need all 8 bytes.

@dataclass
class PatchField:
    """A single byte to patch, defined by section-relative offset."""
    section_type: int
    section_offset: int
    gen3_value: int       # The Gen3 value that needs patching
    gen4_value: int       # The target Gen4 value
    name: str
    description: str
    skip_values: list = field(default_factory=list)
    # Values that are valid and should be silently skipped:
    #   0x00 = disabled port (single-port cards)
    #   0x06 = QSFP28 port gen (same for Gen3 & Gen4)
    #   0x0F = speed table already invalidated (high byte)
    #   0xFF = speed table already invalidated (low byte)

# === Universal patches — apply to ALL CX5 variants ===
# These two bytes are the core Gen4 enablers. They control PCIe capability
# advertisement and link training mode. Consistent across EN, VPI, SFP28, QSFP28.
#
# === Conditional patches — apply only to SFP28 cards ===
# Port generation bytes and speed table entries differ by connector type:
#   SFP28: port_gen=0x01 (Gen3), speed_tables=0x0020/0x0021
#   QSFP28: port_gen=0x06 (both Gen3 & Gen4!), speed_tables=0x0FFF (already invalidated)
# The tool skips these on QSFP28 and single-port cards automatically.

GEN4_PATCHES = [
    # --- Universal: PCIe capability advertisement ---
    PatchField(SEC_FW_BOOT_CFG, 0x0093, 0x45, 0x47,
               "pcie_cap_index",
               "PCIe capability advertisement (0x45=Gen3, 0x47=Gen4)",
               skip_values=[]),
    # --- Universal: Link training mode ---
    PatchField(SEC_HW_BOOT_CFG, 0x0023, 0x07, 0x0F,
               "pcie_max_speed_supported",
               "Link training mode (0x07=Gen3, 0x0F=Gen4 w/ Phase 2/3 EQ)",
               skip_values=[]),
    # --- Conditional: Port 1 PCIe generation ---
    PatchField(SEC_HW_MAIN_CFG, 0x0245, 0x01, 0x04,
               "port1_pcie_gen",
               "Port 1 PCIe generation (0x01=Gen3 SFP28, 0x04=Gen4 SFP28)",
               skip_values=[0x00, 0x06]),  # 0x00=disabled, 0x06=QSFP28
    # --- Conditional: Port 2 PCIe generation ---
    PatchField(SEC_HW_MAIN_CFG, 0x0285, 0x01, 0x04,
               "port2_pcie_gen",
               "Port 2 PCIe generation (0x01=Gen3 SFP28, 0x04=Gen4 SFP28)",
               skip_values=[0x00, 0x06]),  # 0x00=disabled/single-port, 0x06=QSFP28
    # --- Conditional: Speed table profile 0x0020 ---
    PatchField(SEC_HW_MAIN_CFG, 0x0404, 0x00, 0x0F,
               "speed_table_0020_hi",
               "Gen3 speed profile 0x0020 high byte (invalidate)",
               skip_values=[]),
    PatchField(SEC_HW_MAIN_CFG, 0x0405, 0x20, 0xFF,
               "speed_table_0020_lo",
               "Gen3 speed profile 0x0020 low byte (invalidate)",
               skip_values=[]),
    # --- Conditional: Speed table profile 0x0021 ---
    PatchField(SEC_HW_MAIN_CFG, 0x0406, 0x00, 0x0F,
               "speed_table_0021_hi",
               "Gen3 speed profile 0x0021 high byte (invalidate)",
               skip_values=[]),
    PatchField(SEC_HW_MAIN_CFG, 0x0407, 0x21, 0xFF,
               "speed_table_0021_lo",
               "Gen3 speed profile 0x0021 low byte (invalidate)",
               skip_values=[]),
]


# ─── OEM Profiles ────────────────────────────────────────────────────────────

# Dell OEM Profile (0V5DG9 / 0TDNNT)
# Derived from: Dell 16.35.4554 vs ACAT 16.35.4554
DELL_OEM_PROFILE = [
    (SEC_FW_BOOT_CFG, 0x0018, 0x90), (SEC_FW_BOOT_CFG, 0x001C, 0x28),
    (SEC_FW_BOOT_CFG, 0x003D, 0x91), (SEC_FW_BOOT_CFG, 0x011C, 0xBF),
    (SEC_FW_BOOT_CFG, 0x0120, 0x20), (SEC_FW_BOOT_CFG, 0x0131, 0x02),
    (SEC_FW_BOOT_CFG, 0x02BC, 0x07),
    (SEC_FW_MAIN_CFG, 0x0023, 0x12), (SEC_FW_MAIN_CFG, 0x004C, 0x00),
    (SEC_FW_MAIN_CFG, 0x0221, 0x10), (SEC_FW_MAIN_CFG, 0x0227, 0x40),
    (SEC_FW_MAIN_CFG, 0x08A8, 0xA0), (SEC_FW_MAIN_CFG, 0x0938, 0xA0),
    (SEC_FW_MAIN_CFG, 0x09BC, 0x83), (SEC_FW_MAIN_CFG, 0x09C1, 0x00),
    (SEC_FW_MAIN_CFG, 0x09CC, 0x00), (SEC_FW_MAIN_CFG, 0x0A01, 0x40),
    (SEC_FW_MAIN_CFG, 0x0A0C, 0x02), (SEC_FW_MAIN_CFG, 0x0A10, 0x83),
    (SEC_FW_MAIN_CFG, 0x0A12, 0x06), (SEC_FW_MAIN_CFG, 0x0A13, 0x21),
    (SEC_FW_MAIN_CFG, 0x0A14, 0xA0), (SEC_FW_MAIN_CFG, 0x0A15, 0x00),
    (SEC_FW_MAIN_CFG, 0x0A17, 0x01), (SEC_FW_MAIN_CFG, 0x0A1B, 0x10),
    (SEC_FW_MAIN_CFG, 0x0A1C, 0x00), (SEC_FW_MAIN_CFG, 0x0A1F, 0x00),
    (SEC_FW_MAIN_CFG, 0x0A20, 0xCD), (SEC_FW_MAIN_CFG, 0x0A22, 0x8A),
    (SEC_FW_MAIN_CFG, 0x0A2D, 0x03), (SEC_FW_MAIN_CFG, 0x0A2E, 0x19),
    (SEC_FW_MAIN_CFG, 0x0A30, 0x82), (SEC_FW_MAIN_CFG, 0x0A33, 0x04),
    (SEC_FW_MAIN_CFG, 0x0A36, 0x01), (SEC_FW_MAIN_CFG, 0x0A37, 0x01),
    (SEC_FW_MAIN_CFG, 0x0A38, 0x3C), (SEC_FW_MAIN_CFG, 0x0A39, 0x60),
    (SEC_FW_MAIN_CFG, 0x0A3C, 0x82), (SEC_FW_MAIN_CFG, 0x0A3F, 0x04),
    (SEC_FW_MAIN_CFG, 0x0A40, 0xC3), (SEC_FW_MAIN_CFG, 0x0A42, 0x01),
    (SEC_FW_MAIN_CFG, 0x0A43, 0x96), (SEC_FW_MAIN_CFG, 0x0A44, 0x80),
    (SEC_HW_MAIN_CFG, 0x0247, 0x01), (SEC_HW_MAIN_CFG, 0x0287, 0x02),
    (SEC_HW_MAIN_CFG, 0x02C3, 0x48), (SEC_HW_MAIN_CFG, 0x02C5, 0x82),
    (SEC_HW_MAIN_CFG, 0x02C7, 0x50), (SEC_HW_MAIN_CFG, 0x02C9, 0x8C),
    (SEC_HW_MAIN_CFG, 0x02CB, 0x50), (SEC_HW_MAIN_CFG, 0x02CC, 0x41),
    (SEC_HW_MAIN_CFG, 0x02CD, 0x84), (SEC_HW_MAIN_CFG, 0x02D7, 0x00),
    (SEC_HW_MAIN_CFG, 0x02E1, 0x00), (SEC_HW_MAIN_CFG, 0x02E3, 0x00),
    (SEC_HW_MAIN_CFG, 0x02E5, 0x00), (SEC_HW_MAIN_CFG, 0x02E7, 0x00),
    (SEC_HW_MAIN_CFG, 0x02E8, 0x00), (SEC_HW_MAIN_CFG, 0x02E9, 0x00),
    (SEC_HW_MAIN_CFG, 0x0344, 0x0F), (SEC_HW_MAIN_CFG, 0x0345, 0xFF),
    (SEC_HW_MAIN_CFG, 0x0373, 0x0A), (SEC_HW_MAIN_CFG, 0x0385, 0x06),
    (SEC_HW_MAIN_CFG, 0x0387, 0x0E), (SEC_HW_MAIN_CFG, 0x03A0, 0x0F),
    (SEC_HW_MAIN_CFG, 0x03A1, 0xFF), (SEC_HW_MAIN_CFG, 0x03A6, 0x10),
    (SEC_HW_MAIN_CFG, 0x03A7, 0x01), (SEC_HW_MAIN_CFG, 0x03A8, 0x10),
    (SEC_HW_MAIN_CFG, 0x03A9, 0x03), (SEC_HW_MAIN_CFG, 0x03AA, 0x10),
    (SEC_HW_MAIN_CFG, 0x03AB, 0x2A), (SEC_HW_MAIN_CFG, 0x03AC, 0x10),
    (SEC_HW_MAIN_CFG, 0x03AD, 0x2C), (SEC_HW_MAIN_CFG, 0x03EC, 0x10),
    (SEC_HW_MAIN_CFG, 0x03ED, 0x24), (SEC_HW_MAIN_CFG, 0x03EE, 0x10),
    (SEC_HW_MAIN_CFG, 0x03EF, 0x04), (SEC_HW_MAIN_CFG, 0x03F6, 0x0F),
    (SEC_HW_MAIN_CFG, 0x03F7, 0xFF), (SEC_HW_MAIN_CFG, 0x0546, 0x9E),
    (SEC_HW_MAIN_CFG, 0x0549, 0x00), (SEC_HW_MAIN_CFG, 0x054A, 0x91),
    (SEC_HW_MAIN_CFG, 0x054E, 0x9E), (SEC_HW_MAIN_CFG, 0x0564, 0x98),
    (SEC_HW_MAIN_CFG, 0x0569, 0x00), (SEC_HW_MAIN_CFG, 0x056A, 0x98),
    (SEC_HW_MAIN_CFG, 0x056C, 0x98), (SEC_HW_MAIN_CFG, 0x086C, 0x3E),
    (SEC_HW_MAIN_CFG, 0x086D, 0x26), (SEC_HW_MAIN_CFG, 0x086E, 0x3E),
    (SEC_HW_MAIN_CFG, 0x086F, 0x26),
    (SEC_HW_BOOT_CFG, 0x0000, 0x70), (SEC_HW_BOOT_CFG, 0x002B, 0x3B),
    (SEC_HW_BOOT_CFG, 0x0084, 0x5B),
]

OEM_PROFILES = {
    "dell": {
        "name": "Dell OEM (0V5DG9 / 0TDNNT)",
        "detect_psid_prefix": "DEL",
        "detect_subsystem_id": 0x91,
        "profile": DELL_OEM_PROFILE,
    },
}


# ─── Mellanox CRC-16 ─────────────────────────────────────────────────────────

def mlx_crc16(data: bytes) -> int:
    """Calculate Mellanox CRC-16 over firmware section data.
    From mstflint: https://github.com/Mellanox/mstflint/blob/master/mft_utils/crc16.cpp
    Polynomial 0x100B, init 0xFFFF, big-endian 32-bit words,
    16-bit zero-flush finalization, final XOR 0xFFFF."""
    if len(data) % 4 != 0:
        raise ValueError(f"Data must be 4-byte aligned, got {len(data)} bytes")
    crc = 0xFFFF
    for i in range(0, len(data), 4):
        word = struct.unpack_from(">I", data, i)[0]
        for _ in range(32):
            if crc & 0x8000:
                crc = (((crc << 1) | (word >> 31)) ^ 0x100B) & 0xFFFF
            else:
                crc = ((crc << 1) | (word >> 31)) & 0xFFFF
            word = (word << 1) & 0xFFFFFFFF
    for _ in range(16):
        if crc & 0x8000:
            crc = ((crc << 1) ^ 0x100B) & 0xFFFF
        else:
            crc = (crc << 1) & 0xFFFF
    return crc ^ 0xFFFF


# ─── FS4 Image Parser ────────────────────────────────────────────────────────

@dataclass
class ITOCEntry:
    type_id: int
    type_name: str
    size: int
    flash_addr: int
    crc16: int
    entry_crc16: int
    entry_offset: int

    @property
    def end_addr(self):
        return self.flash_addr + self.size


def parse_itoc(data: bytes) -> list[ITOCEntry]:
    entries = []
    header = data[ITOC_OFFSET:ITOC_OFFSET + 4]
    if header != b'ITOC':
        raise ValueError(
            f"No ITOC header at 0x{ITOC_OFFSET:06X} "
            f"(found: {header.hex()}). Not an FS4 firmware image.")
    entry_base = ITOC_OFFSET + ITOC_HEADER_SIZE
    for i in range(64):
        pos = entry_base + i * ITOC_ENTRY_SIZE
        entry = data[pos:pos + ITOC_ENTRY_SIZE]
        if all(b == 0xFF for b in entry) or all(b == 0x00 for b in entry):
            continue
        type_id = entry[0]
        size = (entry[1] << 16) | (entry[2] << 8) | entry[3]
        flash_addr = struct.unpack_from(">I", entry, 20)[0]
        crc16 = struct.unpack_from(">H", entry, 26)[0]
        entry_crc16 = struct.unpack_from(">H", entry, 30)[0]
        entries.append(ITOCEntry(
            type_id=type_id,
            type_name=SECTION_TYPES.get(type_id, f"UNKNOWN_0x{type_id:02X}"),
            size=size, flash_addr=flash_addr,
            crc16=crc16, entry_crc16=entry_crc16, entry_offset=pos))
    return entries


def find_section(entries: list[ITOCEntry], type_id: int) -> Optional[ITOCEntry]:
    for entry in entries:
        if entry.type_id == type_id:
            return entry
    return None


# ─── CRC Fix ─────────────────────────────────────────────────────────────────

def fix_all_crcs(data: bytearray, entries: list[ITOCEntry]) -> int:
    fixed = 0
    for entry in entries:
        if entry.type_id in (0xA0, 0xA3):
            continue
        correct = mlx_crc16(data[entry.flash_addr:entry.end_addr])
        stored = struct.unpack_from(">H", data, entry.entry_offset + 26)[0]
        if stored != correct:
            struct.pack_into(">H", data, entry.entry_offset + 26, correct)
            fixed += 1
        correct = mlx_crc16(data[entry.entry_offset:entry.entry_offset + 28])
        stored = struct.unpack_from(">H", data, entry.entry_offset + 30)[0]
        if stored != correct:
            struct.pack_into(">H", data, entry.entry_offset + 30, correct)
            fixed += 1
    return fixed


# ─── Firmware Analysis ────────────────────────────────────────────────────────

KNOWN_SKIP_REASONS = {
    0x00: "port disabled (single-port card)",
    0x06: "QSFP28 port config (Gen3/Gen4 use same value)",
}

@dataclass
class FirmwareInfo:
    device_id: int
    subsystem_id: int
    psid: str
    oem_key: Optional[str]
    is_gen4_already: bool
    port1_gen: int
    port2_gen: int
    connector_type: str  # "SFP28", "QSFP28", or "unknown"
    port_count: int      # 1 or 2


def analyze_firmware(data: bytes, entries: list[ITOCEntry]) -> FirmwareInfo:
    fw_boot = find_section(entries, SEC_FW_BOOT_CFG)
    hw_main = find_section(entries, SEC_HW_MAIN_CFG)
    hw_boot = find_section(entries, SEC_HW_BOOT_CFG)
    image_info = find_section(entries, SEC_IMAGE_INFO)

    if not all([fw_boot, hw_main, hw_boot, image_info]):
        raise ValueError("Missing required firmware sections")

    device_id = 0x1000 | data[fw_boot.flash_addr + 0x002D]
    subsystem_id = data[fw_boot.flash_addr + 0x003D]

    psid_raw = data[image_info.flash_addr + 0x24:image_info.flash_addr + 0x3C]
    psid = psid_raw.split(b'\x00')[0].decode('ascii', errors='replace')

    oem_key = None
    for key, profile_info in OEM_PROFILES.items():
        if psid.startswith(profile_info["detect_psid_prefix"]):
            oem_key = key
            break
        if subsystem_id == profile_info.get("detect_subsystem_id"):
            oem_key = key
            break

    port1_gen = data[hw_main.flash_addr + 0x0245]
    port2_gen = data[hw_main.flash_addr + 0x0285]
    cap_index = data[fw_boot.flash_addr + 0x0093]
    max_speed = data[hw_boot.flash_addr + 0x0023]

    # Detect connector type from port gen byte patterns
    if port1_gen in (0x01, 0x04):
        connector_type = "SFP28"
    elif port1_gen == 0x06:
        connector_type = "QSFP28"
    else:
        connector_type = "unknown"

    port_count = 2 if port2_gen != 0x00 else 1

    is_gen4 = (cap_index == 0x47 and max_speed in (0x0F, 0x8F))

    return FirmwareInfo(
        device_id, subsystem_id, psid, oem_key, is_gen4,
        port1_gen, port2_gen, connector_type, port_count)


def print_analysis(info: FirmwareInfo, data: bytes, entries: list[ITOCEntry]):
    """Print detailed firmware analysis."""
    id_str = {0x1017: "CX5-EN", 0x1019: "CX5-Ex"}.get(info.device_id, f"0x{info.device_id:04X}")
    oem_name = OEM_PROFILES[info.oem_key]["name"] if info.oem_key else "Stock Mellanox"

    fw_boot = find_section(entries, SEC_FW_BOOT_CFG)
    hw_main = find_section(entries, SEC_HW_MAIN_CFG)
    hw_boot = find_section(entries, SEC_HW_BOOT_CFG)

    print(f"\n  Device ID:      0x{info.device_id:04X} ({id_str})")
    print(f"  PSID:           {info.psid}")
    print(f"  OEM:            {oem_name}")
    print(f"  Connector:      {info.connector_type}")
    print(f"  Port count:     {info.port_count}")
    print(f"  Gen4 status:    {'Enabled' if info.is_gen4_already else 'Disabled (Gen3)'}")
    print(f"\n  Gen4 field values:")
    print(f"    cap_index  (FW_BOOT+0x0093): 0x{data[fw_boot.flash_addr+0x0093]:02X}  {'← Gen4' if data[fw_boot.flash_addr+0x0093]==0x47 else '← Gen3'}")
    print(f"    max_speed  (HW_BOOT+0x0023): 0x{data[hw_boot.flash_addr+0x0023]:02X}  {'← Gen4' if data[hw_boot.flash_addr+0x0023] in (0x0F,0x8F) else '← Gen3'}")
    print(f"    port1_gen  (HW_MAIN+0x0245): 0x{info.port1_gen:02X}  {KNOWN_SKIP_REASONS.get(info.port1_gen, '')}")
    print(f"    port2_gen  (HW_MAIN+0x0285): 0x{info.port2_gen:02X}  {KNOWN_SKIP_REASONS.get(info.port2_gen, '')}")

    st0 = (data[hw_main.flash_addr+0x0404] << 8) | data[hw_main.flash_addr+0x0405]
    st1 = (data[hw_main.flash_addr+0x0406] << 8) | data[hw_main.flash_addr+0x0407]
    print(f"    speed_tbl0 (HW_MAIN+0x0404): 0x{st0:04X}  {'← already invalidated' if st0==0x0FFF else ''}")
    print(f"    speed_tbl1 (HW_MAIN+0x0406): 0x{st1:04X}  {'← already invalidated' if st1==0x0FFF else ''}")


# ─── OEM Profile Application ─────────────────────────────────────────────────

def apply_oem_profile(data, entries, profile, verbose=False):
    applied = 0
    for sec_type, offset, value in profile:
        section = find_section(entries, sec_type)
        if section is None:
            continue
        abs_offset = section.flash_addr + offset
        if abs_offset >= len(data):
            continue
        old_val = data[abs_offset]
        if old_val != value:
            data[abs_offset] = value
            applied += 1
            if verbose:
                sec_name = SECTION_TYPES.get(sec_type, f"0x{sec_type:02X}")
                print(f"    {sec_name}+0x{offset:04X}: 0x{old_val:02X} -> 0x{value:02X}")
    return applied


# ─── Gen4 Patch Application (Universal) ──────────────────────────────────────

def apply_patches(data, entries, patches, force=False, verbose=False):
    """Apply Gen4 patches with intelligent per-field handling.

    For each field:
      - If at gen3_value → patch to gen4_value
      - If at gen4_value → skip (already patched)
      - If in skip_values → skip (e.g., disabled port, QSFP28 port gen)
      - Otherwise → warn (unknown value) unless --force
    """
    changed = 0
    skipped = 0
    warnings = []

    for patch in patches:
        section = find_section(entries, patch.section_type)
        if section is None:
            raise ValueError(
                f"Section {SECTION_TYPES.get(patch.section_type, '??')} "
                f"not found for {patch.name}")

        abs_offset = section.flash_addr + patch.section_offset
        if abs_offset >= len(data):
            raise ValueError(f"Offset 0x{abs_offset:06X} for {patch.name} beyond image size")

        current = data[abs_offset]

        # Already at Gen4 value
        if current == patch.gen4_value:
            if verbose:
                print(f"    {patch.name}: already at Gen4 (0x{current:02X}), skipping")
            skipped += 1
            continue

        # Known skip value (disabled port, QSFP28 port gen, etc.)
        if current in patch.skip_values:
            reason = KNOWN_SKIP_REASONS.get(current, f"recognized value 0x{current:02X}")
            if verbose:
                print(f"    {patch.name}: {reason}, skipping")
            skipped += 1
            continue

        # At expected Gen3 value — patch it
        if current == patch.gen3_value:
            data[abs_offset] = patch.gen4_value
            changed += 1
            if verbose:
                print(f"    {patch.name}: 0x{current:02X} -> 0x{patch.gen4_value:02X}")
            continue

        # Unknown value
        if force:
            data[abs_offset] = patch.gen4_value
            changed += 1
            warnings.append(
                f"{patch.name}: overrode unknown value 0x{current:02X} -> "
                f"0x{patch.gen4_value:02X} (--force)")
        else:
            warnings.append(
                f"{patch.name} @ 0x{abs_offset:06X}: unexpected value 0x{current:02X} "
                f"(expected Gen3=0x{patch.gen3_value:02X}). Use --force to override.")

    return changed, skipped, warnings


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=f"ConnectX-5 PCIe Gen4 Enabler v{VERSION} — Universal Edition",
        epilog="IMPORTANT: Back up firmware before flashing! "
               "Commands like flint/mstflint require root/admin privileges "
               "(use sudo on Linux, run as Administrator on Windows).")
    parser.add_argument("--input", "-i", required=True,
                        help="Input firmware image (.bin)")
    parser.add_argument("--output", "-o",
                        help="Output patched firmware image (.bin)")
    parser.add_argument("--upgrade-base", metavar="LTS_IMAGE",
                        help="Stock Mellanox LTS image as new base (OEM upgrade mode)")
    parser.add_argument("--analyze", "-a", action="store_true",
                        help="Analyze firmware and show Gen4 field values (no patching)")
    parser.add_argument("--force", "-f", action="store_true",
                        help="Apply patches even if values don't match expected stock")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Show what would change without writing output")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed patch decisions per field")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")

    args = parser.parse_args()

    if not args.analyze and not args.output:
        parser.error("--output is required unless using --analyze")

    if not os.path.exists(args.input):
        print(f"Error: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    with open(args.input, "rb") as f:
        raw_input = f.read()

    if len(raw_input) < 0x100000:
        print(f"Error: File too small ({len(raw_input)} bytes).", file=sys.stderr)
        sys.exit(1)

    print(f"ConnectX-5 Gen4 Enabler v{VERSION}")
    print(f"Parsing {args.input} ({len(raw_input):,} bytes)...")
    input_entries = parse_itoc(raw_input)
    input_info = analyze_firmware(raw_input, input_entries)

    # ── Analyze mode ──
    if args.analyze:
        print_analysis(input_info, raw_input, input_entries)
        sys.exit(0)

    print_analysis(input_info, raw_input, input_entries)

    if input_info.device_id not in (0x1017, 0x1019):
        print(f"\n  Warning: Unexpected Device ID 0x{input_info.device_id:04X}.", file=sys.stderr)
        if not args.force:
            print("  Use --force to proceed anyway.", file=sys.stderr)
            sys.exit(1)

    # ── Decide mode: direct patch vs upgrade-base ──
    if args.upgrade_base:
        if not os.path.exists(args.upgrade_base):
            print(f"Error: Base image not found: {args.upgrade_base}", file=sys.stderr)
            sys.exit(1)

        if not input_info.oem_key:
            print(f"\nError: Could not detect OEM vendor from input firmware.", file=sys.stderr)
            print(f"  Supported OEM profiles: {', '.join(OEM_PROFILES.keys())}", file=sys.stderr)
            sys.exit(1)

        profile_info = OEM_PROFILES[input_info.oem_key]

        with open(args.upgrade_base, "rb") as f:
            raw_base = f.read()

        print(f"\nParsing base image {args.upgrade_base}...")
        base_entries = parse_itoc(raw_base)
        base_info = analyze_firmware(raw_base, base_entries)
        print(f"  Base PSID: {base_info.psid}")

        if base_info.oem_key:
            print(f"  WARNING: Base appears to be OEM, not stock Mellanox.", file=sys.stderr)
            if not args.force:
                sys.exit(1)

        data = bytearray(raw_base)
        entries = base_entries

        print(f"\nApplying {profile_info['name']} profile ({len(profile_info['profile'])} customizations)...")
        oem_applied = apply_oem_profile(data, entries, profile_info["profile"], args.verbose)
        print(f"  Applied {oem_applied} OEM customization bytes")

        print(f"\nApplying Gen4 patches...")
        changed, skipped, warnings = apply_patches(data, entries, GEN4_PATCHES, args.force, args.verbose)
        for w in warnings:
            print(f"  WARNING: {w}")
        print(f"  Changed {changed} bytes, skipped {skipped} (already correct or N/A)")

    else:
        if input_info.is_gen4_already and not args.force:
            print("\nFirmware already has Gen4 enabled. Use --force to re-apply.")
            sys.exit(0)

        data = bytearray(raw_input)
        entries = input_entries

        print(f"\nApplying Gen4 patches...")
        changed, skipped, warnings = apply_patches(data, entries, GEN4_PATCHES, args.force, args.verbose)
        for w in warnings:
            print(f"  WARNING: {w}")
        print(f"  Changed {changed} bytes, skipped {skipped} (already correct or N/A)")

        if changed == 0 and not warnings and not args.force:
            print("Nothing to change — firmware may already be Gen4.")
            sys.exit(0)

    if args.dry_run:
        print("\nDry run — no output written.")
        sys.exit(0)

    print("Recalculating CRCs...")
    crc_fixes = fix_all_crcs(data, entries)
    print(f"  Updated {crc_fixes} CRC values")

    with open(args.output, "wb") as f:
        f.write(data)

    mode_desc = "upgraded + patched" if args.upgrade_base else "patched"
    print(f"\n{'='*60}")
    print(f"  {mode_desc.upper()} firmware written to {args.output}")
    print(f"{'='*60}")
    print(f"\nNext steps:")
    print(f"  1. Verify:      mstflint -i {args.output} verify")
    print(f"  2. Flash:       sudo flint -d mt4119_pciconf0 -i {args.output} --skip_ci_req burn")
    print(f"     NOTE: If you see a signature/digest error, the card needs")
    print(f"           FNP recovery mode to bypass secure boot. See README.")
    print(f"  3. FULL power cycle (power off, not reboot)")
    print(f"  4. Verify:      sudo mlxlink -d mt4119_pciconf0")
    print(f"                  lspci -vvs <device> | grep -i lnksta")


if __name__ == "__main__":
    main()
