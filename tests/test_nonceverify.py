"""
Tests for the verifier SD (src/nonceverify).

The verifier's own code is checked against independent references that exist only in
the test environment: embit and libsecp256k1 (through coincurve) for keys and
signatures, SeedSigner's UR and BBQr decoders for the transports. The device image
never contains coincurve, and the verifier is not allowed to import embit.
"""
import base64
import hashlib
import os
import random
import struct
import subprocess
import sys
import zlib

import pytest

from nonceverify import crypto, seedqr, transport
from nonceverify.psbt import PSBTError, address, parse_psbt
from nonceverify.verify import (STATUS__FAIL, STATUS__FOREIGN, STATUS__OK, STATUS__UNSIGNED,
    STATUS__UNVERIFIABLE, VERDICT__FAIL, VERDICT__INCOMPLETE, VERDICT__OK, verify_psbt, VERDICT__PARTIAL)

embit = pytest.importorskip("embit")
coincurve = pytest.importorskip("coincurve")

from embit import bip32, bip39, ec, script                                   # noqa: E402
from embit.networks import NETWORKS                                          # noqa: E402
from embit.psbt import PSBT, DerivationPath                                  # noqa: E402
from embit.transaction import Transaction, TransactionInput, TransactionOutput  # noqa: E402

MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
PASSPHRASE = "tre topi ciechi!"


def master(mnemonic=MNEMONIC, passphrase=PASSPHRASE):
    return crypto.HDKey.from_seed(crypto.bip39_seed(mnemonic, passphrase))


def embit_root(mnemonic=MNEMONIC, passphrase=PASSPHRASE):
    return bip32.HDKey.from_seed(bip39.mnemonic_to_seed(mnemonic, passphrase))


# ---------------------------------------------------------------------------------------
# Crypto against embit / libsecp256k1
# ---------------------------------------------------------------------------------------
def test_point_multiplication_matches_libsecp256k1():
    for _ in range(30):
        k = random.randrange(1, crypto.N)
        assert crypto.privkey_to_pubkey(k) == coincurve.PrivateKey(k.to_bytes(32, "big")).public_key.format()
        p = crypto.pubkey_decode(coincurve.PrivateKey(os.urandom(32)).public_key.format())
        m = random.randrange(1, crypto.N)
        expected = coincurve.PublicKey(crypto.pubkey_compressed(p)).multiply(m.to_bytes(32, "big")).format()
        assert crypto.pubkey_compressed(crypto.point_mul(m, p)) == expected
    assert crypto.point_mul_g(crypto.N) is None


def test_ripemd160_pure():
    for msg in [b"", b"abc", b"message digest", os.urandom(200)]:
        assert crypto.ripemd160_pure(msg) == hashlib.new("ripemd160", msg).digest()


def test_bip39_and_bip32_match_embit():
    for mnemonic, passphrase in [(MNEMONIC, ""), (MNEMONIC, PASSPHRASE),
                                 (bip39.mnemonic_from_bytes(os.urandom(32)), "Ascii only ~!@#$%^&*()_+{}|:\"<>?")]:
        assert crypto.bip39_seed(mnemonic, passphrase) == bip39.mnemonic_to_seed(mnemonic, passphrase)
        ours, ref = master(mnemonic, passphrase), embit_root(mnemonic, passphrase)
        assert ours.fingerprint == ref.my_fingerprint
        for path in ["m/84h/0h/0h/0/7", "m/48h/0h/0h/2h/1/3", "m/49h/1h/5h/1/0", "m/0/1/2h"]:
            node = ours.derive(bip32.parse_path(path))
            assert node.priv.to_bytes(32, "big") == ref.derive(path).key.secret


def test_bip39_passphrase_is_nfkd_normalized():
    """ BIP39 normalizes the passphrase (NFKD); the keyboard is ASCII only anyway """
    import unicodedata
    seed = crypto.bip39_seed(MNEMONIC, "caf\u00e9")
    ref = hashlib.pbkdf2_hmac("sha512", MNEMONIC.encode(),
                              unicodedata.normalize("NFKD", "mnemoniccaf\u00e9").encode(), 2048)
    assert seed == ref == crypto.bip39_seed(MNEMONIC, "cafe\u0301")


def _libsecp_sign(secret: bytes, z32: bytes, counter: int) -> bytes:
    from coincurve._libsecp256k1 import ffi, lib
    nd = ffi.new("unsigned char[32]", struct.pack("<I", counter) + b"\x00" * 28) if counter else ffi.NULL
    return coincurve.PrivateKey(secret).sign(z32, hasher=None, custom_nonce=(lib.secp256k1_nonce_function_rfc6979, nd))


def test_rfc6979_matches_libsecp256k1_with_and_without_grinding():
    for _ in range(20):
        secret, z32 = os.urandom(32), os.urandom(32)
        priv = int.from_bytes(secret, "big") % crypto.N
        for counter in (0, 1, 2, 7):
            r, _s = crypto.der_decode(_libsecp_sign(secret, z32, counter))
            assert crypto.point_mul_g(crypto.rfc6979_k(priv, z32, b"" if counter == 0 else
                                                       struct.pack("<I", counter) + b"\x00" * 28))[0] == r


def test_ecdsa_verify():
    secret, z32 = os.urandom(32), os.urandom(32)
    r, s = crypto.der_decode(coincurve.PrivateKey(secret).sign(z32, hasher=None))
    point = crypto.pubkey_decode(coincurve.PrivateKey(secret).public_key.format())
    z = int.from_bytes(z32, "big")
    assert crypto.ecdsa_verify(point, z, r, s)
    assert not crypto.ecdsa_verify(point, z + 1, r, s)


# ---------------------------------------------------------------------------------------
# SeedQR
# ---------------------------------------------------------------------------------------
def test_seedqr_standard_and_compact():
    for nbytes in (16, 32):
        entropy = os.urandom(nbytes)
        mnemonic = bip39.mnemonic_from_bytes(entropy)
        digits = "".join(f"{bip39.WORDLIST.index(w):04d}" for w in mnemonic.split())
        assert seedqr.decode(digits.encode()) == mnemonic
        assert seedqr.decode(entropy) == mnemonic
    with pytest.raises(seedqr.SeedQRError):
        seedqr.decode(b"0" * 48)                     # "abandon" x12: bad checksum
    with pytest.raises(seedqr.SeedQRError):
        seedqr.decode(b"ur:crypto-psbt/abc")
    assert seedqr.wordlist()[0] == "abandon" and seedqr.wordlist()[-1] == "zoo"


def test_wordlist_is_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(seedqr, "_WORDLIST", None)
    monkeypatch.setattr(seedqr, "WORDLIST_SHA256", "00" * 32)
    with pytest.raises(seedqr.SeedQRError):
        seedqr.wordlist()


# ---------------------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------------------
def test_ur_fountain_against_reference_encoder():
    from seedsigner.helpers.ur2.ur import UR
    from seedsigner.helpers.ur2.ur_encoder import UREncoder
    from urtypes.crypto import PSBT as UR_PSBT
    for trial in range(15):
        payload = b"psbt\xff" + os.urandom(random.randint(50, 3000))
        enc = UREncoder(UR("crypto-psbt", UR_PSBT(payload).to_cbor()), max_fragment_len=random.choice([20, 60, 120]))
        parts = [enc.next_part() for _ in range(enc.fountain_encoder.seq_len() * 4 + 20)]
        if trial % 3 == 0:
            feed = parts[enc.fountain_encoder.seq_len():]          # only mixed parts
        elif trial % 3 == 1:
            feed = parts[:]
            random.shuffle(feed)
        else:
            feed = [p for p in parts if random.random() < 0.6]
        c = transport.PSBTCollector()
        for p in feed:
            c.add((p.upper() if trial % 2 else p).encode())
            if c.complete:
                break
        assert c.result == payload


def bbqr_parts(data, ftype="P", enc="Z", chunk=400):
    def b36(n):
        a = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        return a[n // 36] + a[n % 36]
    if enc == "H":
        body, step = data.hex().upper(), chunk - chunk % 2
    else:
        if enc == "Z":
            c = zlib.compressobj(wbits=-10)
            data = c.compress(data) + c.flush()
        body, step = base64.b32encode(data).decode().rstrip("="), chunk - chunk % 8
    chunks = [body[i:i + step] for i in range(0, len(body), step)]
    return [f"B${enc}{ftype}{b36(len(chunks))}{b36(i)}" + c for i, c in enumerate(chunks)]


def test_bbqr_and_base64():
    from seedsigner.models.decode_qr import DecodeQR
    for enc in ("Z", "2", "H"):
        payload = b"psbt\xff" + bytes(random.randrange(4) for _ in range(3000))
        parts = bbqr_parts(payload, "P", enc)
        d = DecodeQR()
        for p in parts:
            d.add_data(p.encode())
        assert d.is_complete, "SeedSigner's decoder accepts the test encoding"
        random.shuffle(parts)
        c = transport.PSBTCollector()
        for p in parts + parts[:1]:
            c.add(p.encode())
        assert c.result == payload
    # v7.2: a final transaction (BBQr "T") is collected too: check_psbt pairs it with the
    # unsigned PSBT
    final = b"\x02\x00\x00\x00" + os.urandom(80)
    c = transport.PSBTCollector()
    c.add(bbqr_parts(final, "T")[0])
    assert c.result == final and transport.looks_like_final_tx(c.result)
    c = transport.PSBTCollector()
    c.add(base64.b64encode(b"psbt\xff" + b"x" * 30))
    assert c.result == b"psbt\xff" + b"x" * 30
    assert transport.PSBTCollector().add(b"bc1qnotapsbt") is False


# ---------------------------------------------------------------------------------------
# PSBT fixtures signed by independent signers
# ---------------------------------------------------------------------------------------
def p2wpkh_psbt(n_inputs=3, root=None, nested=False, change=True):
    root = root or embit_root()
    paths = [f"m/{49 if nested else 84}h/0h/0h/0/{i}" for i in range(n_inputs)]
    keys = [root.derive(p) for p in paths]
    amounts = [100_000 * (i + 1) for i in range(n_inputs)]
    vout = [TransactionOutput(15_000, script.p2wpkh(embit_root("legal winner thank year wave sausage worth useful legal winner thank yellow", "").derive("m/84h/0h/0h/0/0").key.get_public_key()))]
    change_key = root.derive(f"m/{49 if nested else 84}h/0h/0h/1/0")
    if change:
        vout.append(TransactionOutput(sum(amounts) - 15_000 - 700, script.p2wpkh(change_key.key.get_public_key())))
    tx = Transaction(vin=[TransactionInput(hashlib.sha256(bytes([i])).digest(), i) for i in range(n_inputs)],
                     vout=vout, locktime=870_000)
    psbt = PSBT(tx)
    for i, k in enumerate(keys):
        pub = k.key.get_public_key()
        spk = script.p2sh(script.p2wpkh(pub)) if nested else script.p2wpkh(pub)
        psbt.inputs[i].witness_utxo = TransactionOutput(amounts[i], spk)
        if nested:
            psbt.inputs[i].redeem_script = script.p2wpkh(pub)
        psbt.inputs[i].bip32_derivations[pub] = DerivationPath(root.my_fingerprint, bip32.parse_path(paths[i]))
    if change:
        psbt.outputs[1].bip32_derivations[change_key.key.get_public_key()] = DerivationPath(
            root.my_fingerprint, bip32.parse_path(f"m/{49 if nested else 84}h/0h/0h/1/0"))
    return psbt, keys, amounts


def sighash_of(psbt, i, amount, script_code):
    from nonceverify.psbt import bip143_sighash, parse_tx
    tx = parse_tx(psbt.tx.serialize())
    return bip143_sighash(tx, i, script_code, amount, 1)


def der(r, s):
    def enc(x):
        b = x.to_bytes(33, "big").lstrip(b"\x00")
        if b[0] & 0x80:
            b = b"\x00" + b
        return b"\x02" + bytes([len(b)]) + b
    body = enc(r) + enc(s)
    return b"\x30" + bytes([len(body)]) + body


def sign_with_k(priv, z32, k, low_s=True):
    z = int.from_bytes(z32, "big")
    r = crypto.point_mul_g(k)[0] % crypto.N
    s = pow(k, -1, crypto.N) * (z + r * priv) % crypto.N
    if low_s and s > crypto.N // 2:
        s = crypto.N - s
    return der(r, s) + b"\x01"


def libsecp_sign_psbt(psbt, keys, amounts, grind=True):
    """ Signs like a libsecp256k1 wallet (Bitcoin Core, Coldcard): RFC 6979 + low-R grinding """
    from nonceverify.psbt import p2pkh_script
    for i, k in enumerate(keys):
        pub = k.key.get_public_key()
        z32 = sighash_of(psbt, i, amounts[i], p2pkh_script(pub.sec()))
        counter = 0
        while True:
            sig = _libsecp_sign(k.key.secret, z32, counter)
            if not grind or crypto.der_decode(sig)[0] < (1 << 255):
                break
            counter += 1
        psbt.inputs[i].partial_sigs[pub] = sig + b"\x01"
    psbt._grinding = "libsecp" if grind else "none"
    return psbt


def run_verify(psbt, m=None, grinding=None):
    """ Signers here grind like embit unless libsecp_sign_psbt() marked the PSBT """
    grinding = grinding or getattr(psbt, "_grinding", "embit")
    return verify_psbt(parse_psbt(psbt.serialize()), m or master(), grinding=grinding)


# ---------------------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------------------
def test_embit_signed_psbt_passes():
    psbt, keys, amounts = p2wpkh_psbt(4)
    psbt.sign_with(embit_root())
    report = run_verify(psbt)
    assert report.verdict == VERDICT__OK, report
    assert all(r.status == STATUS__OK for r in report.inputs)
    assert report.fee == 700
    assert [o.is_change for o in report.outputs] == [False, True]
    for i, o in enumerate(psbt.tx.vout):
        assert report.outputs[i].address == o.script_pubkey.address(NETWORKS["main"])
    for i, r in enumerate(report.inputs):
        assert r.address == psbt.inputs[i].witness_utxo.script_pubkey.address(NETWORKS["main"])
        assert r.path == f"m/84'/0'/0'/0/{i}"


def test_libsecp256k1_signer_like_coldcard_passes():
    psbt, keys, amounts = p2wpkh_psbt(6)
    report = run_verify(libsecp_sign_psbt(psbt, keys, amounts))
    assert report.verdict == VERDICT__OK
    assert {r.counter for r in report.inputs} != {0}, "some inputs needed grinding retries"


def test_grinding_policy_is_set_by_the_user():
    """ Grind or not, and when to stop, would be free bits if the signer could choose """
    psbt, keys, amounts = p2wpkh_psbt(8)
    plain = libsecp_sign_psbt(PSBT.parse(psbt.serialize()), keys, amounts, grind=False)
    ground = libsecp_sign_psbt(PSBT.parse(psbt.serialize()), keys, amounts, grind=True)
    m = master()
    assert verify_psbt(parse_psbt(plain.serialize()), m, grinding="none").verdict == VERDICT__OK
    assert verify_psbt(parse_psbt(ground.serialize()), m, grinding="libsecp").verdict == VERDICT__OK
    # 8 inputs: some first nonces have a high R, so the wrong policy fails either way
    for blob, policy in ((plain, "libsecp"), (ground, "none")):
        wrong = verify_psbt(parse_psbt(blob.serialize()), m, grinding=policy)
        assert wrong.verdict == VERDICT__FAIL and "grinding" in " ".join(r.message for r in wrong.inputs)


def test_embit_grinding_rule_differs_from_libsecp():
    """ embit stops at DER <= 70 bytes: a high R with a short s is honest for SeedSigner """
    base, keys, amounts = p2wpkh_psbt(1, change=False)
    root = embit_root()
    for value in range(15_000, 40_000):
        p = PSBT.parse(base.serialize())
        p.outputs[0].value = value
        p.sign_with(root)
        sig = list(p.inputs[0].partial_sigs.values())[0]
        if crypto.der_decode(sig[:-1])[0] >= 1 << 255:
            break
    else:
        pytest.skip("no high-R embit signature found")
    assert run_verify(p, grinding="embit").verdict == VERDICT__OK
    assert run_verify(p, grinding="libsecp").verdict == VERDICT__FAIL


def test_nested_segwit_and_finalized_psbt():
    psbt, keys, amounts = p2wpkh_psbt(2, nested=True)
    psbt.sign_with(embit_root())
    assert run_verify(psbt).verdict == VERDICT__OK
    # finalize: signatures move to the final witness
    for inp in psbt.inputs:
        (pub, sig), = inp.partial_sigs.items()
        inp.final_scriptwitness = script.Witness([sig, pub.sec()])
        inp.final_scriptsig = script.Script(script.Script(inp.redeem_script.data).serialize())
        inp.partial_sigs = {}
    report = run_verify(psbt)
    assert report.verdict == VERDICT__OK, report


def test_multisig_p2wsh_checks_only_our_key():
    roots = [embit_root(), embit_root("legal winner thank year wave sausage worth useful legal winner thank yellow", ""),
             embit_root("letter advice cage absurd amount doctor acoustic avoid letter advice cage above", "")]
    path = "m/48h/0h/0h/2h/0/5"
    pubs = [r.derive(path).key.get_public_key() for r in roots]
    ws = script.multisig(2, sorted(pubs, key=lambda p: p.sec()))
    tx = Transaction(vin=[TransactionInput(b"\x11" * 32, 0)], vout=[TransactionOutput(90_000, script.p2wsh(ws))])
    psbt = PSBT(tx)
    psbt.inputs[0].witness_utxo = TransactionOutput(100_000, script.p2wsh(ws))
    psbt.inputs[0].witness_script = ws
    for r, p in zip(roots, pubs):
        psbt.inputs[0].bip32_derivations[p] = DerivationPath(r.my_fingerprint, bip32.parse_path(path))
    psbt.sign_with(roots[0])
    psbt.sign_with(roots[1])
    report = run_verify(psbt)
    # v7.5: the cosigner's signature is valid but its nonce is unchecked: not green yet
    assert report.verdict == VERDICT__PARTIAL and report.inputs[0].status == STATUS__OK
    assert report.unchecked_cosigners == 1
    assert run_verify(psbt, master("letter advice cage absurd amount doctor acoustic avoid letter advice cage above", "")).verdict == VERDICT__INCOMPLETE
    # finalized multisig witness: [empty, sig, sig, witness_script]
    sigs = [psbt.inputs[0].partial_sigs[p] for p in sorted(pubs, key=lambda p: p.sec()) if p in psbt.inputs[0].partial_sigs]
    psbt.inputs[0].final_scriptwitness = script.Witness([b""] + sigs + [ws.data])
    psbt.inputs[0].partial_sigs = {}
    assert run_verify(psbt).verdict == VERDICT__PARTIAL


def test_chosen_nonce_fails():
    """ The Dark Skippy case: valid signature, nonce chosen by the signer """
    psbt, keys, amounts = p2wpkh_psbt(2)
    psbt.sign_with(embit_root())
    from nonceverify.psbt import p2pkh_script
    k = keys[1]
    z32 = sighash_of(psbt, 1, amounts[1], p2pkh_script(k.key.get_public_key().sec()))
    evil_k = int.from_bytes(hashlib.sha256(b"attacker" + z32).digest(), "big") % crypto.N   # deterministic too
    psbt.inputs[1].partial_sigs[k.key.get_public_key()] = sign_with_k(int.from_bytes(k.key.secret, "big"), z32, evil_k)
    report = run_verify(psbt)
    assert report.verdict == VERDICT__FAIL
    assert report.inputs[0].status == STATUS__OK and report.inputs[1].status == STATUS__FAIL
    assert "NOT" in report.inputs[1].message


def test_skipping_a_grinding_retry_fails():
    """ Choosing among the RFC 6979 retries is a covert channel too """
    psbt, keys, amounts = p2wpkh_psbt(1, change=False)
    from nonceverify.psbt import p2pkh_script
    k = keys[0]
    priv = int.from_bytes(k.key.secret, "big")
    z32 = sighash_of(psbt, 0, amounts[0], p2pkh_script(k.key.get_public_key().sec()))
    lows = [c for c in range(40) if crypto.point_mul_g(crypto.rfc6979_k(priv, z32, b"" if c == 0 else
                                                       struct.pack("<I", c) + b"\x00" * 28))[0] < (1 << 255)]
    skipped = lows[1]                                  # not the first low-R retry
    kk = crypto.rfc6979_k(priv, z32, b"" if skipped == 0 else struct.pack("<I", skipped) + b"\x00" * 28)
    psbt.inputs[0].partial_sigs[k.key.get_public_key()] = sign_with_k(priv, z32, kk)
    report = run_verify(psbt)
    assert report.verdict == VERDICT__FAIL and "retry" in report.inputs[0].message


def test_high_s_reused_r_and_bad_signatures_fail():
    from nonceverify.psbt import p2pkh_script
    psbt, keys, amounts = p2wpkh_psbt(2)
    psbt.sign_with(embit_root())
    k = keys[0]
    priv = int.from_bytes(k.key.secret, "big")
    z32 = sighash_of(psbt, 0, amounts[0], p2pkh_script(k.key.get_public_key().sec()))
    kk = crypto.rfc6979_k(priv, z32)
    high = sign_with_k(priv, z32, kk, low_s=False)
    r, s = crypto.der_decode(high[:-1])
    if s <= crypto.N // 2:
        high = der(r, crypto.N - s) + b"\x01"
    psbt.inputs[0].partial_sigs[k.key.get_public_key()] = high
    assert run_verify(psbt).inputs[0].message.startswith("high s")

    # same nonce on two inputs (same key reused for both inputs)
    psbt, keys, amounts = p2wpkh_psbt(2)
    psbt.sign_with(embit_root())
    k0, k1 = keys
    z1 = sighash_of(psbt, 1, amounts[1], p2pkh_script(k1.key.get_public_key().sec()))
    z0 = sighash_of(psbt, 0, amounts[0], p2pkh_script(k0.key.get_public_key().sec()))
    same_k = crypto.rfc6979_k(int.from_bytes(k0.key.secret, "big"), z0)
    psbt.inputs[1].partial_sigs[k1.key.get_public_key()] = sign_with_k(int.from_bytes(k1.key.secret, "big"), z1, same_k)
    report = run_verify(psbt)
    assert report.verdict == VERDICT__FAIL and report.inputs[1].status == STATUS__FAIL

    # amount in the PSBT altered after signing: the signature no longer matches
    psbt, keys, amounts = p2wpkh_psbt(1)
    psbt.sign_with(embit_root())
    psbt.inputs[0].witness_utxo.value += 1
    report = run_verify(psbt)
    assert report.verdict == VERDICT__FAIL and "not valid" in report.inputs[0].message


def test_wrong_seed_unsigned_and_taproot():
    psbt, keys, amounts = p2wpkh_psbt(1)
    psbt.sign_with(embit_root())
    report = run_verify(psbt, master(MNEMONIC, "passphrase sbagliata"))
    assert report.verdict == VERDICT__FAIL and report.inputs[0].status == STATUS__UNVERIFIABLE
    assert "spends nothing" in report.summary and not report.seed_matches

    psbt, keys, amounts = p2wpkh_psbt(1)
    report = run_verify(psbt)
    assert report.verdict == VERDICT__INCOMPLETE and report.inputs[0].status == STATUS__UNSIGNED

    root = embit_root()
    k = root.derive("m/86h/0h/0h/0/0")
    tx = Transaction(vin=[TransactionInput(b"\x22" * 32, 0)], vout=[TransactionOutput(1000, script.p2tr(k.key.get_public_key()))])
    psbt = PSBT(tx)
    psbt.inputs[0].witness_utxo = TransactionOutput(2000, script.p2tr(k.key.get_public_key()))
    psbt.inputs[0].bip32_derivations[k.key.get_public_key()] = DerivationPath(root.my_fingerprint, bip32.parse_path("m/86h/0h/0h/0/0"))
    report = run_verify(psbt)
    assert report.inputs[0].status == STATUS__UNVERIFIABLE and report.verdict == VERDICT__INCOMPLETE
    assert report.outputs[0].address.startswith("bc1p")


def test_psbt_parser_rejects_garbage():
    with pytest.raises(PSBTError):
        parse_psbt(b"not a psbt")
    with pytest.raises(PSBTError):
        parse_psbt(b"psbt\xff\x01\x00")
    assert address(bytes.fromhex("a914") + b"\x01" * 20 + b"\x87")[0] == "3"


# ---------------------------------------------------------------------------------------
# No signer code in the verifier
# ---------------------------------------------------------------------------------------
def test_verifier_runs_without_embit_or_seedsigner_crypto(tmp_path):
    psbt, keys, amounts = p2wpkh_psbt(3)
    psbt.sign_with(embit_root())
    blob = tmp_path / "signed.psbt"
    blob.write_bytes(psbt.serialize())
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    code = f"""
import sys
sys.path.insert(0, {src!r})
from nonceverify.hw import ImportGuard, loaded_blocked_modules
guard = ImportGuard.install()
from nonceverify import app, crypto, transport, seedqr
from nonceverify.psbt import parse_psbt
from nonceverify.verify import verify_psbt
m = crypto.HDKey.from_seed(crypto.bip39_seed({MNEMONIC!r}, {PASSPHRASE!r}))
report = verify_psbt(parse_psbt(open({str(blob)!r}, "rb").read()), m)
try:
    import embit
    print("EMBIT IMPORTED")
except ImportError:
    pass
print(report.verdict, loaded_blocked_modules(), guard.attempts)
"""
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok [] ['embit']", out.stdout


# ---------------------------------------------------------------------------------------
# Attacks found by the independent review (all must end up anything but green)
# ---------------------------------------------------------------------------------------
EVIL_K = 0xC0FFEE1234567890


def _evil_sig(psbt, keys, amounts, i):
    from nonceverify.psbt import p2pkh_script
    pub = keys[i].key.get_public_key()
    z = sighash_of(psbt, i, amounts[i], p2pkh_script(pub.sec()))
    return pub, sign_with_k(int.from_bytes(keys[i].key.secret, "big"), z, EVIL_K)


def test_review_honest_partial_and_evil_final_witness():
    psbt, keys, amounts = p2wpkh_psbt(1)
    libsecp_sign_psbt(psbt, keys, amounts)
    pub, evil = _evil_sig(psbt, keys, amounts, 0)
    psbt.inputs[0].final_scriptwitness = script.Witness([evil, pub.sec()])
    report = run_verify(psbt)
    assert report.verdict == VERDICT__FAIL and report.inputs[0].status == STATUS__FAIL
    # without the honest decoy the final witness itself is checked
    psbt.inputs[0].partial_sigs = {}
    report = run_verify(psbt)
    assert report.verdict == VERDICT__FAIL and "NOT" in report.inputs[0].message


@pytest.mark.parametrize("relabel", ["strip", "fingerprint", "path"])
def test_review_our_input_with_hidden_ownership(relabel):
    """ Ownership comes from derivation, not from the PSBT's labels """
    psbt, keys, amounts = p2wpkh_psbt(2)
    libsecp_sign_psbt(psbt, keys, amounts)
    pub, evil = _evil_sig(psbt, keys, amounts, 1)
    psbt.inputs[1].partial_sigs[pub] = evil
    if relabel == "strip":
        psbt.inputs[1].bip32_derivations = {}
    elif relabel == "fingerprint":
        psbt.inputs[1].bip32_derivations = {pub: DerivationPath(b"\xde\xad\xbe\xef", bip32.parse_path("m/84h/0h/0h/0/1"))}
    else:
        psbt.inputs[1].bip32_derivations = {pub: DerivationPath(b"\xde\xad\xbe\xef", bip32.parse_path("m/84h/0h/0h/0/999"))}
    report = run_verify(psbt)
    assert report.verdict == VERDICT__FAIL, report
    assert report.inputs[1].status == STATUS__FAIL and report.inputs[1].path == "m/84'/0'/0'/0/1"


def test_review_signature_by_an_unknown_key_is_never_green():
    """ A single-sig input whose key isn't found anywhere: yellow, not green """
    psbt, keys, amounts = p2wpkh_psbt(2)
    libsecp_sign_psbt(psbt, keys, amounts)
    other = embit_root("legal winner thank year wave sausage worth useful legal winner thank yellow", "").derive("m/84h/0h/0h/0/0")
    pub = other.key.get_public_key()
    psbt.inputs[1].witness_utxo = TransactionOutput(amounts[1], script.p2wpkh(pub))
    psbt.inputs[1].bip32_derivations = {}
    from nonceverify.psbt import p2pkh_script
    z = sighash_of(psbt, 1, amounts[1], p2pkh_script(pub.sec()))
    psbt.inputs[1].partial_sigs = {pub: sign_with_k(int.from_bytes(other.key.secret, "big"), z, EVIL_K)}
    report = run_verify(psbt)
    assert report.verdict == VERDICT__INCOMPLETE and report.inputs[1].status == STATUS__UNVERIFIABLE


def test_review_taproot_input_declared_with_tap_bip32():
    psbt, keys, amounts = p2wpkh_psbt(2)
    root = embit_root()
    k = root.derive("m/86h/0h/0h/0/0")
    psbt.inputs[1].bip32_derivations = {}
    psbt.inputs[1].witness_utxo.script_pubkey = script.p2tr(k.key.get_public_key())
    libsecp_sign_psbt(psbt, keys[:1], amounts[:1])
    val = b"\x00" + root.my_fingerprint + b"".join(i.to_bytes(4, "little") for i in bip32.parse_path("m/86h/0h/0h/0/0"))
    psbt.inputs[1].unknown = {b"\x16" + k.key.get_public_key().xonly(): val, b"\x13": b"\x11" * 64}
    report = run_verify(psbt)
    assert report.verdict == VERDICT__INCOMPLETE and report.inputs[1].status == STATUS__UNVERIFIABLE


def _insert_into_input0(raw, entries):
    from nonceverify.psbt import Reader
    rd = Reader(raw)
    rd.take(5)
    for _ in range(2):
        while True:
            kl = rd.varint()
            if kl == 0:
                break
            rd.take(kl)
            rd.varbytes()
    end = rd.i - 1
    return raw[:end] + entries + raw[end:]


def test_review_parser_rejects_duplicates_and_bad_key_lengths():
    from nonceverify.psbt import ser_varbytes, ser_varint
    psbt, keys, amounts = p2wpkh_psbt(1)
    libsecp_sign_psbt(psbt, keys, amounts)
    pub = keys[0].key.get_public_key().sec()
    honest = psbt.inputs[0].partial_sigs[keys[0].key.get_public_key()]
    evil = _evil_sig(psbt, keys, amounts, 0)[1]
    psbt.inputs[0].partial_sigs = {}
    raw = psbt.serialize()
    wit = lambda items: ser_varint(len(items)) + b"".join(ser_varbytes(x) for x in items)
    ent = lambda k, v: ser_varbytes(k) + ser_varbytes(v)
    with pytest.raises(PSBTError):
        parse_psbt(_insert_into_input0(raw, ent(b"\x08", wit([evil, pub])) + ent(b"\x08\x00", wit([honest, pub]))))
    with pytest.raises(PSBTError):
        parse_psbt(_insert_into_input0(raw, ent(b"\x08", wit([evil, pub])) + ent(b"\x08", wit([honest, pub]))))
    with pytest.raises(PSBTError):
        parse_psbt(raw + b"\x00")


def test_review_change_is_derived_not_declared():
    psbt, keys, amounts = p2wpkh_psbt(2)
    libsecp_sign_psbt(psbt, keys, amounts)
    psbt.outputs[0].bip32_derivations[ec.PrivateKey(b"\x07" * 32).get_public_key()] = DerivationPath(
        embit_root().my_fingerprint, bip32.parse_path("m/84h/0h/0h/1/5"))
    report = run_verify(psbt)
    assert [o.is_change for o in report.outputs] == [False, True]


def test_report_carries_the_txid():
    psbt, keys, amounts = p2wpkh_psbt(1)
    libsecp_sign_psbt(psbt, keys, amounts)
    assert run_verify(psbt).txid == psbt.tx.txid().hex()


def test_transport_limits():
    with pytest.raises(transport.TransportError):
        transport.FountainDecoder().add(10**9, 10**8, 10**9, 0, b"x" * 10)
    deep = b"\xc1" * 5000 + b"\x40"
    with pytest.raises(transport.TransportError):
        transport.cbor_decode(deep)
    bomb = zlib.compressobj(wbits=-10)
    data = bomb.compress(b"\x00" * 5_000_000) + bomb.flush()
    parts = bbqr_parts(b"psbt\xff", "P", "Z")         # header shape
    text = parts[0][:8] + base64.b32encode(data).decode().rstrip("=")
    with pytest.raises(transport.TransportError):
        transport.PSBTCollector().add(text.replace(parts[0][4:8], "0100"))



# Second review round
def test_review2_second_key_of_this_seed_in_a_multisig():
    root = embit_root()
    other = embit_root("legal winner thank year wave sausage worth useful legal winner thank yellow", "")
    pA, pB = "m/48h/0h/0h/2h/0/5", "m/48h/0h/1h/2h/0/5"
    A, B, C = root.derive(pA), root.derive(pB), other.derive(pA)
    pubs = sorted([A.key.get_public_key(), B.key.get_public_key(), C.key.get_public_key()], key=lambda p: p.sec())
    ws = script.multisig(2, pubs)
    tx = Transaction(vin=[TransactionInput(b"\x11" * 32, 0)], vout=[TransactionOutput(90_000, script.p2wpkh(C.key.get_public_key()))])
    psbt = PSBT(tx)
    psbt.inputs[0].witness_utxo = TransactionOutput(100_000, script.p2wsh(ws))
    psbt.inputs[0].witness_script = ws
    psbt.inputs[0].bip32_derivations = {A.key.get_public_key(): DerivationPath(root.my_fingerprint, bip32.parse_path(pA))}
    z = sighash_of(psbt, 0, 100_000, ws.data)
    counter = 0
    honest = _libsecp_sign(A.key.secret, z, 0)
    while crypto.der_decode(honest)[0] >= 1 << 255:
        counter += 1
        honest = _libsecp_sign(A.key.secret, z, counter)
    psbt.inputs[0].partial_sigs = {A.key.get_public_key(): honest + b"\x01",
                                   B.key.get_public_key(): sign_with_k(int.from_bytes(B.key.secret, "big"), z, EVIL_K)}
    report = run_verify(psbt)
    assert report.verdict == VERDICT__FAIL, report


def test_review2_taproot_input_without_utxo():
    psbt, keys, amounts = p2wpkh_psbt(2)
    libsecp_sign_psbt(psbt, keys[:1], amounts[:1])
    psbt.inputs[1].witness_utxo = None
    psbt.inputs[1].bip32_derivations = {}
    psbt.inputs[1].unknown = {b"\x13": b"\x11" * 64}
    report = run_verify(psbt)
    assert report.verdict == VERDICT__INCOMPLETE and report.inputs[1].status == STATUS__UNVERIFIABLE


def test_review2_parser_limits_and_tap_leaf_keys():
    from nonceverify.psbt import ser_varbytes
    psbt, keys, amounts = p2wpkh_psbt(1)
    raw = psbt.serialize()
    deep = b"\x00" * 4 + b"\x00\x00\x00\x80" * 1200
    with pytest.raises(PSBTError):
        parse_psbt(_insert_into_input0(raw, ser_varbytes(b"\x06" + b"\x02" * 33) + ser_varbytes(deep)))
    for depth in (0, 1, 3):
        key = b"\x15" + b"\xc0" + b"\x02" * 32 + b"\x03" * 32 * depth
        parse_psbt(_insert_into_input0(raw, ser_varbytes(key) + ser_varbytes(b"\x51\xc0")))



# Third review round (three independent reviewers)
def test_review3_non_strict_der_fails():
    psbt, keys, amounts = p2wpkh_psbt(1)
    psbt.sign_with(embit_root())
    pub, sig = list(psbt.inputs[0].partial_sigs.items())[0]
    r, s = crypto.der_decode(sig[:-1])

    def enc(x, pad):
        b = x.to_bytes(33, "big").lstrip(b"\x00")
        if b[0] & 0x80:
            b = b"\x00" + b
        return b"\x00" * pad + b
    for pad_r, pad_s in [(1, 0), (0, 1), (3, 5)]:
        R, S = enc(r, pad_r), enc(s, pad_s)
        body = b"\x02" + bytes([len(R)]) + R + b"\x02" + bytes([len(S)]) + S
        p = PSBT.parse(psbt.serialize())
        p.inputs[0].partial_sigs[pub] = b"\x30" + bytes([len(body)]) + body + b"\x01"
        report = run_verify(p)
        assert report.verdict == VERDICT__FAIL and "DER" in report.inputs[0].message


def test_review3_transaction_fields_are_constrained():
    base, keys, amounts = p2wpkh_psbt(2)
    for change in ("locktime", "sequence", "mixed", "version"):
        tx = base.tx
        tx = Transaction(version=3 if change == "version" else 2,
                         vin=[TransactionInput(i.txid, i.vout,
                                               sequence=(0x80001234 if change == "sequence" else
                                                         (0xFFFFFFFD + n if change == "mixed" else 0xFFFFFFFD)))
                              for n, i in enumerate(tx.vin)],
                         vout=tx.vout, locktime=600_000_000 if change == "locktime" else 870_000)
        p = PSBT(tx)
        for a, b in zip(p.inputs, base.inputs):
            a.witness_utxo, a.bip32_derivations = b.witness_utxo, b.bip32_derivations
        p.sign_with(embit_root())
        report = run_verify(p)
        assert report.verdict == VERDICT__FAIL and report.tx_problem, change
    honest = PSBT.parse(base.serialize())
    honest.sign_with(embit_root())
    report = run_verify(honest)
    assert report.verdict == VERDICT__OK and report.locktime == 870_000


@pytest.mark.parametrize("hidden_path", ["m/48h/0h/0h/2h/0/45", "m/48h/0h/7h/2h/0/5", "m/48h/0h/9h/1h/0/5"])
def test_review3_second_key_outside_the_plain_scan(hidden_path):
    root = embit_root()
    other = embit_root("legal winner thank year wave sausage worth useful legal winner thank yellow", "")
    pA = "m/48h/0h/0h/2h/0/5"
    A, B, C = root.derive(pA), root.derive(hidden_path), other.derive(pA)
    pubs = sorted([A.key.get_public_key(), B.key.get_public_key(), C.key.get_public_key()], key=lambda p: p.sec())
    ws = script.multisig(2, pubs)
    tx = Transaction(vin=[TransactionInput(b"\x11" * 32, 0)], vout=[TransactionOutput(90_000, script.p2wpkh(C.key.get_public_key()))])
    psbt = PSBT(tx)
    psbt.inputs[0].witness_utxo = TransactionOutput(100_000, script.p2wsh(ws))
    psbt.inputs[0].witness_script = ws
    psbt.inputs[0].bip32_derivations = {A.key.get_public_key(): DerivationPath(root.my_fingerprint, bip32.parse_path(pA)),
                                        C.key.get_public_key(): DerivationPath(other.my_fingerprint, bip32.parse_path(pA))}
    z = sighash_of(psbt, 0, 100_000, ws.data)
    counter = 0
    honest = _libsecp_sign(A.key.secret, z, 0)
    while crypto.der_decode(honest)[0] >= 1 << 255:
        counter += 1
        honest = _libsecp_sign(A.key.secret, z, counter)
    psbt.inputs[0].partial_sigs = {A.key.get_public_key(): honest + b"\x01",
                                   B.key.get_public_key(): sign_with_k(int.from_bytes(B.key.secret, "big"), z, EVIL_K)}
    assert run_verify(psbt, grinding="libsecp").verdict == VERDICT__FAIL


def _cbor_uint(n):
    if n < 24:
        return bytes([n])
    for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if n < 1 << (8 * size):
            return bytes([info]) + n.to_bytes(size, "big")


def _ur_part(seq_num, seq_len, items_cbor):
    import binascii
    body = bytes([0x85]) + items_cbor
    data = body + binascii.crc32(body).to_bytes(4, "big")
    words = [transport._WORDS[b] for b in data]
    return f"ur:crypto-psbt/{seq_num}-{seq_len}/" + "".join(w[0] + w[3] for w in words)


def test_review3_hostile_ur_parts_never_raise_unexpected_errors():
    frag = bytes([0x4A]) + b"A" * 10
    cases = [
        _ur_part(1, 3, bytes([0x41]) + b"x" + _cbor_uint(3) + _cbor_uint(30) + _cbor_uint(1) + frag),
        _ur_part(1, 3, _cbor_uint(1) + bytes([0x80]) + _cbor_uint(30) + _cbor_uint(1) + frag),
        _ur_part(1, 3, _cbor_uint(2 ** 32) + _cbor_uint(3) + _cbor_uint(30) + _cbor_uint(1) + frag),
        _ur_part(4, 3, _cbor_uint(4) + _cbor_uint(3) + _cbor_uint(30) + _cbor_uint(2 ** 40) + frag),
        _ur_part(9, 3, _cbor_uint(1) + _cbor_uint(3) + _cbor_uint(30) + _cbor_uint(1) + frag),   # header lies
    ]
    for part in cases:
        c = transport.PSBTCollector()
        try:
            assert c.add(part.encode()) is False
        except transport.TransportError:
            pass


def test_review3_a_new_message_restarts_the_collectors():
    from seedsigner.helpers.ur2.ur import UR
    from seedsigner.helpers.ur2.ur_encoder import UREncoder
    from urtypes.crypto import PSBT as UR_PSBT
    first, second = b"psbt\xff" + os.urandom(900), b"psbt\xff" + os.urandom(950)
    e1 = UREncoder(UR("crypto-psbt", UR_PSBT(first).to_cbor()), max_fragment_len=100)
    e2 = UREncoder(UR("crypto-psbt", UR_PSBT(second).to_cbor()), max_fragment_len=100)
    c = transport.PSBTCollector()
    for _ in range(3):
        c.add(e1.next_part().encode())
    for _ in range(60):
        c.add(e2.next_part().encode())
        if c.complete:
            break
    assert c.result == second and c.restarted

    a, b = bbqr_parts(b"psbt\xff" + os.urandom(2000), "P", "2", 300), bbqr_parts(b"psbt\xff" + os.urandom(3000), "P", "2", 300)
    c = transport.PSBTCollector()
    c.add(a[0].encode())
    for part in b:
        c.add(part.encode())
    assert c.complete and c.restarted


def test_review3_bbqr_failure_does_not_get_stuck():
    good = b"psbt\xff" + os.urandom(500)
    parts = bbqr_parts(good, "P", "Z", 200)
    broken = parts[0][:8] + "A" * (len(parts[0]) - 8)
    c = transport.PSBTCollector()
    with pytest.raises(transport.TransportError):
        for part in [broken] + parts[1:]:
            c.add(part.encode())
    for part in parts:
        c.add(part.encode())
    assert c.result == good


# v3: SIGHASH_ALL everywhere (Ollen, 7 Nov 2026)
from nonceverify.psbt import p2pkh_script
DANGEROUS_SIGHASHES = (0x02, 0x03, 0x81, 0x82, 0x83)       # NONE, SINGLE, ALL|ACP, NONE|ACP, SINGLE|ACP


def _sighash_ht(psbt, i, amount, script_code, hashtype):
    from nonceverify.psbt import bip143_sighash, parse_tx
    tx = parse_tx(psbt.tx.serialize())
    return bip143_sighash(tx, i, script_code, amount, hashtype)


def _rfc6979_sig(secret: bytes, z32: bytes, hashtype: int) -> bytes:
    """ An honest, valid signature (first RFC 6979 nonce, ground for low R) with this hashtype """
    counter = 0
    sig = _libsecp_sign(secret, z32, 0)
    while crypto.der_decode(sig)[0] >= 1 << 255:
        counter += 1
        sig = _libsecp_sign(secret, z32, counter)
    return sig + bytes([hashtype])


@pytest.mark.parametrize("hashtype", DANGEROUS_SIGHASHES)
def test_sighash_of_our_signature_must_be_all(hashtype):
    """ A valid signature of this seed, honest nonce, but NONE / SINGLE / ANYONECANPAY: red """
    psbt, keys, amounts = p2wpkh_psbt(2)
    libsecp_sign_psbt(psbt, keys, amounts)
    pub = keys[1].key.get_public_key()
    z = _sighash_ht(psbt, 1, amounts[1], p2pkh_script(pub.sec()), hashtype)
    psbt.inputs[1].partial_sigs[pub] = _rfc6979_sig(keys[1].key.secret, z, hashtype)
    report = run_verify(psbt)
    assert report.verdict == VERDICT__FAIL
    assert "instead of ALL" in report.inputs[1].message and report.inputs[0].status == STATUS__OK


def _multisig_psbt(cosigner_hashtype: int, finalize: bool):
    root = embit_root()
    other = embit_root("legal winner thank year wave sausage worth useful legal winner thank yellow", "")
    third = embit_root("letter advice cage absurd amount doctor acoustic avoid letter advice cage above", "")
    path = "m/48h/0h/0h/2h/0/7"
    A, B, C = root.derive(path), other.derive(path), third.derive(path)
    pubs = sorted([k.key.get_public_key() for k in (A, B, C)], key=lambda p: p.sec())
    ws = script.multisig(2, pubs)
    tx = Transaction(vin=[TransactionInput(b"\x22" * 32, 1)],
                     vout=[TransactionOutput(90_000, script.p2wpkh(C.key.get_public_key()))], locktime=870000)
    tx.vin[0].sequence = 0xFFFFFFFD
    psbt = PSBT(tx)
    psbt.inputs[0].witness_utxo = TransactionOutput(100_000, script.p2wsh(ws))
    psbt.inputs[0].witness_script = ws
    psbt.inputs[0].bip32_derivations = {A.key.get_public_key(): DerivationPath(root.my_fingerprint, bip32.parse_path(path))}
    ours = _rfc6979_sig(A.key.secret, _sighash_ht(psbt, 0, 100_000, ws.data, 0x01), 0x01)
    theirs = _rfc6979_sig(B.key.secret, _sighash_ht(psbt, 0, 100_000, ws.data, cosigner_hashtype), cosigner_hashtype)
    sigs = {A.key.get_public_key(): ours, B.key.get_public_key(): theirs}
    if finalize:
        ordered = [sigs[p] for p in pubs if p in sigs]
        psbt.inputs[0].final_scriptwitness = script.Witness([b""] + ordered + [ws.data])
        psbt.inputs[0].partial_sigs = {}
    else:
        psbt.inputs[0].partial_sigs = sigs
    return psbt


@pytest.mark.parametrize("finalize", [False, True])
def test_cosigner_signature_must_be_all_too(finalize):
    """ Before v3 a cosigner's signature was only checked as valid: its sighash byte is checked now """
    report = run_verify(_multisig_psbt(0x01, finalize))
    assert report.verdict == VERDICT__PARTIAL, [(i.status, i.message) for i in report.inputs]
    for hashtype in DANGEROUS_SIGHASHES:
        report = run_verify(_multisig_psbt(hashtype, finalize))
        assert report.verdict == VERDICT__FAIL and "instead of ALL" in report.inputs[0].message, (hex(hashtype), report)


def test_unattributable_signature_must_be_all_too():
    """ An input signed by a key that isn't this seed's: yellow if ALL, red if not ALL """
    other = embit_root("legal winner thank year wave sausage worth useful legal winner thank yellow", "")
    psbt, keys, amounts = p2wpkh_psbt(2)
    libsecp_sign_psbt(psbt, keys[:1], amounts[:1])
    k = other.derive("m/84h/0h/0h/0/3")
    pub = k.key.get_public_key()
    psbt.inputs[1].witness_utxo = TransactionOutput(amounts[1], script.p2wpkh(pub))
    psbt.inputs[1].bip32_derivations = {}
    for hashtype, verdict in ((0x01, VERDICT__INCOMPLETE), (0x02, VERDICT__FAIL), (0x81, VERDICT__FAIL)):
        z = _sighash_ht(psbt, 1, amounts[1], p2pkh_script(pub.sec()), hashtype)
        psbt.inputs[1].partial_sigs = {pub: _rfc6979_sig(k.key.secret, z, hashtype)}
        report = run_verify(psbt)
        assert report.verdict == verdict, (hex(hashtype), [(i.status, i.message) for i in report.inputs])


def test_psbt_sighash_type_field_must_be_all():
    """ PSBT_IN_SIGHASH_TYPE: absent or ALL is fine; any other request is red, signed or not """
    from nonceverify.psbt import ser_varbytes
    psbt, keys, amounts = p2wpkh_psbt(2)
    libsecp_sign_psbt(psbt, keys, amounts)
    raw = psbt.serialize()
    assert verify_psbt(parse_psbt(raw), master(), grinding="libsecp").verdict == VERDICT__OK
    for value, verdict in ((0x01, VERDICT__OK), (0x00, VERDICT__FAIL), (0x02, VERDICT__FAIL),
                           (0x83, VERDICT__FAIL), (0x81, VERDICT__FAIL)):
        field = ser_varbytes(b"\x03") + ser_varbytes(value.to_bytes(4, "little"))
        report = verify_psbt(parse_psbt(_insert_into_input0(raw, field)), master(), grinding="libsecp")
        assert report.verdict == verdict, (hex(value), report.inputs[0].message)
        if verdict == VERDICT__FAIL:
            assert report.inputs[0].message.startswith("the PSBT requests sighash")


def test_taproot_signatures_must_be_all_too():
    """
    Taproot inputs stay yellow (their nonce can't be checked), but a BIP340 signature with
    a sighash other than DEFAULT/ALL is red: in the PSBT fields and in the final witness
    """
    from nonceverify.psbt import ser_varbytes
    psbt, keys, amounts = p2wpkh_psbt(2)
    libsecp_sign_psbt(psbt, keys[:1], amounts[:1])
    xonly = b"\x02" * 32
    psbt.inputs[1].witness_utxo = TransactionOutput(amounts[1], script.Script(b"\x51\x20" + xonly))
    psbt.inputs[1].bip32_derivations = {}
    base = psbt.serialize()

    cases = [
        ("tap key sig, 64 bytes (DEFAULT)", b"\x13", b"\x11" * 64, VERDICT__INCOMPLETE),
        ("tap key sig, 65 bytes, ALL", b"\x13", b"\x11" * 64 + b"\x01", VERDICT__INCOMPLETE),
        ("tap key sig, NONE", b"\x13", b"\x11" * 64 + b"\x02", VERDICT__FAIL),
        ("tap key sig, ALL|ACP", b"\x13", b"\x11" * 64 + b"\x81", VERDICT__FAIL),
        ("tap script sig, SINGLE", b"\x14" + xonly + b"\x33" * 32, b"\x11" * 64 + b"\x03", VERDICT__FAIL),
        ("tap key sig, bad length", b"\x13", b"\x11" * 63, VERDICT__FAIL),
    ]
    from embit.psbt import PSBT as EPSBT
    for name, key, value, verdict in cases:
        p = EPSBT.parse(base)
        p.inputs[1].unknown[key] = value
        report = verify_psbt(parse_psbt(p.serialize()), master(), grinding="libsecp")
        assert report.verdict == verdict, (name, report.inputs[1].message)
        assert report.inputs[0].status == STATUS__OK, name

    # PSBT_IN_SIGHASH_TYPE = DEFAULT (0) on a P2TR coin without BIP371 fields: not red
    from nonceverify.psbt import ser_varbytes
    p = EPSBT.parse(base)
    p.inputs[1].sighash_type = 0
    report = verify_psbt(parse_psbt(p.serialize()), master(), grinding="libsecp")
    assert report.verdict == VERDICT__INCOMPLETE, report.inputs[1].message
    p.inputs[1].sighash_type = 2
    assert verify_psbt(parse_psbt(p.serialize()), master(), grinding="libsecp").verdict == VERDICT__FAIL

    for name, witness, verdict in (("key path, DEFAULT", [b"\x11" * 64], VERDICT__INCOMPLETE),
                                   ("key path, NONE", [b"\x11" * 64 + b"\x02"], VERDICT__FAIL),
                                   ("key path with annex, SINGLE|ACP", [b"\x11" * 64 + b"\x83", b"\x50\x01"], VERDICT__FAIL),
                                   ("script path, sig NONE", [b"\x11" * 64 + b"\x02", b"\x51", b"\xc0" + xonly], VERDICT__FAIL)):
        p = EPSBT.parse(base)
        p.inputs[1].final_scriptwitness = script.Witness(witness)
        report = verify_psbt(parse_psbt(p.serialize()), master(), grinding="libsecp")
        assert report.verdict == verdict, (name, report.inputs[1].message)


def test_unused_partial_signature_next_to_a_final_witness_is_checked():
    """
    v4 review: a finalized 2-of-3 with an extra partial signature (key C) that isn't in
    the final witness. A coordinator could re-finalize with it, so it must be valid and
    ALL like the others: with NONE, or junk, the result is red, not green.
    """
    third = embit_root("letter advice cage absurd amount doctor acoustic avoid letter advice cage above", "")
    C = third.derive("m/48h/0h/0h/2h/0/7")
    pub = C.key.get_public_key()
    psbt = _multisig_psbt(0x01, True)
    ws = psbt.inputs[0].witness_script
    psbt.inputs[0].partial_sigs = {pub: _rfc6979_sig(C.key.secret, _sighash_ht(psbt, 0, 100_000, ws.data, 0x01), 0x01)}
    assert run_verify(psbt).verdict == VERDICT__PARTIAL, "an extra honest ALL signature is fine"
    psbt.inputs[0].partial_sigs = {pub: _rfc6979_sig(C.key.secret, _sighash_ht(psbt, 0, 100_000, ws.data, 0x02), 0x02)}
    report = run_verify(psbt)
    assert report.verdict == VERDICT__FAIL and "instead of ALL" in report.inputs[0].message
    psbt.inputs[0].partial_sigs = {pub: b"\x30\x06\x02\x01\x01\x02\x01\x01\x01"}
    assert run_verify(psbt).verdict == VERDICT__FAIL, "junk next to the final witness"


@pytest.mark.parametrize("hashtype", (0x02, 0x81))
def test_nested_segwit_final_witness_must_be_all(hashtype):
    """ P2SH-P2WPKH, finalized: a non-ALL signature in the final witness is red """
    psbt, keys, amounts = p2wpkh_psbt(1, nested=True)
    pub = keys[0].key.get_public_key()
    z = _sighash_ht(psbt, 0, amounts[0], p2pkh_script(pub.sec()), hashtype)
    sig = _rfc6979_sig(keys[0].key.secret, z, hashtype)
    psbt.inputs[0].final_scriptwitness = script.Witness([sig, pub.sec()])
    psbt.inputs[0].final_scriptsig = script.Script(bytes([22]) + b"\x00\x14" + crypto.hash160(pub.sec()))
    psbt.inputs[0].partial_sigs = {}
    report = verify_psbt(parse_psbt(psbt.serialize()), master(), grinding="libsecp")
    assert report.verdict == VERDICT__FAIL and "instead of ALL" in report.inputs[0].message, report.inputs[0].message
