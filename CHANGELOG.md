# Changelog

All notable changes to this project will be documented in this file.

## [1.1.0] — 2026-03-02

### Added
- **Universal CX5 support** — tool now handles all ConnectX-5 variants: EN, VPI, SFP28, QSFP28, single-port, and dual-port
- **Intelligent per-field skip logic** — automatically detects QSFP28 port gen values (0x06) and disabled ports (0x00) and skips them instead of warning
- **`--analyze` mode** — inspect firmware Gen4 field values without patching
- **Connector type auto-detection** — identifies SFP28 vs QSFP28 from port generation byte patterns
- **Port count detection** — identifies single-port vs dual-port from port 2 generation byte
- **Version flag** (`--version`)
- **CHANGELOG.md** for tracking releases
- Explicit backup instructions and sudo/admin privilege notes in all output and documentation

### Changed
- Patch logic now uses `skip_values` per field instead of rigid stock value matching — eliminates false warnings on QSFP28 and single-port firmware
- Output now shows per-field decisions (changed / skipped / warning) with reasons
- Flash instructions now include `sudo` and signature error guidance
- Analysis output now annotates each field with human-readable status

### Fixed
- **MCX555A-ECAT (VPI single-port QSFP28)**: previously produced 6 spurious warnings and only patched 2 of 8 bytes without `--force`. Now correctly identifies that only 2 bytes need changing and cleanly skips the other 6 with explanations
- Tool no longer warns about port gen = 0x06 (valid QSFP28 value used for both Gen3 and Gen4)
- Tool no longer warns about port gen = 0x00 (disabled port on single-port cards)
- Tool no longer warns about speed table entries already at 0x0FFF (already invalidated on QSFP28 firmware)

## [1.0.0] — 2026-03-01

### Added
- Initial release targeting ConnectX-5 EN dual-port SFP28 cards (MCX512F-ACAT)
- FS4 ITOC parser for firmware-version-independent section discovery
- 8-byte Gen4 patch (port gen, cap index, speed tables, max speed)
- Native Mellanox CRC-16 implementation (poly 0x100B, from mstflint source)
- Dell OEM auto-detection and board tuning profile (95 customization bytes)
- `--upgrade-base` flag for OEM firmware upgrade to stock Mellanox LTS base
- `--dry-run` and `--verbose` modes
- Tested on Dell 0V5DG9/0TDNNT, HPE P12608, stock ACAT
- Confirmed: no Device ID change required for Gen4
