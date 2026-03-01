# ConnectX-5 PCIe Gen4 Enabler

A tool to enable PCIe Gen4 (16 GT/s) on Mellanox/NVIDIA ConnectX-5 EN network adapters by patching user-supplied firmware images.

**This tool does not distribute firmware.** You supply your own firmware image obtained from NVIDIA or your card's OEM vendor. The tool modifies 8 configuration bytes to unlock the PCIe Gen4 capability that is present but disabled in the ConnectX-5 silicon.

## Background

The ConnectX-5 EN (MCX512F-ACAT, Device ID 0x1017) and ConnectX-5 Ex EN (MCX512A-ADAT, Device ID 0x1019) use identical silicon. The only differences are firmware configuration flags — NVIDIA segments them at the firmware level and charges a premium for the Ex. This tool applies the minimum set of changes to enable Gen4 on standard ConnectX-5 EN firmware.

## Supported Cards

| Card | Firmware Source | Status |
|------|----------------|--------|
| Dell 0V5DG9 / 0TDNNT (OEM MCX512F-ACA) | Dell OEM firmware | **Tested & working** |
| HPE OEM variant | HPE OEM firmware | **Tested & working** |
| MCX512F-ACAT (stock Mellanox) | NVIDIA firmware downloads | **Tested & working** |
| Other OEM variants (Lenovo, Supermicro) | OEM firmware | **Untested — needs community validation** |

## Quick Start

```bash
# Back up your current firmware first!
flint -d mt4119_pciconf0 ri backup_fw.bin

# Patch your firmware image
python3 cx5_gen4_enable.py --input your_firmware.bin --output patched_firmware.bin

# Flash the patched image
flint -d mt4119_pciconf0 -i patched_firmware.bin --skip_ci_req burn

# FULL POWER CYCLE (not reboot)
# Then verify:
mlxlink -d mt4119_pciconf0
```

## What It Changes

Exactly **8 bytes** across 3 firmware config sections:

| Field | Section | Offset | Stock | Patched | Purpose |
|-------|---------|--------|-------|---------|---------|
| Port 1 PCIe Gen | HW_MAIN_CFG+0x0245 | varies | 0x01 | 0x04 | PCIe link speed target |
| Port 2 PCIe Gen | HW_MAIN_CFG+0x0285 | varies | 0x01 | 0x04 | PCIe link speed target |
| Capability Index | FW_BOOT_CFG+0x0093 | varies | 0x45 | 0x47 | PCIe capability advertisement |
| Speed Table [0] | HW_MAIN_CFG+0x0404 | varies | 0x0020 | 0x0FFF | Invalidate Gen3 profile |
| Speed Table [1] | HW_MAIN_CFG+0x0406 | varies | 0x0021 | 0x0FFF | Invalidate Gen3 profile |
| Max Speed | HW_BOOT_CFG+0x0023 | varies | 0x07 | 0x0F | Link training mode |

The tool locates fields by parsing the FS4 Image Table of Contents (ITOC) to find section starts, then applies patches at fixed offsets within each section. This makes it firmware-version-independent.

All section CRCs and ITOC entry CRCs are automatically recalculated using the native Mellanox CRC-16 algorithm (polynomial 0x100B), sourced from the open-source [mstflint](https://github.com/Mellanox/mstflint) project.

## Requirements

- Python 3.8+
- No external dependencies (stdlib only)
- MFT (Mellanox Firmware Tools) for flashing — not required for patching

## Safety

- The tool validates the input image before patching (FS4 format, ConnectX-5 device, section structure)
- Original values are verified before overwriting (won't patch an already-patched image without `--force`)
- All CRCs are recalculated natively to produce a valid image
- The original file is never modified — output goes to a new file
- ConnectX cards have flash recovery; a bad image can be recovered with `mstflint`

## Usage

```
python3 cx5_gen4_enable.py --input <fw.bin> --output <patched.bin> [options]

Options:
  --input,   -i    Input firmware image (.bin)
  --output,  -o    Output patched firmware image (.bin)
  --force,   -f    Apply patches even if values don't match expected stock
  --dry-run, -n    Show what would change without writing output
  --verbose, -v    Show detailed section and patch information
```

## Project Status

This is early-stage. See [TODO.md](TODO.md) for the roadmap.

For the full technical writeup, see [TECHNICAL.md](TECHNICAL.md).

## Disclaimer

This is firmware modification on a network adapter. Modified firmware means no vendor support. Test in a non-production environment first. Back up your firmware before starting. The ConnectX-5 silicon is Gen4-capable, but NVIDIA's QA for the Gen4 + SFP28 combination on your specific board/slot is your responsibility to validate.

## License

MIT — see [LICENSE](LICENSE).

CRC-16 algorithm derived from [mstflint](https://github.com/Mellanox/mstflint) (dual-licensed GPL-2.0/BSD).
