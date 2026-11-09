"""
v7.3: address explorer. Single-sig (BIP84/49/44, any account) and the multisig
descriptor of the session, receive and change, pages of six, "Go to #", and a detail
screen whose QR reads back as exactly the address shown.
"""
import pytest

from nonceverify import address as A, app, ui
from nonceverify.hw import Event

pytest.importorskip("embit")
pytest.importorskip("qrcode")
from embit import script                                                          # noqa: E402
from embit.descriptor import Descriptor as EDescriptor                            # noqa: E402
from test_nonceverify_address import eaddr, eroot, master, multisig_descriptor    # noqa: E402
from test_nonceverify_ui import (FULL, LEFT, MIDDLE, RIGHT, FakeHardware, Stop,   # noqa: E402
    load_seed_script, text_mode_zbar, type_text)                                  # noqa: F401

def decode_qr(image):
    """ Text-mode zbar (the device's binary mode isn't in upstream pyzbar) """
    from pyzbar import pyzbar
    from pyzbar.pyzbar import ZBarSymbol
    found = pyzbar.decode(image.convert("L"), symbols=[ZBarSymbol.QRCODE])
    return found[0].data if found else None


EXPLORER = ("tap", 397, 194)            # AddressModeScreen, third button
NATIVE = ("tap", 122, 104)              # ExplorerWalletScreen
NESTED = ("tap", 357, 104)
LEGACY = ("tap", 122, 170)
MULTISIG = ("tap", 357, 170)
TOGGLE = ("tap", 80, 57)
ACCT_PLUS = ("tap", 312, 57)
GOTO = ("tap", 410, 57)
PREV = ("tap", 240, 288)


def row(i):
    return ("tap", 240, app.ExplorerListScreen.ROW_TOP + i * app.ExplorerListScreen.ROW_H + 12)


@pytest.mark.parametrize("purpose,maker", [(84, script.p2wpkh), (49, lambda p: script.p2sh(script.p2wpkh(p))),
                                           (44, script.p2pkh)])
def test_singlesig_explorer_matches_embit(purpose, maker):
    root, m = eroot(), master()
    for account in (0, 2):
        e = A.SinglesigExplorer(m, purpose, account)
        for change in (False, True):
            for index in (0, 1, 57):
                text, path = e.address(change, index)
                want = eaddr(maker(root.derive(f"m/{purpose}h/0h/{account}h/{int(change)}/{index}").key.get_public_key()))
                assert text == want and path[-2:] == [int(change), index]


def test_descriptor_explorer_matches_embit():
    text, _ = multisig_descriptor()
    e = A.DescriptorExplorer(A.parse_descriptor(text))
    ed = EDescriptor.from_string(text.split("#")[0])
    for change in (False, True):
        for index in (0, 9):
            assert e.address(change, index)[0] == eaddr(ed.derive(index, branch_index=int(change)).script_pubkey())


def test_address_qr_reads_back_as_the_address(monkeypatch):
    monkeypatch.setattr(app, "decode_qr", decode_qr)
    for text in (eaddr(script.p2wpkh(eroot().derive("m/84h/0h/0h/0/0").key.get_public_key())),
                 eaddr(script.p2pkh(eroot().derive("m/44h/0h/0h/0/0").key.get_public_key()))):
        image = app.address_qr(text, 222)
        assert image is not None and image.size[0] <= 222
        assert decode_qr(image).decode().lower() == text.lower()


def test_address_qr_refuses_when_the_check_fails(monkeypatch):
    monkeypatch.setattr(app, "decode_qr", lambda image: b"bc1qsomethingelse")
    assert app.address_qr("bc1qxyz", 222) is None


def test_explorer_flow_single_sig():
    root = eroot()
    first = eaddr(script.p2wpkh(root.derive("m/84h/0h/0h/0/0").key.get_public_key()))
    script_ = load_seed_script(shots=False) + [MIDDLE, EXPLORER, ("shot", "80_wallets"), NATIVE, ("shot", "81_list")]
    script_ += [row(0), ("shot", "82_detail"), FULL, RIGHT, ("shot", "83_page2"), TOGGLE, ("shot", "84_change"),
                ACCT_PLUS, ("shot", "85_account1"), GOTO] + type_text("100") + [("tap", 425, 280), ("shot", "86_goto"),
                row(2), ("shot", "87_detail_102"), ("stop",)]
    hw = FakeHardware(script_)
    seen = []
    real = app.AddressDetailScreen

    class Spy(real):
        def __init__(self, text, where, path, label):
            seen.append((text, where, path, label))
            super().__init__(text, where, path, label)
    app_mod_detail = app.AddressDetailScreen
    app.AddressDetailScreen = Spy
    try:
        with pytest.raises(Stop):
            app.run(hw)
    finally:
        app.AddressDetailScreen = app_mod_detail
    assert seen[0][:3] == (first, "receive #0", "m/84'/0'/0'/0/0")
    want = eaddr(script.p2wpkh(root.derive("m/84h/0h/1h/1/102").key.get_public_key()))
    assert seen[1][:3] == (want, "change #102", "m/84'/0'/1'/1/102"), "account 1, change, from #100"
    detail = hw.shots["82_detail"]
    qr = detail.crop((ui.PAD, ui.TOP + 2, ui.PAD + 222, ui.TOP + 224))
    assert decode_qr(qr).decode().lower() == first, "the QR on screen is the address"


def test_explorer_flow_multisig_uses_the_session_descriptor():
    text, _ = multisig_descriptor()
    ed = EDescriptor.from_string(text.split("#")[0])
    script_ = load_seed_script(shots=False) + [MIDDLE, EXPLORER, MULTISIG, ("blank",), ("qr", text), RIGHT]
    script_ += [("shot", "88_multisig_list"), TOGGLE, row(3), ("shot", "89_multisig_detail"), ("stop",)]
    hw = FakeHardware(script_)
    session = app.Session()
    with pytest.raises(Stop):
        app.run(hw, session)
    shot = hw.shots["89_multisig_detail"]
    qr = shot.crop((ui.PAD, ui.TOP + 2, ui.PAD + 222, ui.TOP + 224))
    assert decode_qr(qr).decode().lower() == eaddr(ed.derive(3, branch_index=1).script_pubkey())
    assert session.descriptor is not None


def test_list_screen_events():
    e = A.SinglesigExplorer(master(), 84, 0)
    s = app.ExplorerListScreen(e, False, 0, True)
    assert s.on_event(Event(Event.SWIPE_UP, 240, 200)) == "next"
    assert s.on_event(Event(Event.TAP, *row(5)[1:])) == ("row", 5)
    assert len(s.rows) == 6 and s.rows[5][1][-1] == 5
