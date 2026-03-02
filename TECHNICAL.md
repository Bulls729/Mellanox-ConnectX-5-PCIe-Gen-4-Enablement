# Technical Details: ConnectX-5 PCIe Gen4 Enablement

## How Gen4 Differs Across Connector Types

A key discovery from analyzing firmware images across the full CX5 product line:

| Card Family | Port Gen (Gen3) | Port Gen (Gen4) | Speed Tables (Gen3) | Speed Tables (Gen4) |
|-------------|----------------|----------------|--------------------|--------------------|
| SFP28 (MCX512F-ACAT) | **0x01** | **0x04** | 0x0020, 0x0021 | 0x0FFF, 0x0FFF |
| QSFP28 (MCX555A-ECAT) | **0x06** | **0x06** | 0x0FFF, 0x0FFF | 0x0FFF, 0x0FFF |
| QSFP28 (MCX516A-CDA Ex) | **0x06** | **0x06** | 0x0FFF, 0x0FFF | 0x0FFF, 0x0FFF |

QSFP28 cards use port_gen=0x06 for **both** Gen3 and Gen4, and their speed tables ship pre-invalidated. This means Gen4 enablement on QSFP28 is controlled entirely by 2 universal bytes:

| Field | Section + Offset | Gen3 | Gen4 | Applies To |
|-------|-----------------|------|------|-----------|
| cap_index | FW_BOOT_CFG+0x0093 | 0x45 | 0x47 | **All CX5** |
| max_speed | HW_BOOT_CFG+0x0023 | 0x07 | 0x0F | **All CX5** |
| port1_gen | HW_MAIN_CFG+0x0245 | 0x01 | 0x04 | SFP28 only |
| port2_gen | HW_MAIN_CFG+0x0285 | 0x01 | 0x04 | SFP28 dual-port only |
| speed_tbl[0] | HW_MAIN_CFG+0x0404 | 0x0020 | 0x0FFF | SFP28 only |
| speed_tbl[1] | HW_MAIN_CFG+0x0406 | 0x0021 | 0x0FFF | SFP28 only |

## FS4 Firmware Image Structure

ConnectX-5 uses Mellanox's FS4 firmware image format. The ITOC at offset `0x5000` indexes all sections. Section sizes are consistent across variants (HW_MAIN_CFG = 0x0980, FW_BOOT_CFG = 0x04C0, HW_BOOT_CFG = 0x0140), confirmed on MCX512F-ACAT, MCX555A-ECAT, MCX516A-CDA, and Dell OEM images.

### Section-Relative Addressing

All patch fields are section-relative offsets, not absolute positions. The ITOC parser finds each section's start address at runtime, making the tool firmware-version-independent. Confirmed stable across 16.35.1012, 16.35.4554, and 16.35.8002.

### Named vs Unnamed Fields

Only 1 of the 8 patch bytes is exposed by `mstflint dc`: `pcie_cfg.pcie_max_speed_supported` (HW_BOOT_CFG+0x0023). The other 7 were found through binary diffing.

## Dell OEM vs Stock Mellanox Firmware

Dell CX512F cards use Socket Direct (2×8 lanes) and Dell-specific PHY tuning. 95 bytes of functional config differ from stock, covering PHY EQ coefficients, PCIe port mode, SFP link tuning, power budget (15.9W vs 12.5W), and PHY calibration. Flashing stock Mellanox firmware breaks autonegotiation due to PCIe port mode mismatch.

The `--upgrade-base` feature ports all 95 Dell customizations onto a stock LTS base, then applies Gen4 patches on top.

## CRC-16 Algorithm

Mellanox CRC-16, polynomial 0x100B. Sourced from [mstflint](https://github.com/Mellanox/mstflint) `mft_utils/crc16.cpp`. Init 0xFFFF, 32-bit big-endian words, 16-bit zero-flush finalization, final XOR 0xFFFF. Two CRCs per ITOC entry: section data CRC and entry CRC.

## Device ID

Gen4 is **not** gated behind Device ID. Confirmed on 0x1017 (CX5-EN) across Dell OEM, HPE OEM, stock ACAT, and MCX555A-ECAT. No Device ID change required.

## Secure Boot and FNP Recovery

CX5 firmware images are RSA-signed. Modified images fail signature verification. FNP jumper forces recovery mode (card appears as PCI ID 15b3:020d), bypassing signing. Alternative: short SPI flash pins 2+4 during boot.
