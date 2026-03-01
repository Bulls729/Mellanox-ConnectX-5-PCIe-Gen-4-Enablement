#!/usr/bin/env python3
"""
ConnectX-5 PCIe Gen4 Enabler

Patches a user-supplied ConnectX-5 EN firmware image to enable PCIe Gen4 (16 GT/s).
Locates config fields via FS4 ITOC parsing (firmware-version-independent).

Optionally upgrades OEM firmware to a newer stock Mellanox LTS base while
preserving vendor-specific board tuning (Dell OEM profile built-in).

No external dependencies — uses native Mellanox CRC-16 (poly 0x100B) from the
open-source mstflint project (https://github.com/Mellanox/mstflint).

Usage:
    # Patch existing firmware in-place:
    python3 cx5_gen4_enable.py -i firmware.bin -o patched.bin

    # Upgrade Dell OEM to latest LTS base + Gen4:
    python3 cx5_gen4_enable.py -i dell_oem.bin -o patched.bin --upgrade-base acat_8002.bin
"""

import argparse
import struct
import sys
import os
from dataclasses import dataclass
from typing import Optional


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

@dataclass
class PatchField:
    """A single byte to patch, defined by section-relative offset."""
    section_type: int
    section_offset: int
    stock_value: int
    gen4_value: int
    name: str
    description: str

# The 8 bytes that enable PCIe Gen4 on ConnectX-5 EN.
# Verified working on Dell OEM (0V5DG9), HPE OEM, and stock Mellanox ACAT.
# No Device ID change required — Gen4 is not gated behind Device ID.
CORE_GEN4_PATCHES = [
    PatchField(SEC_HW_MAIN_CFG, 0x0245, 0x01, 0x04,
               "port1_pcie_gen",
               "Port 1 PCIe generation target (0x01=Gen3, 0x04=Gen4)"),
    PatchField(SEC_HW_MAIN_CFG, 0x0285, 0x01, 0x04,
               "port2_pcie_gen",
               "Port 2 PCIe generation target"),
    PatchField(SEC_FW_BOOT_CFG, 0x0093, 0x45, 0x47,
               "pcie_cap_index",
               "PCIe capability advertisement index (0x45=Gen3, 0x47=Gen4)"),
    PatchField(SEC_HW_MAIN_CFG, 0x0404, 0x00, 0x0F,
               "speed_table_0020_hi",
               "Invalidate Gen3-only speed profile 0x0020 (high byte)"),
    PatchField(SEC_HW_MAIN_CFG, 0x0405, 0x20, 0xFF,
               "speed_table_0020_lo",
               "Invalidate Gen3-only speed profile 0x0020 (low byte)"),
    PatchField(SEC_HW_MAIN_CFG, 0x0406, 0x00, 0x0F,
               "speed_table_0021_hi",
               "Invalidate Gen3-only speed profile 0x0021 (high byte)"),
    PatchField(SEC_HW_MAIN_CFG, 0x0407, 0x21, 0xFF,
               "speed_table_0021_lo",
               "Invalidate Gen3-only speed profile 0x0021 (low byte)"),
    PatchField(SEC_HW_BOOT_CFG, 0x0023, 0x07, 0x0F,
               "pcie_max_speed_supported",
               "pcie_cfg.pcie_max_speed_supported — link training mode (0x07=Gen3, 0x0F=Gen4)"),
]


# ─── OEM Profiles ────────────────────────────────────────────────────────────
#
# Each OEM profile is a list of (section_type, offset, value) tuples
# representing the vendor-specific customizations that differ from stock
# Mellanox ACAT firmware. Derived by binary diffing OEM images against
# their stock Mellanox counterparts at the same firmware version.
#
# These are applied to a stock Mellanox LTS base image when using
# --upgrade-base to produce a vendor-tuned image at the latest firmware.

# Dell OEM Profile (0V5DG9 / 0TDNNT)
# Derived from: Dell 16.35.4554 vs ACAT 16.35.4554
# Covers: PHY EQ coefficients, PCIe port mode (Socket Direct 2x8),
#         board power budget (15.9W), SFP link tuning, PHY calibration,
#         speed table profiles, boot config, and subsystem ID.
# Identity strings (PSID, name, description) are NOT included —
# those remain from the base image.
DELL_OEM_PROFILE = [
    # FW_BOOT_CFG — boot-time PCIe and NV config
    (SEC_FW_BOOT_CFG, 0x0018, 0x90),  # boot config
    (SEC_FW_BOOT_CFG, 0x001C, 0x28),  # boot config
    (SEC_FW_BOOT_CFG, 0x003D, 0x91),  # subsystem_id (Dell = 0x91)
    (SEC_FW_BOOT_CFG, 0x011C, 0xBF),  # PCIe subsystem config
    (SEC_FW_BOOT_CFG, 0x0120, 0x20),  # PCIe subsystem config
    (SEC_FW_BOOT_CFG, 0x0131, 0x02),  # PCIe subsystem config
    (SEC_FW_BOOT_CFG, 0x02BC, 0x07),  # misc config
    # FW_MAIN_CFG — PHY, link, SFP settings
    (SEC_FW_MAIN_CFG, 0x0023, 0x12),  # FW config misc
    (SEC_FW_MAIN_CFG, 0x004C, 0x00),  # FW config misc
    (SEC_FW_MAIN_CFG, 0x0221, 0x10),  # port/link config
    (SEC_FW_MAIN_CFG, 0x0227, 0x40),  # port/link config
    (SEC_FW_MAIN_CFG, 0x08A8, 0xA0),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0938, 0xA0),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x09BC, 0x83),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x09C1, 0x00),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x09CC, 0x00),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A01, 0x40),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A0C, 0x02),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A10, 0x83),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A12, 0x06),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A13, 0x21),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A14, 0xA0),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A15, 0x00),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A17, 0x01),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A1B, 0x10),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A1C, 0x00),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A1F, 0x00),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A20, 0xCD),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A22, 0x8A),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A2D, 0x03),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A2E, 0x19),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A30, 0x82),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A33, 0x04),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A36, 0x01),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A37, 0x01),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A38, 0x3C),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A39, 0x60),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A3C, 0x82),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A3F, 0x04),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A40, 0xC3),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A42, 0x01),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A43, 0x96),  # SFP link tuning
    (SEC_FW_MAIN_CFG, 0x0A44, 0x80),  # SFP link tuning
    # HW_MAIN_CFG — PCIe port mode, PHY EQ, speed tables, PHY tuning
    (SEC_HW_MAIN_CFG, 0x0247, 0x01),  # PCIe port width/mode (Socket Direct)
    (SEC_HW_MAIN_CFG, 0x0287, 0x02),  # PCIe port width/mode (Socket Direct)
    (SEC_HW_MAIN_CFG, 0x02C3, 0x48),  # PHY EQ coefficients (Gen4-tuned)
    (SEC_HW_MAIN_CFG, 0x02C5, 0x82),  # PHY EQ coefficients
    (SEC_HW_MAIN_CFG, 0x02C7, 0x50),  # PHY EQ coefficients
    (SEC_HW_MAIN_CFG, 0x02C9, 0x8C),  # PHY EQ coefficients
    (SEC_HW_MAIN_CFG, 0x02CB, 0x50),  # PHY EQ coefficients
    (SEC_HW_MAIN_CFG, 0x02CC, 0x41),  # PHY EQ coefficients
    (SEC_HW_MAIN_CFG, 0x02CD, 0x84),  # PHY EQ coefficients
    (SEC_HW_MAIN_CFG, 0x02D7, 0x00),  # PHY EQ coefficients (port 2)
    (SEC_HW_MAIN_CFG, 0x02E1, 0x00),  # PHY EQ coefficients (port 2)
    (SEC_HW_MAIN_CFG, 0x02E3, 0x00),  # PHY EQ coefficients (port 2)
    (SEC_HW_MAIN_CFG, 0x02E5, 0x00),  # PHY EQ coefficients (port 2)
    (SEC_HW_MAIN_CFG, 0x02E7, 0x00),  # PHY EQ coefficients (port 2)
    (SEC_HW_MAIN_CFG, 0x02E8, 0x00),  # PHY EQ coefficients (port 2)
    (SEC_HW_MAIN_CFG, 0x02E9, 0x00),  # PHY EQ coefficients (port 2)
    (SEC_HW_MAIN_CFG, 0x0344, 0x0F),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x0345, 0xFF),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x0373, 0x0A),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x0385, 0x06),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x0387, 0x0E),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x03A0, 0x0F),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x03A1, 0xFF),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x03A6, 0x10),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x03A7, 0x01),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x03A8, 0x10),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x03A9, 0x03),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x03AA, 0x10),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x03AB, 0x2A),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x03AC, 0x10),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x03AD, 0x2C),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x03EC, 0x10),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x03ED, 0x24),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x03EE, 0x10),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x03EF, 0x04),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x03F6, 0x0F),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x03F7, 0xFF),  # speed table profiles
    (SEC_HW_MAIN_CFG, 0x0546, 0x9E),  # PHY tuning
    (SEC_HW_MAIN_CFG, 0x0549, 0x00),  # PHY tuning
    (SEC_HW_MAIN_CFG, 0x054A, 0x91),  # PHY tuning
    (SEC_HW_MAIN_CFG, 0x054E, 0x9E),  # PHY tuning
    (SEC_HW_MAIN_CFG, 0x0564, 0x98),  # PHY tuning
    (SEC_HW_MAIN_CFG, 0x0569, 0x00),  # PHY tuning
    (SEC_HW_MAIN_CFG, 0x056A, 0x98),  # PHY tuning
    (SEC_HW_MAIN_CFG, 0x056C, 0x98),  # PHY tuning
    (SEC_HW_MAIN_CFG, 0x086C, 0x3E),  # PHY calibration
    (SEC_HW_MAIN_CFG, 0x086D, 0x26),  # PHY calibration
    (SEC_HW_MAIN_CFG, 0x086E, 0x3E),  # PHY calibration
    (SEC_HW_MAIN_CFG, 0x086F, 0x26),  # PHY calibration
    # HW_BOOT_CFG — boot-time hardware config
    (SEC_HW_BOOT_CFG, 0x0000, 0x70),  # boot config
    (SEC_HW_BOOT_CFG, 0x002B, 0x3B),  # boot config
    (SEC_HW_BOOT_CFG, 0x0084, 0x5B),  # boot config
]

OEM_PROFILES = {
    "dell": {
        "name": "Dell OEM (0V5DG9 / 0TDNNT)",
        "detect_psid_prefix": "DEL",
        "detect_subsystem_id": 0x91,
        "profile": DELL_OEM_PROFILE,
    },
    # Future profiles can be added here:
    # "hpe": { ... },
    # "lenovo": { ... },
}


# ─── Mellanox CRC-16 ─────────────────────────────────────────────────────────
#
# From mstflint: https://github.com/Mellanox/mstflint/blob/master/mft_utils/crc16.cpp
# Polynomial 0x100B, init 0xFFFF, big-endian 32-bit words,
# 16-bit zero-flush finalization, final XOR 0xFFFF.

def mlx_crc16(data: bytes) -> int:
    """Calculate Mellanox CRC-16 over firmware section data."""
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
    """Parse the FS4 ITOC to find all firmware sections."""
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
    """Recalculate all section CRCs and ITOC entry CRCs. Returns count of fixes."""
    fixed = 0
    for entry in entries:
        if entry.type_id in (0xA0, 0xA3):  # signature sections — CRC ignored
            continue

        # Section data CRC
        correct = mlx_crc16(data[entry.flash_addr:entry.end_addr])
        stored = struct.unpack_from(">H", data, entry.entry_offset + 26)[0]
        if stored != correct:
            struct.pack_into(">H", data, entry.entry_offset + 26, correct)
            fixed += 1

        # ITOC entry CRC (over bytes 0-27 of the 32-byte entry)
        correct = mlx_crc16(data[entry.entry_offset:entry.entry_offset + 28])
        stored = struct.unpack_from(">H", data, entry.entry_offset + 30)[0]
        if stored != correct:
            struct.pack_into(">H", data, entry.entry_offset + 30, correct)
            fixed += 1

    return fixed


# ─── Firmware Analysis ────────────────────────────────────────────────────────

@dataclass
class FirmwareInfo:
    device_id: int
    subsystem_id: int
    psid: str
    oem_key: Optional[str]  # "dell", "hpe", etc. or None
    is_gen4_already: bool


def analyze_firmware(data: bytes, entries: list[ITOCEntry]) -> FirmwareInfo:
    fw_boot_cfg = find_section(entries, SEC_FW_BOOT_CFG)
    hw_main_cfg = find_section(entries, SEC_HW_MAIN_CFG)
    image_info = find_section(entries, SEC_IMAGE_INFO)

    if not all([fw_boot_cfg, hw_main_cfg, image_info]):
        raise ValueError("Missing required firmware sections")

    device_id = 0x1000 | data[fw_boot_cfg.flash_addr + 0x002D]
    subsystem_id = data[fw_boot_cfg.flash_addr + 0x003D]

    psid_raw = data[image_info.flash_addr + 0x24:image_info.flash_addr + 0x3C]
    psid = psid_raw.split(b'\x00')[0].decode('ascii', errors='replace')

    # Detect OEM
    oem_key = None
    for key, profile_info in OEM_PROFILES.items():
        if psid.startswith(profile_info["detect_psid_prefix"]):
            oem_key = key
            break
        if subsystem_id == profile_info.get("detect_subsystem_id"):
            oem_key = key
            break

    is_gen4 = data[hw_main_cfg.flash_addr + 0x0245] == 0x04

    return FirmwareInfo(device_id, subsystem_id, psid, oem_key, is_gen4)


# ─── OEM Profile Application ─────────────────────────────────────────────────

def apply_oem_profile(
    data: bytearray,
    entries: list[ITOCEntry],
    profile: list[tuple],
    verbose: bool = False,
) -> int:
    """Apply an OEM profile to a stock Mellanox base image.

    Each profile entry is (section_type, offset, value).
    Returns the number of bytes written.
    """
    applied = 0
    for sec_type, offset, value in profile:
        section = find_section(entries, sec_type)
        if section is None:
            sec_name = SECTION_TYPES.get(sec_type, f"0x{sec_type:02X}")
            print(f"  WARNING: Section {sec_name} not found, skipping profile "
                  f"entry at +0x{offset:04X}", file=sys.stderr)
            continue

        abs_offset = section.flash_addr + offset
        if abs_offset >= len(data):
            print(f"  WARNING: Offset 0x{abs_offset:06X} beyond image, skipping",
                  file=sys.stderr)
            continue

        old_val = data[abs_offset]
        if old_val != value:
            data[abs_offset] = value
            applied += 1
            if verbose:
                sec_name = SECTION_TYPES.get(sec_type, f"0x{sec_type:02X}")
                print(f"    {sec_name}+0x{offset:04X}: 0x{old_val:02X} -> 0x{value:02X}")

    return applied


# ─── Gen4 Patch Application ──────────────────────────────────────────────────

def apply_patches(data, entries, patches, force=False):
    changed = 0
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

        if current == patch.gen4_value:
            if not force:
                warnings.append(
                    f"{patch.name} already at Gen4 value 0x{patch.gen4_value:02X} "
                    f"@ 0x{abs_offset:06X}")
            continue

        if current != patch.stock_value and not force:
            warnings.append(
                f"{patch.name} @ 0x{abs_offset:06X}: expected stock 0x{patch.stock_value:02X}, "
                f"found 0x{current:02X}. Use --force to override.")
            continue

        data[abs_offset] = patch.gen4_value
        changed += 1

    return changed, warnings


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Enable PCIe Gen4 on ConnectX-5 EN firmware images",
        epilog="Back up your firmware before flashing! "
               "Use: flint -d mt4119_pciconf0 ri backup.bin")
    parser.add_argument("--input", "-i", required=True,
                        help="Input firmware image (.bin)")
    parser.add_argument("--output", "-o", required=True,
                        help="Output patched firmware image (.bin)")
    parser.add_argument("--upgrade-base", metavar="LTS_IMAGE",
                        help="Stock Mellanox LTS firmware image to use as the new base. "
                             "OEM customizations from --input are applied to this base, "
                             "then Gen4 patches are applied on top.")
    parser.add_argument("--force", "-f", action="store_true",
                        help="Apply patches even if values don't match expected stock")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Show what would change without writing output")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed section and patch information")

    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"Error: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    with open(args.input, "rb") as f:
        raw_input = f.read()

    if len(raw_input) < 0x100000:
        print(f"Error: File too small ({len(raw_input)} bytes).", file=sys.stderr)
        sys.exit(1)

    # ── Parse input image ──
    print(f"Parsing {args.input} ({len(raw_input):,} bytes)...")
    input_entries = parse_itoc(raw_input)
    input_info = analyze_firmware(raw_input, input_entries)

    id_str = {0x1017: "CX5-EN", 0x1019: "CX5-Ex"}.get(input_info.device_id, "UNKNOWN")
    oem_name = OEM_PROFILES[input_info.oem_key]["name"] if input_info.oem_key else "Stock Mellanox"

    print(f"\nInput firmware analysis:")
    print(f"  Device ID:    0x{input_info.device_id:04X} ({id_str})")
    print(f"  PSID:         {input_info.psid}")
    print(f"  Type:         {oem_name}")
    print(f"  Gen4 already: {'Yes' if input_info.is_gen4_already else 'No'}")

    if input_info.device_id not in (0x1017, 0x1019):
        print(f"\nWarning: Unexpected Device ID 0x{input_info.device_id:04X}.", file=sys.stderr)
        if not args.force:
            sys.exit(1)

    # ── Decide mode: direct patch vs upgrade-base ──
    if args.upgrade_base:
        # ── Upgrade mode ──
        if not os.path.exists(args.upgrade_base):
            print(f"Error: Base image not found: {args.upgrade_base}", file=sys.stderr)
            sys.exit(1)

        if not input_info.oem_key:
            print(f"\nError: Could not detect OEM vendor from input firmware.", file=sys.stderr)
            print(f"  Detected PSID: {input_info.psid}", file=sys.stderr)
            print(f"  Supported OEM profiles: {', '.join(OEM_PROFILES.keys())}", file=sys.stderr)
            print(f"\nThe --upgrade-base feature requires a recognized OEM firmware as input",
                  file=sys.stderr)
            print(f"so the tool knows which vendor customizations to apply.", file=sys.stderr)
            sys.exit(1)

        profile_info = OEM_PROFILES[input_info.oem_key]

        with open(args.upgrade_base, "rb") as f:
            raw_base = f.read()

        print(f"\nParsing base image {args.upgrade_base} ({len(raw_base):,} bytes)...")
        base_entries = parse_itoc(raw_base)
        base_info = analyze_firmware(raw_base, base_entries)

        print(f"  Base PSID:    {base_info.psid}")
        print(f"  Base type:    {'Stock Mellanox' if not base_info.oem_key else 'OEM (unexpected)'}")

        if base_info.oem_key:
            print(f"\n  WARNING: Base image appears to be OEM firmware, not stock Mellanox.",
                  file=sys.stderr)
            print(f"  The --upgrade-base image should be a stock ACAT image from NVIDIA.",
                  file=sys.stderr)
            if not args.force:
                sys.exit(1)

        # Work on a copy of the base image
        data = bytearray(raw_base)
        entries = base_entries

        # Apply OEM profile
        print(f"\nApplying {profile_info['name']} profile ({len(profile_info['profile'])} customizations)...")
        oem_applied = apply_oem_profile(data, entries, profile_info["profile"], args.verbose)
        print(f"  Applied {oem_applied} OEM customization bytes")

        # Apply Gen4 patches on top (use --force since base values are now OEM-modified
        # for some fields — but the Gen4 patch fields don't overlap with OEM profiles)
        print(f"\nApplying {len(CORE_GEN4_PATCHES)} Gen4 patches...")
        changed, warnings = apply_patches(data, entries, CORE_GEN4_PATCHES, force=args.force)
        for w in warnings:
            print(f"  WARNING: {w}")
        print(f"  Changed {changed} Gen4 bytes")

    else:
        # ── Direct patch mode ──
        if input_info.is_gen4_already and not args.force:
            print("\nFirmware already has Gen4 enabled. Use --force to re-apply.")
            sys.exit(0)

        data = bytearray(raw_input)
        entries = input_entries

        print(f"\nApplying {len(CORE_GEN4_PATCHES)} Gen4 patches...")
        changed, warnings = apply_patches(data, entries, CORE_GEN4_PATCHES, args.force)
        for w in warnings:
            print(f"  WARNING: {w}")
        print(f"  Changed {changed} bytes")

        if changed == 0 and not args.force:
            print("Nothing to change.")
            sys.exit(0)

    if args.dry_run:
        print("\nDry run — no output written.")
        sys.exit(0)

    # ── Fix CRCs ──
    print("Recalculating CRCs...")
    crc_fixes = fix_all_crcs(data, entries)
    print(f"  Updated {crc_fixes} CRC values")

    # ── Write output ──
    with open(args.output, "wb") as f:
        f.write(data)

    mode_desc = "upgraded + patched" if args.upgrade_base else "patched"
    print(f"\n{'='*60}")
    print(f"  {mode_desc.upper()} firmware written to {args.output}")
    print(f"{'='*60}")
    print(f"\nNext steps:")
    print(f"  1. Verify:     mstflint -i {args.output} verify")
    print(f"  2. Flash:      flint -d mt4119_pciconf0 -i {args.output} --skip_ci_req burn")
    if args.upgrade_base and input_info.oem_key:
        print(f"     NOTE: Cross-vendor flash requires FNP recovery mode")
    print(f"  3. Power cycle (full power off, not reboot)")
    print(f"  4. Check:      mlxlink -d mt4119_pciconf0")


if __name__ == "__main__":
    main()
