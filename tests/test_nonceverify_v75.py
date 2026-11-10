"""
v7.5: fixes from the independent review of 10 Nov 2026. The reviewers' proof-of-concept
attacks are here as regression tests, each now asserting the attack FAILS.
"""
import hashlib
import json
import os

import pytest

from nonceverify import address as A, app, crypto, storage, transport, ui
from nonceverify.hw import Event, ImportGuard
from nonceverify.psbt import combine_final_tx, parse_psbt, parse_tx, p2pkh_script
from nonceverify.verify import (STATUS__UNVERIFIABLE, VERDICT__FAIL, VERDICT__INCOMPLETE, VERDICT__OK,
    VERDICT__PARTIAL, txid, verify_psbt)

pytest.importorskip("embit")
from embit import bip32, script                                                       # noqa: E402
from embit.psbt import PSBT, DerivationPath                                           # noqa: E402
from embit.transaction import Transaction, TransactionInput, TransactionOutput        # noqa: E402
from test_nonceverify import (EVIL_K, _rfc6979_sig, _sighash_ht, der, embit_root,      # noqa: E402
    libsecp_sign_psbt, master, p2wpkh_psbt, sighash_of, sign_with_k)
from test_nonceverify_address import multisig_descriptor                              # noqa: E402
from test_nonceverify_ui import (DESCRIPTOR_CONFIRM, ENDING_OK, FULL, LEFT, MIDDLE, RIGHT, SAME_TXID,  # noqa: E402
    SEEDSIGNER, WIPE, FakeHardware, Stop, load_seed_script, text_mode_zbar, ur_parts)  # noqa: F401
from test_nonceverify_v72 import coldcard_pair, finalize                              # noqa: E402

SEED_B = "legal winner thank year wave sausage worth useful legal winner thank yellow"
SEED_C = "letter advice cage absurd amount doctor acoustic avoid letter advice cage above"
DATA = os.path.join(os.path.dirname(__file__), "data")


def banner(shot):
    return shot.getpixel((240, 34))


def run(script_, session=None):
    hw = FakeHardware(script_ + [("stop",)])
    with pytest.raises(Stop):
        app.run(hw, session)
    return hw


# ---------------------------------------------------------------------------------------
# C1: the transaction broadcast is the one verified
# ---------------------------------------------------------------------------------------
def leaky_final(unsigned):
    """ What a malicious Coldcard would write to its .txn: same txid, chosen nonces """
    leaky = PSBT.parse(unsigned)
    psbt, keys, amounts = p2wpkh_psbt(2)
    for i, k in enumerate(keys):
        pub = k.key.get_public_key()
        z = sighash_of(leaky, i, amounts[i], p2pkh_script(pub.sec()))
        leaky.inputs[i].partial_sigs[pub] = sign_with_k(int.from_bytes(k.key.secret, "big"), z, EVIL_K + i)
    return finalize(leaky)


def test_broadcast_tx_is_exactly_the_verified_one():
    unsigned, clean = coldcard_pair()
    report = verify_psbt(combine_final_tx(parse_psbt(unsigned), clean), master(), grinding="libsecp")
    assert report.verdict == VERDICT__OK
    assert report.broadcast_tx == clean, "the clean final transaction, byte for byte"
    leaky = leaky_final(unsigned)
    assert Transaction.parse(leaky).txid() == Transaction.parse(clean).txid(), "same txid, other witnesses"
    assert report.broadcast_tx != leaky, "what the AKC exports is never the leaky one"


def test_broadcast_tx_from_partial_signatures_matches_embit_finalizer():
    for nested in (False, True):
        psbt, keys, amounts = p2wpkh_psbt(3, nested=nested)
        psbt.sign_with(embit_root())
        report = verify_psbt(parse_psbt(psbt.serialize()), master(), grinding="embit")
        assert report.verdict == VERDICT__OK
        want = finalize(PSBT.parse(psbt.serialize()))
        assert report.broadcast_tx == want
        assert Transaction.parse(report.broadcast_tx).txid().hex() == report.txid


def test_bbqr_export_round_trips_through_the_scanner():
    unsigned, clean = coldcard_pair(n=4)
    parts = app.bbqr_parts(clean)
    assert len(parts) > 1 and all(p.startswith("B$2T") for p in parts)
    c = transport.PSBTCollector()
    for p in reversed(parts):
        c.add(p.encode())
    assert c.result == clean
    assert transport.looks_like_final_tx(c.result)


def test_broadcast_screen_frames_read_back(monkeypatch):
    monkeypatch.setattr(app, "decode_qr", lambda image: _zbar(image))
    unsigned, clean = coldcard_pair(n=3)
    report = verify_psbt(combine_final_tx(parse_psbt(unsigned), clean), master(), grinding="libsecp")
    screen = app.BroadcastScreen(report)
    assert screen.ok and len(screen.frames) == len(app.bbqr_parts(clean))
    for frame, part in zip(screen.frames, screen.parts):
        assert _zbar(frame) == part.encode()
        assert frame.size[0] <= app.BroadcastScreen.QR_BOX and frame.size[0] // 53 >= 4, "4-pixel modules"


def _zbar(image):
    from pyzbar import pyzbar
    from pyzbar.pyzbar import ZBarSymbol
    found = pyzbar.decode(image.convert("L"), symbols=[ZBarSymbol.QRCODE])
    return found[0].data if found else None


def test_green_result_offers_the_tx_qr_and_shows_it():
    psbt, keys, amounts = p2wpkh_psbt(2)
    psbt.sign_with(embit_root())
    script_ = load_seed_script(shots=False) + SEEDSIGNER + [RIGHT, ("blank",)] + ur_parts(psbt.serialize())
    script_ += [SAME_TXID, ENDING_OK, ("shot", "90_green_txqr"), FULL, ("shot", "91_broadcast_qr"), FULL,
                ("shot", "92_back_to_result")]
    hw = run(script_)
    assert banner(hw.shots["90_green_txqr"]) == ui.GREEN
    shot = hw.shots["91_broadcast_qr"]
    qr = shot.crop((ui.PAD, ui.TOP, ui.PAD + app.BroadcastScreen.QR_BOX, ui.TOP + app.BroadcastScreen.QR_BOX))
    part = _zbar(qr).decode()
    assert part.startswith("B$2T"), part[:8]
    assert banner(hw.shots["92_back_to_result"]) == ui.GREEN


def test_wrong_txid_ending_is_red():
    psbt, keys, amounts = p2wpkh_psbt(1)
    psbt.sign_with(embit_root())
    wrong = ("tap", 240, 168)                       # FixedRandom: the right ending is the first
    script_ = load_seed_script(shots=False) + SEEDSIGNER + [RIGHT, ("blank",)] + ur_parts(psbt.serialize())
    script_ += [SAME_TXID, ("shot", "93_ending_choice"), wrong, ("shot", "94_ending_wrong")]
    hw = run(script_)
    assert banner(hw.shots["94_ending_wrong"]) == ui.RED


def test_txid_ending_screen_options():
    screen = app.TxidEndingScreen("ab" * 28 + "12345678", rng=app.random.Random(7))
    assert screen.options.count("12345678") == 1 and len(set(screen.options)) == 3
    assert all(o[-4:] != "5678" for o in screen.options if o != "12345678"), "decoys differ at the end"
    right = screen.buttons[screen.options.index("12345678")]
    x, y = (right.rect[0] + right.rect[2]) / 2, (right.rect[1] + right.rect[3]) / 2
    assert screen.on_event(Event(Event.TAP, x, y)) == "same"


# ---------------------------------------------------------------------------------------
# C2 / C3: cosigners
# ---------------------------------------------------------------------------------------
def _multisig(cosigner_sig_fn, finalize_ws=False):
    roots = [embit_root(), embit_root(SEED_B, ""), embit_root(SEED_C, "")]
    path = "m/48h/0h/0h/2h/0/7"
    A_, B, C = (r.derive(path) for r in roots)
    pubs = sorted([k.key.get_public_key() for k in (A_, B, C)], key=lambda p: p.sec())
    ws = script.multisig(2, pubs)
    tx = Transaction(vin=[TransactionInput(b"\x22" * 32, 1)],
                     vout=[TransactionOutput(90_000, script.p2wpkh(C.key.get_public_key()))], locktime=870000)
    tx.vin[0].sequence = 0xFFFFFFFD
    psbt = PSBT(tx)
    psbt.inputs[0].witness_utxo = TransactionOutput(100_000, script.p2wsh(ws))
    psbt.inputs[0].witness_script = ws
    for r, k in zip(roots, (A_, B, C)):
        psbt.inputs[0].bip32_derivations[k.key.get_public_key()] = DerivationPath(r.my_fingerprint, bip32.parse_path(path))
    z = _sighash_ht(psbt, 0, 100_000, ws.data, 0x01)
    sigs = {A_.key.get_public_key(): _rfc6979_sig(A_.key.secret, z, 0x01),
            B.key.get_public_key(): cosigner_sig_fn(B, z)}
    if finalize_ws:
        psbt.inputs[0].final_scriptwitness = script.Witness([b""] + [sigs[p] for p in pubs if p in sigs] + [ws.data])
    else:
        psbt.inputs[0].partial_sigs = sigs
    return psbt


@pytest.mark.parametrize("finalize_ws", [False, True])
def test_cosigner_with_a_chosen_nonce_is_not_green(finalize_ws):
    """ PoC 2 of the review: green before; now yellow with seed A, red with seed B """
    psbt = _multisig(lambda B, z: sign_with_k(int.from_bytes(B.key.secret, "big"), z, EVIL_K), finalize_ws)
    raw = psbt.serialize()
    report = verify_psbt(parse_psbt(raw), master(), grinding="embit")
    assert report.verdict == VERDICT__PARTIAL and report.unchecked_cosigners == 1
    assert verify_psbt(parse_psbt(raw), master(SEED_B, ""), grinding="embit").verdict == VERDICT__FAIL


def test_honest_cosigner_turns_green_once_its_seed_checked_it():
    psbt = _multisig(lambda B, z: _rfc6979_sig(B.key.secret, z, 0x01))
    raw = psbt.serialize()
    with_b = verify_psbt(parse_psbt(raw), master(SEED_B, ""), grinding="embit")
    assert with_b.verdict == VERDICT__PARTIAL
    clean = {c for r in with_b.inputs for c in r.checked_sigs}
    with_a = verify_psbt(parse_psbt(raw), master(), grinding="embit", clean_elsewhere=clean)
    assert with_a.verdict == VERDICT__OK and with_a.unchecked_cosigners == 0
    assert with_a.broadcast_tx is not None


def test_two_seeds_in_one_power_on_make_the_multisig_green():
    """ Seed A: yellow. Change seed, seed B, same PSBT: green (B's check remembered for A's) """
    psbt = _multisig(lambda B, z: _rfc6979_sig(B.key.secret, z, 0x01))
    parts = ur_parts(psbt.serialize())
    script_ = load_seed_script(shots=False) + SEEDSIGNER + [RIGHT, ("blank",)] + parts
    script_ += [SAME_TXID, ENDING_OK, ("shot", "95_this_seed_clean"), LEFT, WIPE]            # Finish, Wipe
    from test_nonceverify_ui import TYPE_SEED, WORDS_12, type_word
    script_ += [FULL, TYPE_SEED, WORDS_12]
    for w in SEED_B.split():
        script_ += type_word(w)
    script_ += [RIGHT, LEFT] + SEEDSIGNER + [RIGHT, ("blank",)] + parts                    # Confirm, None
    script_ += [SAME_TXID, ENDING_OK, ("shot", "96_both_checked")]
    hw = run(script_)
    assert banner(hw.shots["95_this_seed_clean"]) == ui.YELLOW
    assert banner(hw.shots["96_both_checked"]) == ui.GREEN


def test_cosigner_high_s_is_red():
    def high_s(B, z):
        r, s = crypto.der_decode(_rfc6979_sig(B.key.secret, z, 0x01)[:-1])
        return der(r, crypto.N - s) + b"\x01"
    report = verify_psbt(parse_psbt(_multisig(high_s, True).serialize()), master(), grinding="embit")
    assert report.verdict == VERDICT__FAIL and "high s" in report.inputs[0].message


# ---------------------------------------------------------------------------------------
# C4 / C5 / C6
# ---------------------------------------------------------------------------------------
def test_unknown_psbt_fields_are_counted_and_shown():
    psbt, keys, amounts = p2wpkh_psbt(1)
    libsecp_sign_psbt(psbt, keys, amounts)
    blob = hashlib.sha512(b"payload").digest()
    psbt.inputs[0].unknown = {b"\xfc\x04evil\x00": blob, b"\x42": blob}
    psbt.unknown = {b"\xfc\x04evil\x01": blob}
    report = verify_psbt(parse_psbt(psbt.serialize()), master(), grinding="libsecp")
    assert report.extra_fields == 3
    screen = app.ResultScreen(report, "coldcard_q", txid_confirmed=True)
    assert any("the check doesn't" in line[0] for line in screen.list.lines)


def test_preimages_and_stray_bip32_are_counted():
    """ Review of v7.5: free bytes in "known" fields (hash preimages, BIP32 of fake keys) """
    psbt, keys, amounts = p2wpkh_psbt(1)
    libsecp_sign_psbt(psbt, keys, amounts)
    raw = psbt.serialize()
    base = verify_psbt(parse_psbt(raw), master(), grinding="libsecp")
    assert base.extra_fields == 0 and base.verdict == VERDICT__OK
    p2 = PSBT.parse(raw)
    p2.inputs[0].unknown = {b"\x0b" + hashlib.sha256(b"x" * 360).digest(): b"x" * 360}       # SHA256 preimage
    from embit.ec import PrivateKey
    fake = PrivateKey(b"\x07" * 32).get_public_key()
    p2.inputs[0].bip32_derivations[fake] = DerivationPath(b"\xde\xad\xbe\xef", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
    report = verify_psbt(parse_psbt(p2.serialize()), master(), grinding="libsecp")
    assert report.extra_fields == 2


def test_finalized_legacy_input_is_unsupported_not_red():
    root = embit_root()
    k = root.derive("m/44h/0h/0h/0/0")
    pub = k.key.get_public_key()
    prev = Transaction(vin=[TransactionInput(b"\x33" * 32, 0)], vout=[TransactionOutput(50_000, script.p2pkh(pub))])
    tx = Transaction(vin=[TransactionInput(prev.txid(), 0)], vout=[TransactionOutput(40_000, script.p2pkh(pub))],
                     locktime=870000)
    psbt = PSBT(tx)
    psbt.inputs[0].non_witness_utxo = prev
    psbt.inputs[0].bip32_derivations[pub] = DerivationPath(root.my_fingerprint, bip32.parse_path("m/44h/0h/0h/0/0"))
    psbt.sign_with(root)
    (p, sig), = psbt.inputs[0].partial_sigs.items()
    psbt.inputs[0].final_scriptsig = script.Script(bytes([len(sig)]) + sig + bytes([33]) + p.sec())
    psbt.inputs[0].partial_sigs = {}
    report = verify_psbt(parse_psbt(psbt.serialize()), master(), grinding="embit")
    assert report.verdict == VERDICT__INCOMPLETE and report.inputs[0].status == STATUS__UNVERIFIABLE
    assert "legacy" in report.inputs[0].message


def test_txid_of_unfinalized_nested_psbt_is_the_broadcast_one():
    psbt, keys, amounts = p2wpkh_psbt(2, nested=True)
    psbt.sign_with(embit_root())
    shown = verify_psbt(parse_psbt(psbt.serialize()), master(), grinding="embit")
    assert shown.txid == Transaction.parse(finalize(PSBT.parse(psbt.serialize()))).txid().hex()


# ---------------------------------------------------------------------------------------
# Addresses: B1 BIP21, B2-B4 descriptor review, B5-B7
# ---------------------------------------------------------------------------------------
def test_bip21_with_other_payment_data_is_red(monkeypatch):
    ours, _ = A.SinglesigExplorer(master(), 84, 0).address(False, 3)
    for uri in (f"bitcoin:{ours.upper()}?amount=0.5&lightning=LNBC1XYZ", f"bitcoin:{ours}?pj=https://x/pj",
                f"bitcoin:{ours}?req-whatever=1"):
        addr = A.parse_address(uri)
        assert addr.extras
        screen = app.AddressResultScreen(addr, A.search_singlesig(master(), addr), "single-sig", "")
        assert screen.banner()[0] == ui.RED and screen.banner()[2] == "DO NOT SHARE THIS QR"
    plain = A.parse_address(f"bitcoin:{ours}?amount=0.5&label=Rent&message=hi")
    assert not plain.extras and plain.amount == "0.5"
    screen = app.AddressResultScreen(plain, A.search_singlesig(master(), plain), "single-sig", "")
    assert screen.banner()[0] == ui.GREEN
    assert any("amount in the QR: 0.5" in line[0] for line in screen.list.lines)


def forged(mode):
    with open(os.path.join(DATA, f"forged_descriptor_{mode}.json")) as f:
        return json.load(f)


@pytest.mark.parametrize("mode", ["suffix", "checksum"])
def test_lookalike_cosigners_fail_the_random_groups(mode):
    """
    PoC B2/B3 of the review: start, end (and the checksum) of the forged xpubs match the
    paper. The groups the device picks come from the key-dependent part, where they differ.
    """
    data = forged(mode)
    genuine, tampered = A.parse_descriptor(data["genuine"]), A.parse_descriptor(data["tampered"])
    if mode == "checksum":
        assert genuine.checksum == tampered.checksum
    for i in (1, 2):
        g = A.grouped(genuine.keys[i].xpub_text).split(" ")
        t = A.grouped(tampered.keys[i].xpub_text).split(" ")
        assert g[:4] == t[:4], "the forged start"
        for seed in range(50):
            picked = app.pick_groups(genuine.keys[i].xpub_text, app.random.Random(seed))
            assert len(picked) == 3 and len(set(picked)) == 3
            assert sum(1 for n in picked if g[n] != t[n]) >= 2, "a forged key is caught"


def _chaincode_copy(xpub_text, seed):
    """ Review of v7.5: the real chain code and metadata (all public) with the attacker's key """
    from nonceverify.psbt import base58check
    raw = A.b58decode_check(xpub_text)
    priv = int.from_bytes(hashlib.sha256(seed).digest(), "big") % crypto.N
    return base58check(raw[:45] + crypto.pubkey_compressed(crypto.point_mul_g(priv)))


def test_chaincode_copy_is_caught_by_every_pick():
    """
    The leading ~60 characters of an xpub don't depend on the key: v7.5 first draft
    picked groups there and a chain-code copy passed ~26% of the time per key. Now
    every eligible group depends on the key.
    """
    text, _ = multisig_descriptor()
    desc = A.parse_descriptor(text)
    for i in (1, 2):
        real = desc.keys[i].xpub_text
        start = app.key_dependent_start(real)
        assert 50 <= start <= 70, start
        for s_ in range(30):
            fake = _chaincode_copy(real, b"attacker %d %d" % (i, s_))
            assert fake[:start - 2] == real[:start - 2], "the copied part matches for free"
            g, f = A.grouped(real).split(" "), A.grouped(fake).split(" ")
            for seed in range(20):
                picked = app.pick_groups(real, app.random.Random(seed))
                assert all(n * 4 >= start for n in picked)
                assert sum(1 for n in picked if g[n] != f[n]) >= 2


def test_tampered_descriptor_is_refused_when_a_group_differs():
    data = forged("checksum")
    session = app.Session()
    script_ = load_seed_script(shots=False) + [MIDDLE, ("tap", 240, 194), ("blank",), ("qr", data["tampered"])]
    script_ += [RIGHT, ("shot", "97_key_check_tampered"), LEFT, ("shot", "98_not_your_backup"), FULL, ("stop",)]
    hw = FakeHardware(script_)
    with pytest.raises(Stop):
        app.run(hw, session)
    assert session.descriptor is None
    shot = hw.shots["98_not_your_backup"]
    assert any(shot.getpixel((x, y)) == ui.RED for x in range(10, 300) for y in range(40, 64))


def test_extra_key_with_threshold_one_is_refused_at_the_threshold():
    from embit.descriptor.checksum import add_checksum
    text, _ = multisig_descriptor()
    body = text.split("#")[0]
    x = 0xA77ACC
    pub = crypto.pubkey_compressed(crypto.point_mul_g(x))
    from nonceverify.psbt import base58check
    xpub = base58check(bytes.fromhex("0488b21e") + b"\x04" + b"\x12\x34\x56\x78" + (0x80000002).to_bytes(4, "big")
                       + hashlib.sha256(b"c").digest() + pub)
    keys_start = body.index("(2,") + 3
    evil = add_checksum(body[:keys_start - 2] + "1," + f"[12345678/48h/0h/0h/2h]{xpub}/<0;1>/*," + body[keys_start:])
    desc = A.parse_descriptor(evil)
    screen = app.ThresholdScreen(desc)
    assert [b.label for b in screen.buttons] == ["No", "Yes, 1 of 4"]
    session = app.Session()
    # the attacker key and the two cosigners "match" (the user taps Same), the threshold doesn't
    script_ = load_seed_script(shots=False) + [MIDDLE, ("tap", 240, 194), ("blank",), ("qr", evil)]
    script_ += [RIGHT, RIGHT, RIGHT, RIGHT, ("shot", "99_threshold_1_of_4"), LEFT, FULL, ("stop",)]
    hw = FakeHardware(script_)
    with pytest.raises(Stop):
        app.run(hw, session)
    assert session.descriptor is None


def test_genuine_descriptor_goes_through_the_key_checks():
    text, _ = multisig_descriptor()
    session = app.Session()
    script_ = load_seed_script(shots=False) + [MIDDLE, ("tap", 240, 194), ("blank",), ("qr", text)]
    script_ += DESCRIPTOR_CONFIRM + [("shot", "99b_scan_address"), ("stop",)]
    hw = FakeHardware(script_)
    with pytest.raises(Stop):
        app.run(hw, session)
    assert session.descriptor is not None
    shot = hw.shots["60b_key_check"]
    assert any(shot.getpixel((x, y)) == ui.ACCENT for x in range(10, 470) for y in range(66, 200)), "groups highlighted"


def test_unusual_account_is_yellow():
    m = master()
    text, _ = A.SinglesigExplorer(m, 44, 9).address(True, 19)
    match = A.search_singlesig(m, A.parse_address(text))
    screen = app.AddressResultScreen(A.parse_address(text), match, "single-sig", "")
    assert screen.banner()[0] == ui.YELLOW


def test_qr_self_check_is_case_exact(monkeypatch):
    import qrcode
    monkeypatch.setattr(app, "decode_qr", _zbar)
    text, _ = A.SinglesigExplorer(master(), 44, 0).address(False, 0)
    assert app.address_qr(text, 222) is not None
    real_add = qrcode.QRCode.add_data
    monkeypatch.setattr(qrcode.QRCode, "add_data", lambda self, data, *a, **k: real_add(self, data.swapcase(), *a, **k))
    assert app.address_qr(text, 222) is None, "a QR of another string is never shown"


def test_reversed_multipath_is_refused():
    from embit.descriptor.checksum import add_checksum
    text, _ = multisig_descriptor()
    with pytest.raises(A.DescriptorError, match="only <0;1>"):
        A.parse_descriptor(add_checksum(text.split("#")[0].replace("<0;1>", "<1;0>")))


# ---------------------------------------------------------------------------------------
# S1 / S2: the microSD
# ---------------------------------------------------------------------------------------
def test_microsd_is_remounted_read_only(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("rootfs / rootfs rw 0 0\n/dev/mmcblk0p1 /mnt/microsd vfat rw,sync,relatime 0 0\n")
    calls = []

    def remount(mountpoint):
        calls.append(mountpoint)
        mounts.write_text("rootfs / rootfs rw 0 0\n/dev/mmcblk0p1 /mnt/microsd vfat ro,sync,relatime 0 0\n")
        return 0
    assert storage.protect_microsd(mounts=str(mounts), remount=remount) == storage.STATUS__READ_ONLY
    assert calls == ["/mnt/microsd"]
    # a failed remount is reported, not hidden
    mounts.write_text("/dev/mmcblk0p1 /mnt/microsd vfat rw,sync 0 0\n")
    assert storage.protect_microsd(mounts=str(mounts), remount=lambda m: 13) == storage.STATUS__WRITABLE
    mounts.write_text("rootfs / rootfs rw 0 0\n")
    assert storage.protect_microsd(mounts=str(mounts), remount=remount) == storage.STATUS__ABSENT


def test_splash_shows_the_microsd_status(monkeypatch):
    monkeypatch.setattr(app.SplashScreen, "sd_status", storage.STATUS__WRITABLE)
    image = app.SplashScreen().render()
    assert any(image.getpixel((x, 289)) == ui.YELLOW for x in range(100, 380))
    monkeypatch.setattr(app.SplashScreen, "sd_status", storage.STATUS__READ_ONLY)
    image = app.SplashScreen().render()
    assert not any(image.getpixel((x, 289)) == ui.YELLOW for x in range(100, 380))


def test_import_guard_blocks_the_modules_that_write_the_card():
    guard = ImportGuard()
    for name in ("seedsigner.models.settings", "seedsigner.hardware.microsd", "seedsigner.hardware.camera",
                 "seedsigner.gui.renderer"):
        with pytest.raises(ModuleNotFoundError):
            guard.find_spec(name)
    for _ in range(50):                               # a driver retrying every frame: no growth
        with pytest.raises(ModuleNotFoundError):
            guard.find_spec("seedsigner.models.settings")
    assert guard.attempts.count("seedsigner.models.settings") == 1
    assert guard.find_spec("seedsigner.hardware.ili9486") is None


# ---------------------------------------------------------------------------------------
# Usability
# ---------------------------------------------------------------------------------------
def test_change_seed_asks_and_keep_keeps_the_seed():
    session = app.Session()
    hw = run(load_seed_script(shots=False) + [LEFT, ("shot", "100_wipe_question"), RIGHT, ("shot", "101_kept")],
             session)
    assert session.master is not None, "Keep: the seed stays"
    shot = hw.shots["100_wipe_question"]
    assert any(shot.getpixel((x, y)) == ui.YELLOW for x in range(10, 60) for y in range(40, 64))


def test_seed_words_and_passphrase_hidden_until_show():
    from nonceverify.wordentry import SeedReviewScreen
    words = "abandon " * 11 + "about"
    screen = SeedReviewScreen(words.split())
    assert not screen.revealed
    show = screen.buttons[1]
    assert screen.on_event(Event(Event.TAP, (show.rect[0] + show.rect[2]) / 2, 288)) == "__redraw__"
    assert screen.revealed
    p = app.ConfirmPassphraseScreen("secret phrase", "deadbeef")
    assert not p.revealed and p.on_event(Event(Event.TAP, 240, 288)) == "__redraw__" and p.revealed


def test_seedqr_preview_is_a_mosaic():
    from PIL import Image
    import qrcode
    q = qrcode.make("0123456789012345678901234567890123456789").convert("RGB").resize((320, 320))
    screen = app.ScanScreen("SeedQR", lambda p: (False, 0, ""), private=True)
    image = screen.compose(q)
    crop = image.crop((screen.LEFT, 0, screen.LEFT + 320, 320))
    assert len(set(crop.getdata())) <= 100 + 5, "a coarse mosaic, no readable modules"
    assert _zbar(crop) is None


def test_numpad_returns_digits_only():
    pad = ui.NumberPadScreen("Go to address #")
    for ch in "102":
        b = next(b for b in pad.keys() if b.value == ("digit", ch))
        pad.on_event(Event(Event.TAP, (b.rect[0] + b.rect[2]) / 2, (b.rect[1] + b.rect[3]) / 2))
    assert pad.on_event(Event(Event.TAP, 357, 288)) == ("done", "102")


# ---------------------------------------------------------------------------------------
# Review of v7.5 (second round)
# ---------------------------------------------------------------------------------------
def test_partial_never_exports_and_says_do_not_broadcast():
    psbt = _multisig(lambda B, z: sign_with_k(int.from_bytes(B.key.secret, "big"), z, EVIL_K))
    report = verify_psbt(parse_psbt(psbt.serialize()), master(), grinding="embit")
    assert report.verdict == VERDICT__PARTIAL
    screen = app.ResultScreen(report, "seedsigner", txid_confirmed=True)
    assert not screen.can_export and "txqr" not in [b.value for b in screen.buttons]
    assert "do not broadcast" in screen.banner()[3]
    assert not any("Broadcast exactly" in line[0] for line in screen.list.lines)


def test_this_seed_signed_alone_is_not_complete_not_green():
    """ 2-of-3 signed only by this seed: OK nonces, nothing to broadcast yet -> yellow """
    roots = [embit_root(), embit_root(SEED_B, ""), embit_root(SEED_C, "")]
    path = "m/48h/0h/0h/2h/0/7"
    keys = [r.derive(path) for r in roots]
    pubs = sorted([k.key.get_public_key() for k in keys], key=lambda p: p.sec())
    ws = script.multisig(2, pubs)
    tx = Transaction(vin=[TransactionInput(b"\x22" * 32, 1)], vout=[TransactionOutput(90_000, script.p2wsh(ws))],
                     locktime=870000)
    tx.vin[0].sequence = 0xFFFFFFFD
    psbt = PSBT(tx)
    psbt.inputs[0].witness_utxo = TransactionOutput(100_000, script.p2wsh(ws))
    psbt.inputs[0].witness_script = ws
    for r, k in zip(roots, keys):
        psbt.inputs[0].bip32_derivations[k.key.get_public_key()] = DerivationPath(r.my_fingerprint, bip32.parse_path(path))
    psbt.sign_with(roots[0])
    report = verify_psbt(parse_psbt(psbt.serialize()), master(), grinding="embit")
    assert report.verdict == VERDICT__PARTIAL and report.unchecked_cosigners == 0 and report.broadcast_tx is None
    screen = app.ResultScreen(report, "seedsigner", txid_confirmed=True)
    assert screen.banner()[0] == ui.YELLOW and "not complete" in screen.banner()[3]
    assert "broadcast" not in screen.banner()[3].replace("not complete", "")


def test_microsd_guard_catches_a_later_mount(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("rootfs / rootfs rw 0 0\n")
    now = [0.0]
    seen = []

    def remount(mountpoint):
        mounts.write_text(mounts.read_text().replace("vfat rw", "vfat ro"))
        return 0
    guard = storage.MicroSDGuard(mounts=str(mounts), remount=remount, clock=lambda: now[0], on_change=seen.append)
    assert guard.tick() == storage.STATUS__ABSENT
    mounts.write_text("rootfs / rootfs rw 0 0\n/dev/mmcblk0p1 /mnt/microsd vfat rw,sync 0 0\n")   # mdev, later
    now[0] = 0.5
    assert guard.tick() == storage.STATUS__ABSENT, "at most once a second"
    now[0] = 1.2
    assert guard.tick() == storage.STATUS__READ_ONLY
    assert "vfat ro" in mounts.read_text()
    assert seen == [storage.STATUS__ABSENT, storage.STATUS__READ_ONLY]


def test_housekeeping_runs_in_screen_loops():
    calls = []
    ui.housekeeping_hooks.append(lambda: calls.append(1))
    try:
        hw = FakeHardware([("tap", 240, 250), ("stop",)])
        with pytest.raises(Stop):
            app.run(hw)
    finally:
        ui.housekeeping_hooks.pop()
    assert calls, "the splash loop ran the hooks"


def test_txid_mismatch_is_not_recorded_against_the_signer():
    psbt, keys, amounts = p2wpkh_psbt(1)
    psbt.sign_with(embit_root())
    session = app.Session()
    script_ = load_seed_script(shots=False) + SEEDSIGNER + [RIGHT, ("blank",)] + ur_parts(psbt.serialize())
    script_ += [LEFT, ("shot", "102_different")]
    hw = run(script_, session)
    assert banner(hw.shots["102_different"]) == ui.RED
    assert not session.failed, "a mis-tap on the txid doesn't lock that transaction red for the other seeds"


def test_wrong_checksum_shows_the_words():
    from nonceverify.wordentry import SeedReviewScreen
    screen = SeedReviewScreen(("abandon " * 12).split())
    assert not screen.valid and screen.revealed
    assert screen.buttons[2].label == "Wrong"
