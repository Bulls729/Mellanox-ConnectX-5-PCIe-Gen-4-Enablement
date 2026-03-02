# ConnectX-5 PCIe Gen4 Enabler — Universal Edition

Enables PCIe Gen4 (16 GT/s) on **all** Mellanox/NVIDIA ConnectX-5 adapters — EN, VPI, SFP28, QSFP28, single-port, and dual-port — by patching user-supplied firmware images.

**This tool does not distribute firmware.** You supply your own image; the tool makes targeted modifications and recalculates CRCs.

## Background

Every ConnectX-5 card uses the same Gen4-capable silicon. NVIDIA segments the product line at the firmware level: "Ex" variants with Gen4 enabled carry a premium, while standard variants are firmware-locked to Gen3. This tool applies the minimum changes needed to unlock Gen4.

## Supported Cards

| Card Type | Example OPNs | Status |
|-----------|-------------|--------|
| EN dual-port SFP28 | MCX512F-ACAT, Dell 0TDNNT, HPE P12608 | **Tested & working** |
| EN dual-port QSFP28 | MCX516A-CCAT | **Supported** (CRC-validated) |
| VPI single-port QSFP28 | MCX555A-ECAT | **Supported** (CRC-validated) |
| VPI dual-port QSFP28 | MCX556A-ECAT | **Supported** (needs community testing) |
| EN single-port QSFP28 | MCX515A-CCAT | **Supported** (needs community testing) |
| Other OEM variants | Lenovo, Supermicro, Cisco | **Supported** (needs community testing) |

## ⚠️ Before You Start — Back Up Your Firmware

**Always back up your current working firmware before making any changes.**

```bash
# Linux (requires root)
sudo flint -d mt4119_pciconf0 ri backup_fw.bin

# Also note your GUIDs and MACs
sudo flint -d mt4119_pciconf0 query
```

If anything goes wrong, you can restore from backup using FNP recovery mode. Without a backup, recovery requires sourcing the correct firmware image for your specific card.

> **Note:** All `flint`, `mstflint`, `mlxlink`, and `mstconfig` commands require **root/admin privileges**. Use `sudo` on Linux or run as Administrator on Windows.

## Quick Start

### Option A: Patch Your Existing Firmware

```bash
# Back up first!
sudo flint -d mt4119_pciconf0 ri backup_fw.bin

# Patch
python3 cx5_gen4_enable.py --input firmware.bin --output patched.bin

# Flash
sudo flint -d mt4119_pciconf0 -i patched.bin --skip_ci_req burn

# FULL POWER CYCLE (not reboot — the card only reads flash on cold boot)

# Verify
sudo mlxlink -d mt4119_pciconf0
lspci -vvs <device> | grep -i lnksta
```

### Option B: Upgrade OEM Firmware to Latest LTS + Gen4

```bash
python3 cx5_gen4_enable.py \
    --input your_dell_oem.bin \
    --upgrade-base fw-ConnectX5-rel-16_35_8002-MCX512F-ACA.bin \
    --output patched_8002.bin
```

### Option C: Analyze Without Patching

```bash
python3 cx5_gen4_enable.py --input firmware.bin --analyze
```

## How It Works Across Card Types

The tool detects your card type automatically and applies only the patches that are needed:

**SFP28 cards (MCX512F, MCX512A):** All 8 bytes are patched — port generation, capability index, speed tables, and link training mode.

**QSFP28 cards (MCX555A, MCX515A, MCX516A):** Only 2 bytes need patching — capability index and link training mode. Port generation bytes use 0x06 for both Gen3 and Gen4 on QSFP28, and speed tables ship pre-invalidated. The tool detects this and skips accordingly.

**Single-port cards:** Port 2 generation byte is 0x00 (disabled). The tool skips it automatically.

## Flashing — Signature Errors and FNP Recovery

If you see `The Digest in the signature is wrong` when flashing, the card's secure boot is rejecting the modified image. This happens when flashing modified firmware or firmware from a different OEM vendor.

**Solution:** Put the card in **FNP (Firmware Not Present) recovery mode** by shorting the FNP jumper pins on the card. This bypasses secure boot entirely. In recovery mode, the card appears as `MT28800 Family [ConnectX-5 Flash Recovery]` in `lspci`. Flash normally from this state, then remove the jumper and power cycle.

For cards without an accessible FNP header, shorting SPI flash pins 2 and 4 during boot achieves the same recovery state.

## Usage

```
python3 cx5_gen4_enable.py --input <fw.bin> --output <patched.bin> [options]

Options:
  --input,   -i          Input firmware image (.bin)
  --output,  -o          Output patched firmware image (.bin)
  --upgrade-base FILE    Stock Mellanox LTS image as new base (OEM upgrade mode)
  --analyze, -a          Show firmware Gen4 field values (no patching)
  --force,   -f          Apply patches even if values don't match expected
  --dry-run, -n          Show what would change without writing output
  --verbose, -v          Show detailed per-field patch decisions
  --version              Show tool version
```

## Requirements

- Python 3.8+
- No external dependencies
- MFT (Mellanox Firmware Tools) for flashing — not required for patching

## Project Files

- [CHANGELOG.md](CHANGELOG.md) — Release history
- [TECHNICAL.md](TECHNICAL.md) — Full reverse engineering writeup
- [TODO.md](TODO.md) — Roadmap

## Disclaimer

Modified firmware means no vendor support. Test in a non-production environment first. Back up your firmware before starting. The ConnectX-5 silicon is Gen4-capable, but your specific board/slot/platform combination is your responsibility to validate.

## License

MIT — see [LICENSE](LICENSE).

CRC-16 algorithm derived from [mstflint](https://github.com/Mellanox/mstflint) (dual-licensed GPL-2.0/BSD).
