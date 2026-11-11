# Seed Max

**An unofficial fork of [SeedSigner](https://github.com/SeedSigner/seedsigner) for a touchscreen, battery-powered build — plus *Anti-Klepto Check*, an air-gapped verifier that checks what your hardware wallet actually signed.**

> Seed Max is not affiliated with, endorsed by, or supported by the SeedSigner project. Please don't ask the SeedSigner team for help with it.

Two images for the same device:

| Image | Branch | Latest | What it does |
|---|---|---|---|
| **Seed Max** | [`seed-max`](../../tree/seed-max) | v5.6 | SeedSigner 0.8.7 with a 3.5" resistive touch UI and a battery. Signs like SeedSigner. |
| **Anti-Klepto Check** | [`anti-klepto-check`](../../tree/anti-klepto-check) | v7.8 | A separate image that **never signs**. It verifies transactions signed by *another* device (Coldcard Q, SeedSigner, Trezor) against your seed. |

---

## Hardware (v5)

No custom PCB, no soldering.

| Part | Notes |
|---|---|
| Raspberry Pi Zero **v1.3** | no Wi-Fi / Bluetooth on the board; hammer header |
| Waveshare 3.5inch RPi LCD **(A)** | 480×320 ILI9486 + XPT2046 resistive touch, plugged onto pins 1–26. Use a stylus. |
| Waveshare **UPS HAT (C)** | fixed-function chips only (no updatable MCU), physical ON/OFF switch; LiPo cell |
| Pi camera | under the display's overhang, flex cable folded once |

The v4 code paths (ST7796 4.0" panel, custom carrier PCB) are still in the tree but v5 is the tested build. All pins and panel parameters live in one auditable file: [`src/seedsigner/hardware/hw_profile.py`](../../blob/seed-max/src/seedsigner/hardware/hw_profile.py).

---

## Seed Max — what changes compared to SeedSigner 0.8.7

**The signing path is untouched.** `models/psbt_parser.py`, `seed.py`, `encode_qr.py`, `decode_qr.py`, `seed_storage.py`, `settings.py`, `views/psbt_views.py`, `seed_views.py`, `tools_views.py`, `scan_views.py`, the whole of `helpers/` and `main.py` are byte-identical to SeedSigner 0.8.7 (commit `e0a80d4b`). Check it yourself (no output = identical):

```bash
S=src/seedsigner
git diff e0a80d4b seed-max --stat -- src/main.py $S/helpers \
  $S/models/psbt_parser.py $S/models/seed.py $S/models/encode_qr.py $S/models/decode_qr.py \
  $S/models/seed_storage.py $S/models/settings.py \
  $S/views/psbt_views.py $S/views/seed_views.py $S/views/tools_views.py $S/views/scan_views.py
```

The only change inside `psbt_screens.py`: *Approve* and *Skip verification* now need a 1-second hold.

What is new:

- **Touch as the only input.** XPT2046 driver, debounce, calibrated hit-testing, drag to scroll. The joystick GPIO pins are no longer read (bare header pins next to GND could otherwise "press" Approve).
- **Hold to confirm** (1 s, green progress bar) for approving a transaction, skipping change verification, exporting an xpub and signing a message. It was attacked adversarially in real time: lifts, stalls, clock steps and finger drift all cancel it.
- **Input guard.** A touch that confirmed the previous screen, a resting finger, a double tap or a tap on a warning toast can't act on the next screen.
- **Battery gauge.** The INA219 on the UPS HAT is read **read-only** over bit-banged I2C. No register is ever written, and the OS has no I2C driver loaded.
- **Bigger QR display** (320×320) with on-screen brightness +/− and an explicit *Done* button: a stray touch can't close a signed-PSBT QR.
- **Scanner zoom 1×/2×/3×** (camera ISP crop) for small address QR codes.
- **Seed word entry:** tap a candidate word to choose it.
- **Passphrase on three lines** of 40 characters, with spaces shown as dots: a passphrase that differs by one space is a different wallet.
- **Touch calibration** (Settings › Advanced › Hardware) and a battery diagnostics screen.

Tests: `tests/test_seedsigner_max*.py` and `tests/seedsigner_max_*.py`. They include a real-time harness that drives the real screens with scripted touches.

---

## Anti-Klepto Check — verify what your signer actually signed

A hardware wallet can be compromised (malicious firmware, supply chain, a coordinator you don't control) in ways its own screen will never show you:

- **Nonce covert channel ([Dark Skippy](https://darkskippy.com/) and similar):** the signatures look valid, but their nonces leak your seed to whoever watches the blockchain. A couple of transactions can be enough.
- **Wrong sighash:** a signature that commits to less than the whole transaction, so others can change it.
- **Change that isn't yours:** the "change" output pays an attacker, or a multisig quorum that isn't your wallet.
- **A different transaction:** what gets broadcast is not what you checked, for example because the segwit txid doesn't cover the witnesses.

Anti-Klepto Check runs on a **second, air-gapped device** that holds your seed only in RAM and never signs. It recomputes everything with its own code:

| Check | How |
|---|---|
| **RFC 6979 nonce** | recomputes the deterministic nonce each signature *must* have used, under the low-R rule of the signer you name (Coldcard Q = libsecp, SeedSigner = embit, Trezor = none) |
| **SIGHASH_ALL** | on every signature, and low-S on every signature |
| **Txid match** | the full txid, shown large, to compare with Sparrow's |
| **Change check** | every output classified YOURS / NOT YOURS / NOT VERIFIED. Single-sig change keys are re-derived from the seed. Multisig change must match the descriptor confirmed against your paper backup, or the quorum being spent. |
| **Address check / explorer** | is this address in your wallet? (single-sig BIP84/49/44, or a multisig descriptor from your backup) |
| **Air-gapped** | the microSD is remounted read-only. Modules that could write it are blocked by an import guard. |

If everything is green, the device rebuilds the transaction **from the signatures it checked**, shows it as a QR code, and you broadcast *that* one from Sparrow (File › Load Transaction › From QR).

**Workflow:** sign on your hardware wallet as usual → scan the signed PSBT (or, for a Coldcard Q, the final transaction plus the unsigned PSBT) into Anti-Klepto Check → compare the txid → if green, scan its QR back into Sparrow and broadcast.

### Limits — read before relying on it

- It only works with signers that use **deterministic RFC 6979 nonces**. Signers that use random nonces, or the interactive *anti-exfil* protocol (Jade, BitBox02), can't be checked this way. Despite the similar name, this tool is not that protocol: it is an after-the-fact check.
- **Taproot (bech32m) is not supported.** Finalized legacy (non-segwit) inputs are not supported.
- **Multisig:** a cosigner's nonce can't be recomputed without that cosigner's seed. The result is yellow ("THIS SEED: CLEAN") until you load each cosigner's seed in the same session.
- **The verifier needs your seed.** Treat it exactly like a signing device: air-gapped, powered off after use (the OS runs from RAM), never connected to anything.

---

## Status and honesty

- **Experimental software built by one person.** It is tested by the author on his own device: a mainnet signing test with Seed Max and Sparrow, and device tests of Anti-Klepto Check with a Coldcard Q and Sparrow. **No human third party has audited it.** Start with a test seed and small amounts.
- **Developed with AI assistance** (Anthropic's Claude). Commits carry `Co-Authored-By` lines. The "independent reviews" mentioned in commit messages were separate AI-agent reviews, adversarial tests and mutation testing, not human audits. Every finding is reproduced as a regression test.
- Seed Max's signing path is SeedSigner 0.8.7's, unchanged (see the `git diff` above). Anti-Klepto Check's verifier (`src/nonceverify/`) is new code that uses the Python standard library only.

Bug reports are welcome as issues. Report security problems **privately** through [Security › Report a vulnerability](../../security/advisories/new), not in a public issue.

---

## Download and verify

Each [release](../../releases) has the `.img`, the patch series, `SHA256SUMS` and `SHA256SUMS.asc`.

```bash
# 1. get my key (verify the fingerprint below from at least two places:
#    this README, my GitHub profile, keys.openpgp.org)
gpg --keyserver hkps://keys.openpgp.org --recv-keys 513B35830E4F869EFC6D9534F1A05CB16C898D92

# 2. verify the signature, then the files
gpg --verify SHA256SUMS.asc SHA256SUMS
sha256sum -c SHA256SUMS --ignore-missing
```

Flash the image to a microSD card with `dd` or GNOME Disks.

**Building from source.** The images are built with [SeedSigner OS](https://github.com/SeedSigner/seedsigner-os) for `--pi0`, using this repository and branch as the app source. Bit-for-bit reproducibility is not documented yet.

---

## Signing key

All commits and tags are signed.

```
Ollen_47
513B 3583 0E4F 869E FC6D 9534 F1A0 5CB1 6C89 8D92
```

---

## Support the project

If Seed Max or Anti-Klepto Check is useful to you, a donation is welcome. The address is a **silent payment address (BIP 352)**: every payment lands on a fresh on-chain address, so donations can't be linked or counted from outside.

The address is in [`DONATE.txt.asc`](DONATE.txt.asc), **signed with the key above**. Verify the signature before sending: a fork could replace this README, but it can't forge the signature.

```bash
gpg --verify DONATE.txt.asc
```

---

## License and credits

MIT, like SeedSigner. Original code © 2021 SeedSigner contributors (see [`LICENSE.md`](LICENSE.md)). Seed Max and Anti-Klepto Check changes © 2026 Ollen_47, same license.

- [SeedSigner](https://github.com/SeedSigner/seedsigner): everything this is built on.
- XPT2046 read sequence after Paul Stoffregen's XPT2046_Touchscreen (MIT).
- ILI9486 init sequence after the Linux kernel's `drm/tiny/ili9486` driver.
