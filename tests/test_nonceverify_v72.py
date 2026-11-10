"""
v7.2 (8 Nov 2026, device test with the Coldcard Q): when a transaction is fully signed
the Q returns the FINAL transaction (.txn), not the signed PSBT; on microSD it writes only
the .txn. The verifier now pairs that final transaction with the UNSIGNED PSBT of the same
transaction (amounts, scripts, paths), in either order, and checks the signatures that
will actually be broadcast.
"""
import pytest

from nonceverify import app, ui, transport
from nonceverify.psbt import PSBTError, combine_final_tx, has_signatures, parse_psbt
from nonceverify.verify import VERDICT__FAIL, VERDICT__OK, verify_psbt

pytest.importorskip("embit")
from embit import script                                                          # noqa: E402
from test_nonceverify import (bbqr_parts, embit_root, libsecp_sign_psbt, master,   # noqa: E402
    p2wpkh_psbt)
from test_nonceverify_ui import (COLDCARD_Q, FULL, RIGHT, SAME_TXID, FakeHardware, Stop, text_mode_zbar,  # noqa: E402,F401
    load_seed_script, ur_parts)


def finalize(psbt):
    """ What the Coldcard Q does: move the signatures into the final witness / scriptSig """
    for inp in psbt.inputs:
        (pub, sig), = inp.partial_sigs.items()
        inp.final_scriptwitness = script.Witness([sig, pub.sec()])
        if inp.redeem_script is not None:
            inp.final_scriptsig = script.Script(script.Script(inp.redeem_script.data).serialize())
        inp.partial_sigs = {}
    from embit.transaction import Transaction
    tx = Transaction.parse(psbt.tx.serialize())
    for vin, inp in zip(tx.vin, psbt.inputs):
        vin.witness = inp.final_scriptwitness
        if inp.final_scriptsig is not None:
            vin.script_sig = inp.final_scriptsig
    return tx.serialize()


def coldcard_pair(n=2, nested=False, grind=True):
    psbt, keys, amounts = p2wpkh_psbt(n, nested=nested)
    unsigned = psbt.serialize()
    libsecp_sign_psbt(psbt, keys, amounts, grind=grind)
    return unsigned, finalize(psbt)


@pytest.mark.parametrize("nested", [False, True])
def test_final_tx_plus_unsigned_psbt_is_verified(nested):
    from embit.transaction import Transaction
    unsigned, final = coldcard_pair(nested=nested)
    psbt = parse_psbt(unsigned)
    assert not has_signatures(psbt)
    report = verify_psbt(combine_final_tx(psbt, final), master(), grinding="libsecp")
    assert report.verdict == VERDICT__OK, report
    assert report.txid == Transaction.parse(final).txid().hex(), "the txid Sparrow shows, scriptSig included"


def test_skipped_grinding_in_a_final_tx_is_red():
    unsigned, final = coldcard_pair(grind=False)
    report = verify_psbt(combine_final_tx(parse_psbt(unsigned), final), master(), grinding="libsecp")
    # not ground: with libsecp's rule, a high-R first nonce is red (low-R ones pass by luck)
    high_r = [r for r in report.inputs if "retry" in r.message]
    assert (report.verdict == VERDICT__FAIL) == bool(high_r)


def test_final_tx_of_another_transaction_is_refused():
    unsigned, _ = coldcard_pair()
    _, other_final = coldcard_pair(n=3)
    with pytest.raises(PSBTError, match="not the same transaction"):
        combine_final_tx(parse_psbt(unsigned), other_final)


def test_hex_final_tx_is_collected():
    _, final = coldcard_pair()
    c = transport.PSBTCollector()
    c.add(final.hex().encode())
    assert c.result == final


@pytest.mark.parametrize("order", ["final_first", "unsigned_first"])
def test_coldcard_flow_on_the_device(order):
    unsigned, final = coldcard_pair()
    final_qr = [("qr", p) for p in bbqr_parts(final, "T")]
    unsigned_qr = ur_parts(unsigned)
    first, second = (final_qr, unsigned_qr) if order == "final_first" else (unsigned_qr, final_qr)
    script_ = load_seed_script(shots=False) + COLDCARD_Q + [RIGHT, FULL, ("blank",)] + first
    script_ += [("shot", "70_second_scan_prompt"), FULL, ("blank",)] + second
    script_ += [("shot", "71_txid"), SAME_TXID, ("shot", "72_green"), ("stop",)]
    hw = FakeHardware(script_)
    with pytest.raises(Stop):
        app.run(hw)
    assert hw.shots["72_green"].getpixel((240, 34)) == ui.GREEN


def test_second_scan_insists_on_the_other_kind():
    unsigned, final = coldcard_pair()
    final_qr = [("qr", p) for p in bbqr_parts(final, "T")]
    script_ = load_seed_script(shots=False) + COLDCARD_Q + [RIGHT, FULL, ("blank",)] + final_qr
    script_ += [FULL, ("blank",)] + final_qr + [("shot", "73_still_scanning")] + ur_parts(unsigned)
    script_ += [SAME_TXID, ("shot", "74_green"), ("stop",)]
    hw = FakeHardware(script_)
    with pytest.raises(Stop):
        app.run(hw)
    assert hw.shots["74_green"].getpixel((240, 34)) == ui.GREEN
