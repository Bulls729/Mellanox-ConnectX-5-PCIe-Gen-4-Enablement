# Roadmap

## Complete
- [x] FS4 ITOC parser for firmware-version-independent patching
- [x] Core Gen4 patch with native CRC-16
- [x] Dell OEM profile and `--upgrade-base` mode
- [x] Universal CX5 support (EN, VPI, SFP28, QSFP28, single/dual-port)
- [x] Intelligent skip logic for QSFP28 port gen (0x06) and disabled ports (0x00)
- [x] `--analyze` mode for firmware inspection
- [x] Connector type and port count auto-detection
- [x] Confirmed: no Device ID change required

## Needs Community Testing
- [ ] MCX555A-ECAT (VPI 1×QSFP28) — hardware bench test
- [ ] MCX515A-CCAT (EN 1×QSFP28) — hardware bench test
- [ ] MCX516A-CCAT (EN 2×QSFP28) — hardware bench test
- [ ] MCX556A-ECAT (VPI 2×QSFP28) — hardware bench test
- [ ] Lenovo, Supermicro, Cisco OEM variants

## Planned
- [ ] HPE OEM profile (needs matched firmware pair for diffing)
- [ ] Rollback/unpatch mode (restore stock Gen3 values)
- [ ] Firmware version extraction from IMAGE_INFO
- [ ] Platform compatibility notes (AMD Strix Halo Gen4 training issues reported)
