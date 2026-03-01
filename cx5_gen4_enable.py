#!/usr/bin/env python3
"""
ConnectX-5 PCIe Gen4 Enabler

Patches a user-supplied ConnectX-5 EN firmware image to enable PCIe Gen4 (16 GT/s).
Locates config fields via FS4 ITOC parsing (firmware-version-independent).

No external dependencies — uses native Mellanox CRC-16 (poly 0x100B) from the
open-source mstflint project (https://github.com/Mellanox/mstflint).

Usage:
    python3 cx5_gen4_enable.py --input firmware.bin --output patched.bin [--force]
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


# ─── Mellanox CRC-16 ─────────────────────────────────────────────────────────
#
# From the open-source mstflint project:
#   https://github.com/Mellanox/mstflint/blob/master/mft_utils/crc16.cpp
#
# Polynomial 0x100B, init 0xFFFF, big-endian 32-bit word processing,
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
    is_dell_oem: bool
    is_hpe_oem: bool
    is_gen4_already: bool


def analyze_firmware(data: bytes, entries: list[ITOCEntry]) -> FirmwareInfo:
    fw_boot_cfg = find_section(entries, SEC_FW_BOOT_CFG)
    hw_main_cfg = find_section(entries, SEC_HW_MAIN_CFG)
    image_info = find_section(entries, SEC_IMAGE_INFO)

    if not all([fw_boot_cfg, hw_main_cfg, image_info]):
        raise ValueError("Missing required firmware sections")

    device_id = 0x1000 | data[fw_boot_cfg.flash_addr + 0x002D]
    subsystem_id = data[fw_boot_cfg.flash_addr + 0x0031]

    psid_raw = data[image_info.flash_addr + 0x24:image_info.flash_addr + 0x3C]
    psid = psid_raw.split(b'\x00')[0].decode('ascii', errors='replace')

    is_dell = psid.startswith('DEL') or subsystem_id == 0x91
    is_hpe = psid.startswith('HP')
    is_gen4 = data[hw_main_cfg.flash_addr + 0x0245] == 0x04

    return FirmwareInfo(device_id, subsystem_id, psid, is_dell, is_hpe, is_gen4)


# ─── Patch Application ───────────────────────────────────────────────────────

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
    parser.add_argument("--input", "-i", required=True, help="Input firmware image (.bin)")
    parser.add_argument("--output", "-o", required=True, help="Output patched firmware image (.bin)")
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
        raw = f.read()

    if len(raw) < 0x100000:
        print(f"Error: File too small ({len(raw)} bytes).", file=sys.stderr)
        sys.exit(1)

    data = bytearray(raw)

    # Parse ITOC
    print(f"Parsing {args.input} ({len(data):,} bytes)...")
    entries = parse_itoc(data)
    print(f"Found {len(entries)} ITOC sections")
    if args.verbose:
        for e in entries:
            print(f"  {e.type_name:24s}  0x{e.flash_addr:08X}  "
                  f"size=0x{e.size:06X}  crc=0x{e.crc16:04X}")

    # Analyze
    info = analyze_firmware(data, entries)
    id_str = {0x1017: "CX5-EN", 0x1019: "CX5-Ex"}.get(info.device_id, "UNKNOWN")
    oem = "Dell OEM" if info.is_dell_oem else "HPE OEM" if info.is_hpe_oem else "Stock Mellanox"

    print(f"\nFirmware analysis:")
    print(f"  Device ID:    0x{info.device_id:04X} ({id_str})")
    print(f"  PSID:         {info.psid}")
    print(f"  Type:         {oem}")
    print(f"  Gen4 already: {'Yes' if info.is_gen4_already else 'No'}")

    if info.is_gen4_already and not args.force:
        print("\nFirmware already has Gen4 enabled. Use --force to re-apply.")
        sys.exit(0)

    if info.device_id not in (0x1017, 0x1019):
        print(f"\nWarning: Unexpected Device ID 0x{info.device_id:04X}.", file=sys.stderr)
        if not args.force:
            sys.exit(1)

    # Patch
    print(f"\nApplying {len(CORE_GEN4_PATCHES)} patches...")
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

    # Fix CRCs
    print("Recalculating CRCs...")
    crc_fixes = fix_all_crcs(data, entries)
    print(f"  Updated {crc_fixes} CRC values")

    # Write
    with open(args.output, "wb") as f:
        f.write(data)

    print(f"\nPatched firmware written to {args.output}")
    print(f"\nNext steps:")
    print(f"  1. Verify:     mstflint -i {args.output} verify")
    print(f"  2. Flash:      flint -d mt4119_pciconf0 -i {args.output} --skip_ci_req burn")
    print(f"  3. Power cycle (full power off, not reboot)")
    print(f"  4. Check:      mlxlink -d mt4119_pciconf0")


if __name__ == "__main__":
    main()
