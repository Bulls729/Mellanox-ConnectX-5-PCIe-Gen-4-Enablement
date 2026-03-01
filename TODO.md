# Roadmap

## Phase 1: Core Tool (Complete)
- [x] FS4 ITOC parser for section discovery
- [x] Section-relative addressing for firmware-version-independent patching
- [x] Core 8-byte Gen4 patch
- [x] Native Mellanox CRC-16 (poly 0x100B from mstflint source — zero dependencies)
- [x] Dell OEM detection and testing
- [x] HPE OEM testing (confirmed working)
- [x] Stock Mellanox ACAT testing
- [x] Dry-run mode
- [x] Confirmed: No Device ID change required (tested Dell, HPE, stock Mellanox)
- [x] Verified byte-identical output to manually crafted proven patch

## Phase 2: OEM Firmware Upgrade (Complete)
- [x] Dell OEM profile (95 customization bytes, derived from Dell 4554 vs ACAT 4554)
- [x] `--upgrade-base` flag for LTS base upgrade with OEM tuning preservation
- [x] Auto-detection of OEM vendor from input image PSID/subsystem ID
- [x] Output validated with mstflint verify
- [ ] HPE OEM profile (needs HPE firmware sample at matching stock version)
- [ ] Lenovo OEM profile
- [ ] Supermicro OEM profile

## Phase 3: Broader Card Support
- [ ] Single-port CX5 cards (MCX511F — 1 port, may need only port1 gen byte)
- [ ] ConnectX-5 VPI card support (InfiniBand + Ethernet dual-mode)
- [ ] 100GbE CX5 variants (MCX516A — QSFP28, different speed profiles)

## Phase 4: Robustness
- [ ] Firmware version detection from IMAGE_INFO section
- [ ] Section-relative offset validation across more firmware versions
  - Need community reports from 16.28.x, 16.32.x, 16.33.x, etc.
- [ ] Pre-patch validation (sanity check surrounding bytes)
- [ ] Rollback/unpatch mode (restore stock Gen3 values)
- [ ] Comprehensive test suite with known firmware hash checks

## Phase 5: Community Features
- [ ] Firmware analysis mode (`--analyze` to dump Gen4 state without patching)
- [ ] Speed table decoder (human-readable dump of PCIe speed profiles)
- [ ] NVConfig interaction guidance (which mlxconfig settings interact with Gen4)

## Research Needed

### Offset Stability
Confirmed identical section-relative offsets on:
- 16.35.4554 (ACAT, Dell OEM)
- 16.35.8002 (ACAT)

Need community testing on older firmware versions and different card models.

### OEM Variants
Each OEM potentially has different signing keys, PHY tuning, boot config defaults, and board power budgets. Need firmware samples from Lenovo, Supermicro, Cisco UCS to build profiles.
