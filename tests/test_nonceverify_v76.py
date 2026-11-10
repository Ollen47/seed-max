"""
v7.6 (Ollen, 10 Nov 2026): "in a transaction with change, check the change address and
confirm it is mine". Single-sig: the key is derived here from the seed. Multisig: the
address must be the confirmed descriptor's. Declared as ours but not ours: red.
"""
import pytest

from nonceverify import address as A, app, ui
from nonceverify.psbt import parse_psbt
from nonceverify.verify import (OWNER__EXTERNAL, OWNER__FAKE, OWNER__OURS, OWNER__UNVERIFIED, VERDICT__FAIL,
    VERDICT__OK, verify_psbt)

pytest.importorskip("embit")
from embit import bip32, script                                                     # noqa: E402
from embit.psbt import DerivationPath                                               # noqa: E402
from embit.transaction import TransactionOutput                                     # noqa: E402
from test_nonceverify import embit_root, libsecp_sign_psbt, master, p2wpkh_psbt     # noqa: E402
from test_nonceverify_address import multisig_descriptor                            # noqa: E402
from test_nonceverify_ui import (RIGHT, SAME_TXID, SEEDSIGNER, FakeHardware, Stop,  # noqa: E402
    load_seed_script, text_mode_zbar, ur_parts)                                     # noqa: F401

ATTACKER = "letter advice cage absurd amount doctor acoustic avoid letter advice cage above"


def with_output_script(psbt, index, spk):
    """ embit rebuilds psbt.tx from its scopes: a new PSBT with output `index` paying `spk` """
    from embit.psbt import PSBT
    from embit.transaction import Transaction
    tx = Transaction.parse(psbt.tx.serialize())
    tx.vout[index].script_pubkey = spk
    new = PSBT(tx)
    for a, b in zip(psbt.inputs, new.inputs):
        b.witness_utxo, b.redeem_script, b.bip32_derivations = a.witness_utxo, a.redeem_script, dict(a.bip32_derivations)
    for a, b in zip(psbt.outputs, new.outputs):
        b.bip32_derivations = dict(a.bip32_derivations)
    return new


def signed(psbt, keys, amounts):
    libsecp_sign_psbt(psbt, keys, amounts)
    return verify_psbt(parse_psbt(psbt.serialize()), master(), grinding="libsecp")


def test_singlesig_change_is_proven_yours():
    psbt, keys, amounts = p2wpkh_psbt(2)
    report = signed(psbt, keys, amounts)
    assert report.verdict == VERDICT__OK
    pay, change = report.outputs
    assert pay.owner == OWNER__EXTERNAL and change.owner == OWNER__OURS
    assert change.label == "change #0" and change.path == "m/84'/0'/0'/1/0" and not change.note
    screen = app.ResultScreen(report, "coldcard_q", txid_confirmed=True)
    texts = [line[0] for line in screen.list.lines]
    assert any(t.startswith("Change") and "YOURS, change #0" in t for t in texts)
    assert screen.banner()[0] == ui.GREEN


def test_change_declared_ours_but_paying_someone_else_is_red():
    """ A malicious Sparrow shows the attacker's output as "change": our path, our fingerprint """
    psbt, keys, amounts = p2wpkh_psbt(2)
    thief = embit_root(ATTACKER, "").derive("m/84h/0h/0h/1/0").key.get_public_key()
    psbt = with_output_script(psbt, 1, script.p2wpkh(thief))
    psbt.outputs[1].bip32_derivations = {thief: DerivationPath(embit_root().my_fingerprint,
                                                               bip32.parse_path("m/84h/0h/0h/1/0"))}
    report = signed(psbt, keys, amounts)
    assert report.outputs[1].owner == OWNER__FAKE
    assert report.verdict == VERDICT__FAIL and "NOT YOUR CHANGE" in report.tx_problem
    screen = app.ResultScreen(report, "coldcard_q")
    assert screen.banner()[0] == ui.RED
    assert "NOT YOUR CHANGE" in screen.list.lines[0][0], "the reason first"


def test_change_with_our_key_declared_but_another_script_is_not_ours():
    psbt, keys, amounts = p2wpkh_psbt(1)
    ours = embit_root().derive("m/84h/0h/0h/1/0").key.get_public_key()
    thief = embit_root(ATTACKER, "").derive("m/84h/0h/0h/1/0").key.get_public_key()
    psbt = with_output_script(psbt, 1, script.p2wpkh(thief))   # bip32 still says our key
    report = signed(psbt, keys, amounts)
    assert report.outputs[1].owner == OWNER__FAKE and report.verdict == VERDICT__FAIL
    assert ours in psbt.outputs[1].bip32_derivations


def test_unusual_change_is_yours_with_a_warning():
    psbt, keys, amounts = p2wpkh_psbt(1)
    root = embit_root()
    for path, kind, expect in (("m/84h/0h/5h/1/3", script.p2wpkh, "another account"),
                               ("m/84h/0h/0h/1/5000", script.p2wpkh, "may not find it"),
                               ("m/84h/0h/0h/1/2", lambda p: script.p2sh(script.p2wpkh(p)), "script type")):
        pub = root.derive(path).key.get_public_key()
        psbt = with_output_script(psbt, 1, kind(pub))
        psbt.outputs[1].bip32_derivations = {pub: DerivationPath(root.my_fingerprint, bip32.parse_path(path))}
        for i in psbt.inputs:
            i.partial_sigs = {}
        report = signed(psbt, keys, amounts)
        assert report.outputs[1].owner == OWNER__OURS, path
        assert expect in report.outputs[1].note, (path, report.outputs[1].note)


def _with_multisig_change(desc_text, roots, index=5, attacker=False):
    psbt, keys, amounts = p2wpkh_psbt(1, change=False)
    desc = A.parse_descriptor(desc_text)
    path = f"m/48h/0h/0h/2h/1/{index}"
    pubs = [r.derive(path).key.get_public_key() for r in roots]
    threshold = 2
    if attacker:                         # our key + two attacker keys, threshold 1
        evil = embit_root(ATTACKER, "")
        pubs = [pubs[0], evil.derive("m/1/1").key.get_public_key(), evil.derive("m/1/2").key.get_public_key()]
        threshold = 1
    ws = script.multisig(threshold, sorted(pubs, key=lambda p: p.sec()))
    from embit.psbt import OutputScope
    psbt.outputs.append(OutputScope(vout=TransactionOutput(50_000, script.p2wsh(ws))))
    psbt.outputs[-1].witness_script = ws
    for r, p in zip(roots, pubs):
        psbt.outputs[-1].bip32_derivations[p] = DerivationPath(r.my_fingerprint, bip32.parse_path(path))
    return psbt, keys, amounts, desc


def test_multisig_change_on_a_singlesig_wallet_is_red():
    """ Review of v7.6 (F1): our change key in a 1-of-2 with the attacker's, single-sig coins """
    psbt, keys, amounts = p2wpkh_psbt(1, change=False)
    root, evil = embit_root(), embit_root(ATTACKER, "")
    ours = root.derive("m/84h/0h/0h/1/0").key.get_public_key()
    thief = evil.derive("m/84h/0h/0h/1/0").key.get_public_key()
    ws = script.multisig(1, sorted([ours, thief], key=lambda p: p.sec()))
    from embit.psbt import OutputScope
    psbt.outputs.append(OutputScope(vout=TransactionOutput(80_000, script.p2wsh(ws))))
    psbt.outputs[-1].witness_script = ws
    psbt.outputs[-1].bip32_derivations[ours] = DerivationPath(root.my_fingerprint, bip32.parse_path("m/84h/0h/0h/1/0"))
    report = signed(psbt, keys, amounts)
    assert report.outputs[-1].owner == OWNER__FAKE and report.verdict == VERDICT__FAIL


SEED_B = "legal winner thank year wave sausage worth useful legal winner thank yellow"
SEED_C = "letter advice cage absurd amount doctor acoustic avoid letter advice cage above"


def _multisig_spend(change_keys="ours", threshold=2, xpubs=True, index=5):
    """
    A 2-of-3 P2WSH input (our seed + B + C, Sparrow-like: global xpubs, bip32 for every key),
    signed by our seed, with a multisig change at /1/index. change_keys: "ours" (same
    quorum) or "attacker" (our key + two attacker keys).
    """
    from embit.psbt import PSBT, OutputScope
    from embit.transaction import Transaction, TransactionInput
    from embit.ec import PrivateKey
    roots = [embit_root(), embit_root(SEED_B, ""), embit_root(SEED_C, "")]
    acct = "m/48h/0h/0h/2h"
    accounts = [r.derive(acct) for r in roots]

    def ws_at(chain, i, keys=None, m=2):
        pubs = keys or [a.derive([chain, i]).key.get_public_key() for a in accounts]
        return script.multisig(m, sorted(pubs, key=lambda p: p.sec())), pubs
    in_ws, in_pubs = ws_at(0, 3)
    if change_keys == "ours":
        out_ws, out_pubs = ws_at(1, index)
    else:
        mine = accounts[0].derive([1, index]).key.get_public_key()
        bad = [PrivateKey(bytes([9]) * 32).get_public_key(), PrivateKey(bytes([10]) * 32).get_public_key()]
        out_ws, out_pubs = ws_at(1, index, [mine] + bad, m=threshold if threshold != 2 else 1)
    tx = Transaction(vin=[TransactionInput(b"\x44" * 32, 0)],
                     vout=[TransactionOutput(30_000, script.p2wpkh(roots[1].derive("m/84h/0h/0h/0/9").key.get_public_key())),
                           TransactionOutput(69_000, script.p2wsh(out_ws))], locktime=870000)
    tx.vin[0].sequence = 0xFFFFFFFD
    psbt = PSBT(tx)
    psbt.inputs[0].witness_utxo = TransactionOutput(100_000, script.p2wsh(in_ws))
    psbt.inputs[0].witness_script = in_ws
    for r, p in zip(roots, in_pubs):
        psbt.inputs[0].bip32_derivations[p] = DerivationPath(r.my_fingerprint, bip32.parse_path(acct + "/0/3"))
    psbt.outputs[1].witness_script = out_ws
    path_out = bip32.parse_path(acct + f"/1/{index}")
    psbt.outputs[1].bip32_derivations[out_pubs[0]] = DerivationPath(roots[0].my_fingerprint, path_out)
    if change_keys == "ours":
        for r, p in zip(roots[1:], out_pubs[1:]):
            psbt.outputs[1].bip32_derivations[p] = DerivationPath(r.my_fingerprint, path_out)
    if xpubs:
        for r, a in zip(roots, accounts):
            psbt.xpubs[a.to_public()] = DerivationPath(r.my_fingerprint, bip32.parse_path(acct))
    psbt.sign_with(roots[0])
    return psbt


def test_multisig_change_same_quorum_as_the_coins_is_yours_without_descriptor():
    report = verify_psbt(parse_psbt(_multisig_spend().serialize()), master(), grinding="embit")
    out = report.outputs[1]
    assert out.owner == OWNER__OURS and out.label == "change #5", (out.owner, out.note)


def test_multisig_change_with_attacker_keys_is_red_without_descriptor():
    report = verify_psbt(parse_psbt(_multisig_spend("attacker").serialize()), master(), grinding="embit")
    assert report.outputs[1].owner == OWNER__FAKE and report.verdict == VERDICT__FAIL


def test_multisig_change_without_xpubs_needs_the_descriptor():
    raw = _multisig_spend(xpubs=False).serialize()
    report = verify_psbt(parse_psbt(raw), master(), grinding="embit")
    assert report.outputs[1].owner == OWNER__UNVERIFIED and report.change_unverified
    screen = app.ResultScreen(report, "seedsigner", txid_confirmed=True)
    assert not screen.can_export
    text, _roots = multisig_descriptor()
    desc = A.parse_descriptor(text)
    with_desc = verify_psbt(parse_psbt(raw), master(), grinding="embit", descriptor=desc)
    assert with_desc.outputs[1].owner == OWNER__OURS, with_desc.outputs[1].note


def test_attacker_multisig_change_without_xpubs_is_never_yours():
    raw = _multisig_spend("attacker", xpubs=False).serialize()
    text, _roots = multisig_descriptor()
    report = verify_psbt(parse_psbt(raw), master(), grinding="embit", descriptor=A.parse_descriptor(text))
    assert report.outputs[1].owner in (OWNER__UNVERIFIED, OWNER__FAKE)
    assert not app.ResultScreen(report, "seedsigner", txid_confirmed=True).can_export


def test_hidden_change_paths_are_flagged_even_with_stray_input_bip32():
    """ Review of v7.6 (F2/F3/F4): warnings from verified keys and absolute rules, yellow banner """
    root = embit_root()
    for path in ("m/84h/0h/777h/1/5", "m/84h/1h/0h/1/0", "m/48h/0h/0h/2h/1/0", "m/1/5", "m/84h/0h/0h/1/2000000"):
        psbt, keys, amounts = p2wpkh_psbt(1)
        pub = root.derive(path).key.get_public_key()
        psbt = with_output_script(psbt, 1, script.p2wpkh(pub))
        psbt.outputs[1].bip32_derivations = {pub: DerivationPath(root.my_fingerprint, bip32.parse_path(path))}
        stray = root.derive("m/84h/0h/777h/0/0").key.get_public_key()
        psbt.inputs[0].bip32_derivations[stray] = DerivationPath(root.my_fingerprint, bip32.parse_path("m/84h/0h/777h/0/0"))
        report = signed(psbt, keys, amounts)
        out = report.outputs[1]
        assert out.owner == OWNER__OURS and out.note, (path, out.note)
        assert report.change_warning
        banner = app.ResultScreen(report, "coldcard_q", txid_confirmed=True).banner()
        assert banner[0] == ui.YELLOW, path


def test_our_key_declared_with_another_fingerprint_is_red():
    """ Review of v7.6 (F6) """
    psbt, keys, amounts = p2wpkh_psbt(1)
    root = embit_root()
    ours = root.derive("m/84h/0h/0h/1/0").key.get_public_key()
    thief = embit_root(ATTACKER, "").derive("m/84h/0h/0h/1/0").key.get_public_key()
    psbt = with_output_script(psbt, 1, script.p2wpkh(thief))
    psbt.outputs[1].bip32_derivations = {ours: DerivationPath(b"\x01\x02\x03\x04", bip32.parse_path("m/84h/0h/0h/1/0"))}
    report = signed(psbt, keys, amounts)
    assert report.outputs[1].owner == OWNER__FAKE


def test_change_check_on_the_device():
    psbt, keys, amounts = p2wpkh_psbt(2)
    psbt.sign_with(embit_root())
    script_ = load_seed_script(shots=False) + SEEDSIGNER + [RIGHT, ("blank",)] + ur_parts(psbt.serialize())
    script_ += [SAME_TXID, ("shot", "110_change_yours"), ("stop",)]
    hw = FakeHardware(script_)
    with pytest.raises(Stop):
        app.run(hw)
    assert hw.shots["110_change_yours"].getpixel((240, 34)) == ui.GREEN
