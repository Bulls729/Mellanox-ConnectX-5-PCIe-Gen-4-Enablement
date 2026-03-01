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

Values:
- `0x01` = PCIe Gen3 (8 GT/s)
- `0x04` = PCIe Gen4 (16 GT/s)

During initialization, the firmware reads these values to configure the PCIe PHY's target link speed. The PHY will attempt to train at the specified generation during link negotiation.

### Capability Advertisement Index (FW_BOOT_CFG + 0x0093)

Controls the PCIe Express Capability Structure exposed to the host during enumeration. This determines what the host sees when it reads the card's PCIe capability registers.

Values:
- `0x45` = Advertise up to Gen3 in Link Capabilities
- `0x47` = Advertise up to Gen4 in Link Capabilities

Without this change, the host's root complex will not attempt Gen4 link training because the endpoint advertises Gen3 as its maximum supported speed.

### Speed Table Entries (HW_MAIN_CFG + 0x0404 through 0x0407)

The firmware has an internal lookup table mapping speed profile indices to PCIe speed/width configurations. Entries at these offsets point to Gen3-specific profiles:

- Entry at +0x0404: Profile index `0x0020` (Gen3 x16)
- Entry at +0x0406: Profile index `0x0021` (Gen3 x8)

Setting these to `0x0FFF` marks them as invalid/unused. This prevents the link training state machine from falling back to Gen3-only profiles. Without invalidation, the firmware may select a Gen3 fallback profile even when the generation byte is set to Gen4.

### PCIe Max Speed (HW_BOOT_CFG + 0x0023)

Named `pcie_cfg.pcie_max_speed_supported` in mstflint's config dump. Controls PHY-level link training behavior.

Values:
- `0x07` = Constrain to Gen3 link training sequences
- `0x0F` = Enable Gen4 link training (includes Phase 2/3 equalization)

PCIe 4.0 requires additional equalization phases during link training (Phases 2 and 3) to achieve reliable 16 GT/s signaling. This byte enables those phases in the link training state machine. This was the critical missing piece in initial patch attempts — all other Gen4 parameters could be set correctly, but without enabling the Gen4 link training sequence, the PHY would never attempt 16 GT/s.

## Device ID: No Gate Exists

Early experiments hypothesized that the ConnectX-5 firmware gates Gen4 behind a Device ID check (0x1017 CX5-EN vs 0x1019 CX5-Ex). **This turned out to be incorrect.** The 8-byte patch enables Gen4 on:

- Dell OEM firmware (Device ID 0x1017, PSID DEL0000000015) — tested
- HPE OEM firmware (Device ID 0x1017) — tested
- Stock Mellanox ACAT firmware (Device ID 0x1017, PSID MT_0000000183) — tested

No Device ID change is required. The early confusion arose because the boot config byte (`pcie_max_speed_supported`) and speed table invalidation hadn't been discovered yet — without those, Gen4 didn't work regardless of Device ID.

## CRC-16 Algorithm

Mellanox uses a non-standard CRC-16 with polynomial `0x100B`. This was determined from the open-source [mstflint](https://github.com/Mellanox/mstflint) project (`mft_utils/crc16.cpp`).

Algorithm parameters:
- **Polynomial:** `0x100B` (not a standard CRC-16 variant)
- **Init value:** `0xFFFF`
- **Processing:** 32-bit big-endian words, MSB-first
- **Finalization:** 16 additional zero bits shifted through the CRC
- **Final XOR:** `0xFFFF`

Two CRC fields exist per ITOC entry:
1. **Section data CRC** (bytes 26-27): CRC over the section's flash data
2. **ITOC entry CRC** (bytes 30-31): CRC over the first 28 bytes of the ITOC entry (includes the section CRC but excludes the reserved field and entry CRC itself)

The entry CRC must be recalculated after updating the section CRC, since the section CRC is part of the entry CRC's input.

## Dell OEM vs Stock Mellanox Firmware

Dell's OEM firmware for the CX5 (0V5DG9, 0TDNNT) differs from stock Mellanox ACAT in ~2,230 bytes within the configuration region. Key differences:

| Category | Dell | Stock ACAT |
|----------|------|-----------|
| PHY EQ coefficients | Gen4-optimized position | Gen3 position |
| Boot config params | Dell-tuned values | Standard values |
| PHY calibration | 0x3E26 | 0x30D4 |
| PCIe width/mode | Socket Direct (2×8) | Standard (1×16) |
| Subsystem ID | 0x0091 | 0x0061 |
| Board power budget | 15910 mW | 12500 mW |
| Signing keys | Dell keys | Mellanox keys |

The signing key difference means Dell firmware cannot be flashed onto stock Mellanox cards (and vice versa) — the card's secure boot checks reject the mismatched keys.

## Verification

After patching and flashing, verify with:

```bash
# Check PCIe link status
mlxlink -d mt4119_pciconf0
# Look for: Speed: 16G, Width: xN

# Check from host side
lspci -vvs <device> | grep -i "lnksta\|lnkcap"
# LnkCap should show Speed 16GT/s
# LnkSta should show Speed 16GT/s
```
