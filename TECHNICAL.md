# Technical Details: ConnectX-5 PCIe Gen4 Enablement

## FS4 Firmware Image Structure

ConnectX-5 uses Mellanox's FS4 firmware image format. The image is a flat binary with sections indexed by an Image Table of Contents (ITOC) at offset `0x5000`.

### ITOC Entry Format (32 bytes)

```
Byte  0:     Section type ID
Bytes 1-3:   Section size (big-endian 24-bit)
Bytes 4-19:  Flags and metadata
Bytes 20-23: Flash address (big-endian 32-bit)
Bytes 24-25: Flags
Bytes 26-27: Section data CRC-16
Bytes 28-29: Reserved
Bytes 30-31: ITOC entry CRC-16 (over bytes 0-27)
```

### Key Section Types

| Type ID | Name | Contains |
|---------|------|----------|
| 0x08 | HW_BOOT_CFG | Boot-time hardware config (PCIe link training params) |
| 0x09 | HW_MAIN_CFG | Hardware config (PCIe gen, width, PHY EQ, speed tables) |
| 0x10 | IMAGE_INFO | Firmware metadata (PSID, Device ID, description) |
| 0x11 | FW_BOOT_CFG | Boot-time firmware config (PCIe capability advertisement) |
| 0x12 | FW_MAIN_CFG | Main firmware config (PHY, link, SFP settings) |
| 0x18 | ROM_CODE | PXE/UEFI expansion ROM |

### Section-Relative Addressing

All Gen4 patch fields are defined by their offset within a section, not their absolute position in the image. This makes the tool firmware-version-independent — as long as the section-relative layout is consistent (confirmed across 16.35.4554 and 16.35.8002), the same relative offsets work regardless of where the sections are placed in the image.

### Named vs Unnamed Fields

`mstflint dc` can dump named configuration fields from the firmware image. However, only 1 of the 8 Gen4 patch bytes is exposed as a named field:

| Field | Named in mstflint dc? | Name |
|-------|----------------------|------|
| pcie_max_speed_supported | **Yes** | `pcie_cfg.pcie_max_speed_supported` |
| port1_pcie_gen | No | — |
| port2_pcie_gen | No | — |
| pcie_cap_index | No | — |
| speed_table entries | No | — |

The 7 unnamed fields were discovered through binary diffing between Dell OEM and stock Mellanox firmware images. They reside in opaque regions of HW_MAIN_CFG and FW_BOOT_CFG that mstflint treats as unstructured data.

## Gen4 Patch Fields

### Port PCIe Generation (HW_MAIN_CFG + 0x0245 / 0x0285)

The firmware stores the target PCIe generation for each port in HW_MAIN_CFG. These are single-byte fields 0x40 apart (one per physical port).

Values: `0x01` = Gen3 (8 GT/s), `0x04` = Gen4 (16 GT/s).

### Capability Advertisement Index (FW_BOOT_CFG + 0x0093)

Controls the PCIe Express Capability Structure exposed to the host during enumeration. Values: `0x45` = advertise Gen3, `0x47` = advertise Gen4. Without this, the root complex won't attempt Gen4 link training.

### Speed Table Entries (HW_MAIN_CFG + 0x0404 through 0x0407)

Internal lookup table mapping speed profile indices to PCIe configurations. Profiles 0x0020 (Gen3 x16) and 0x0021 (Gen3 x8) are invalidated by writing 0x0FFF, preventing Gen3 fallback during link training.

### PCIe Max Speed (HW_BOOT_CFG + 0x0023)

Named `pcie_cfg.pcie_max_speed_supported` in mstflint. Value `0x07` constrains to Gen3 link training; `0x0F` enables Gen4 with Phase 2/3 equalization. This was the last byte discovered — without it, all other Gen4 settings are ignored.

## Device ID: No Gate Exists

Early experiments hypothesized that Gen4 was gated behind Device ID (0x1017 CX5-EN vs 0x1019 CX5-Ex). **This turned out to be incorrect.** Confirmed on Dell OEM, HPE OEM, and stock ACAT — all with original 0x1017 Device ID.

## Dell OEM vs Stock Mellanox Firmware

Flashing stock Mellanox ACAT firmware onto Dell OEM cards caused autonegotiation failures. The Dell OEM firmware differs from stock in 95 bytes of functional configuration across four sections. These differences explain why the stock image doesn't work correctly on Dell hardware:

### Why Stock Firmware Fails on Dell Cards

The Dell CX512F cards (0V5DG9, 0TDNNT) use a **Socket Direct** PCIe configuration (2×8 lanes) rather than standard x16. This is reflected in multiple firmware configuration fields. When stock Mellanox firmware (configured for standard x16) is flashed onto Dell hardware, the PCIe port mode mismatch causes the card to fail to negotiate links correctly. Additionally, the PHY equalization coefficients, calibration values, and SFP link tuning parameters are all board-specific — Dell's PCB layout and trace routing require different compensation values than the reference Mellanox design.

### Configuration Differences by Category

| Category | Bytes | Section | What It Controls |
|----------|-------|---------|-----------------|
| PHY EQ coefficients | 14 | HW_MAIN_CFG | Signal equalization tuned for Dell PCB layout |
| PHY tuning | 8 | HW_MAIN_CFG | SFP signal quality parameters |
| PHY calibration | 4 | HW_MAIN_CFG | Trace compensation (Dell 0x3E26 vs stock 0x30D4) |
| Speed table profiles | 22 | HW_MAIN_CFG | PCIe speed/width profile mappings |
| PCIe port mode | 2 | HW_MAIN_CFG | Socket Direct 2×8 vs standard x16 |
| SFP link tuning | 32 | FW_MAIN_CFG | Link and SFP configuration parameters |
| FW misc config | 4 | FW_MAIN_CFG | LED behavior, port/link settings |
| Boot/PCIe config | 7 | FW_BOOT_CFG | Subsystem ID, PCIe subsystem, boot params |
| Boot config | 3 | HW_BOOT_CFG | Hardware boot parameters |

**Total: 95 bytes** (identity strings like PSID, name, and description excluded).

### Key Measurable Differences

| Parameter | Dell OEM | Stock ACAT |
|-----------|----------|-----------|
| PCIe port mode | Socket Direct (2×8) | Standard (1×16) |
| PHY calibration | 0x3E26 | 0x30D4 |
| Board power budget | 15,910 mW | 12,500 mW |
| Subsystem ID | 0x0091 | 0x0061 |
| Signing keys | Dell keys | Mellanox keys |

The signing key difference means Dell firmware cannot be flashed onto stock Mellanox cards (and vice versa) without using FNP firmware recovery mode to bypass secure boot.

### OEM Firmware Upgrade

The `--upgrade-base` feature addresses this by porting all 95 Dell customization bytes onto a stock Mellanox LTS base image. The Dell OEM profile is built into the tool and was derived by diffing Dell 4554 against ACAT 4554 at the section level. The profile entries are defined as section-relative offsets, making them version-independent.

## CRC-16 Algorithm

Mellanox uses a non-standard CRC-16 with polynomial `0x100B`. Sourced from the open-source [mstflint](https://github.com/Mellanox/mstflint) project (`mft_utils/crc16.cpp`).

- **Polynomial:** `0x100B`
- **Init value:** `0xFFFF`
- **Processing:** 32-bit big-endian words, MSB-first
- **Finalization:** 16 additional zero bits shifted through the CRC
- **Final XOR:** `0xFFFF`

Two CRC fields per ITOC entry: section data CRC (bytes 26-27) and entry CRC (bytes 30-31, over first 28 bytes of entry). The entry CRC must be recalculated after updating the section CRC.

## Verification

```bash
# Verify image integrity
mstflint -i patched.bin verify

# Check PCIe link status (after flashing and power cycle)
mlxlink -d mt4119_pciconf0
lspci -vvs <device> | grep -i "lnksta\|lnkcap"
```
