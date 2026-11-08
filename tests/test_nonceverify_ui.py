"""
The verifier's screens and flow, driven end to end on a fake device: scripted taps,
camera frames that are real QR images (decoded by zbar as on the device), screenshots.

    SEEDSIGNER_SCREENSHOT_DIR=/tmp/shots pytest tests/test_nonceverify_ui.py
"""
import hashlib
import os
from collections import deque

import pytest

from nonceverify import app, ui
from nonceverify.hw import HEIGHT, WIDTH, Event

pytest.importorskip("embit")
qrcode = pytest.importorskip("qrcode")
pytest.importorskip("pyzbar.pyzbar")

from test_nonceverify import (MNEMONIC, PASSPHRASE, bbqr_parts, embit_root, p2wpkh_psbt,   # noqa: E402
    sighash_of, sign_with_k)

SHOTS = os.environ.get("SEEDSIGNER_SCREENSHOT_DIR")


@pytest.fixture(autouse=True)
def text_mode_zbar(monkeypatch):
    """
    The device's pyzbar fork has binary mode (required by hw.decode_qr). Upstream pyzbar
    doesn't: the tests read QR codes in text mode, which is exact for the ASCII and
    low-byte payloads used here.
    """
    import inspect
    from pyzbar import pyzbar
    if "binary" in inspect.signature(pyzbar.decode).parameters:
        return

    def decode(image):
        from pyzbar.pyzbar import ZBarSymbol
        found = pyzbar.decode(image.convert("L"), symbols=[ZBarSymbol.QRCODE])
        return found[0].data if found else None
    monkeypatch.setattr(app, "decode_qr", decode)


class Stop(Exception):
    pass


class FakeHardware:
    """ Plays a script: ("tap", x, y), ("swipe_up",), ("qr", payload), ("blank",), ("shot", name), ("stop",) """
    def __init__(self, script):
        self.script = deque(script)
        self.shown = []
        self.zoom = 1
        self.camera_on = False
        self.idle = 0
        self.shots = {}

    def _next(self, kinds):
        while self.script and self.script[0][0] == "shot":
            name = self.script.popleft()[1]
            self.shots[name] = self.shown[-1]
            if SHOTS:
                os.makedirs(SHOTS, exist_ok=True)
                self.shown[-1].save(os.path.join(SHOTS, f"{name}.png"))
        if self.script and self.script[0][0] == "stop":
            raise Stop()
        if self.script and self.script[0][0] in kinds:
            self.idle = 0
            return self.script.popleft()
        self.idle += 1
        if self.idle > 2000:
            raise AssertionError(f"script stalled at {list(self.script)[:2]}")
        return None

    def show(self, image):
        assert image.size == (WIDTH, HEIGHT)
        self.shown.append(image.copy())

    def poll(self):
        step = self._next(("tap", "swipe_up", "swipe_down"))
        if step is None:
            return []
        if step[0] == "tap":
            return [Event(Event.TAP, step[1], step[2])]
        return [Event(Event.SWIPE_UP if step[0] == "swipe_up" else Event.SWIPE_DOWN, 240, 160)]

    def wait(self, seconds=0):
        pass

    def camera_start(self):
        self.camera_on = True

    def camera_stop(self):
        self.camera_on = False
        # frames scripted beyond what the decoder needed: the camera is off, they're gone
        while self.script and self.script[0][0] in ("qr", "blank"):
            self.script.popleft()

    def camera_set_zoom(self, factor):
        self.zoom = factor

    def camera_frame(self):
        assert self.camera_on
        step = self._next(("qr", "blank"))
        if step is None:
            return None
        from PIL import Image
        if step[0] == "blank":
            return Image.new("RGB", (320, 320), (90, 90, 90))
        qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(step[1])
        qr.make(fit=True)
        return qr.make_image().convert("RGB").resize((320, 320))


# Bottom button rows (ui.button_row)
LEFT = ("tap", 122, 288)
RIGHT = ("tap", 357, 288)
FULL = ("tap", 240, 288)
MIDDLE = FULL                               # middle of three bottom buttons ("Address" on the seed screen)
SAME_TXID = RIGHT                           # v7: the txid step before every green
SIGNER_BUTTON = ("tap", 240, 216)          # SeedReadyScreen: GRINDING_Y 196 .. 236
# Taps on the signer button after a seed load (it starts empty): 1 Coldcard Q, 2 SeedSigner, 3 Trezor
COLDCARD_Q, SEEDSIGNER, TREZOR = [SIGNER_BUTTON], [SIGNER_BUTTON] * 2, [SIGNER_BUTTON] * 3


def hint_shown(shot) -> bool:
    """ The yellow hint line above the signer button (SeedReadyScreen.GRINDING_Y - 8) """
    return any(shot.getpixel((x, y)) == ui.YELLOW for x in range(ui.PAD, 300) for y in range(176, 190))


def type_text(text):
    """ Taps on the passphrase keyboard, switching pages as a person would """
    taps = []
    kb = ui.KeyboardScreen()

    def tap_value(value):
        for b in kb.keys():
            if b.value == value:
                x = int((b.rect[0] + b.rect[2]) / 2)
                y = int((b.rect[1] + b.rect[3]) / 2)
                taps.append(("tap", x, y))
                kb.on_event(Event(Event.TAP, x, y))
                return True
        return False

    for ch in text:
        for _ in range(4):
            if tap_value(("char", ch)):
                break
            page = next(p for p, rows in ui.KeyboardScreen.PAGES.items() if any(ch in r for r in rows))
            target = {"abc": "abc", "ABC": "ABC", "123": "123", "#+=": "#+="}[page]
            if not tap_value(("page", target)):
                tap_value(("page", "123" if target == "#+=" else "abc"))
        else:
            raise AssertionError(f"can't type {ch!r}")
    assert kb.text == text
    return taps


def ur_parts(psbt_bytes, fragment=120, count=None):
    from seedsigner.helpers.ur2.ur import UR
    from seedsigner.helpers.ur2.ur_encoder import UREncoder
    from urtypes.crypto import PSBT as UR_PSBT
    enc = UREncoder(UR("crypto-psbt", UR_PSBT(psbt_bytes).to_cbor()), max_fragment_len=fragment)
    n = count or enc.fountain_encoder.seq_len() + 3
    return [("qr", enc.next_part().upper()) for _ in range(n)]


def compact_seedqr(mnemonic):
    from embit import bip39
    return bip39.mnemonic_to_bytes(mnemonic)


def load_seed_script(passphrase=PASSPHRASE, shots=True):
    script = [("shot", "00_splash"), ("tap", 240, 160), ("shot", "01_home")] if shots else [("tap", 240, 160)]
    script += [LEFT, ("blank",), ("qr", compact_seedqr(MNEMONIC))]           # home: "SeedQR"
    if shots:
        script += [("shot", "02_seed_read")]
    script += [RIGHT]
    if shots:
        script += [("shot", "03_keyboard_empty")]
    script += type_text(passphrase)
    if shots:
        script += [("shot", "04_keyboard_typed")]
    script += [("tap", 425, 280)]                     # "Fatto"
    if shots:
        script += [("shot", "05_confirm_passphrase")]
    script += [RIGHT]
    if shots:
        script += [("shot", "06_seed_ready")]
    return script


def test_full_flow_ok():
    psbt, keys, amounts = p2wpkh_psbt(3)
    psbt.sign_with(embit_root())
    script = load_seed_script() + SEEDSIGNER
    script += [RIGHT, ("blank",)] + ur_parts(psbt.serialize())
    script += [SAME_TXID, ("shot", "07_result_ok"), ("swipe_up",), ("swipe_up",), ("swipe_up",), ("shot", "08_result_ok_scrolled")]
    script += [LEFT, ("shot", "09_seed_wiped"), FULL, ("shot", "10_home_again"), ("stop",)]
    hw = FakeHardware(script)
    with pytest.raises(Stop):
        app.run(hw)
    expected_fp = embit_root().my_fingerprint.hex()
    ready = hw.shots["06_seed_ready"]
    assert ready.getpixel((240, 82)) is not None
    banner = hw.shots["07_result_ok"].getpixel((240, 34))
    assert banner == ui.GREEN, banner
    assert expected_fp


def test_keyboard_types_every_printable_ascii_character():
    import string
    text = "".join(c for c in string.printable if c not in "\t\n\r\x0b\x0c")
    kb = ui.KeyboardScreen()
    for tap in type_text(text):
        kb.on_event(Event(Event.TAP, tap[1], tap[2]))
    assert kb.text == text


def test_flow_detects_a_chosen_nonce_from_a_coldcard_bbqr():
    """ The COLDCARD Q shows the signed PSBT as BBQr: scanned directly, chosen nonce caught """
    from nonceverify.psbt import p2pkh_script
    import nonceverify.crypto as crypto
    psbt, keys, amounts = p2wpkh_psbt(2)
    psbt.sign_with(embit_root())
    k = keys[0]
    z32 = sighash_of(psbt, 0, amounts[0], p2pkh_script(k.key.get_public_key().sec()))
    evil = int.from_bytes(hashlib.sha256(b"leak" + z32).digest(), "big") % crypto.N
    psbt.inputs[0].partial_sigs[k.key.get_public_key()] = sign_with_k(int.from_bytes(k.key.secret, "big"), z32, evil)
    script = load_seed_script(shots=False) + SEEDSIGNER
    script += [RIGHT] + [("qr", p) for p in bbqr_parts(psbt.serialize(), "P", "Z", chunk=200)]
    script += [("shot", "11_result_fail"), ("swipe_up",), ("shot", "12_result_fail_scrolled"), RIGHT, ("blank",), ("tap", 40, 240),
               ("shot", "13_back_to_ready"), ("stop",)]
    hw = FakeHardware(script)
    with pytest.raises(Stop):
        app.run(hw)
    assert hw.shots["11_result_fail"].getpixel((240, 34)) == ui.RED


def test_wrong_passphrase_and_raw_transaction_qr():
    psbt, keys, amounts = p2wpkh_psbt(1)
    psbt.sign_with(embit_root())
    script = load_seed_script(passphrase="sbagliata", shots=False) + SEEDSIGNER
    script += [RIGHT] + [("qr", p) for p in bbqr_parts(b"\x02\x00\x00\x00" + os.urandom(60), "T")]
    script += [("shot", "14_raw_tx_qr"), ("tap", 40, 240)]
    script += [RIGHT] + ur_parts(psbt.serialize())
    script += [("shot", "15_result_wrong_seed"), ("stop",)]
    hw = FakeHardware(script)
    with pytest.raises(Stop):
        app.run(hw)
    assert hw.shots["15_result_wrong_seed"].getpixel((240, 34)) == ui.RED


# ---------------------------------------------------------------------------------------
# v3: the signer is chosen by name (Coldcard Q, SeedSigner, Trezor)
# ---------------------------------------------------------------------------------------
def test_signer_button_cycles_through_the_device_names():
    from nonceverify.verify import GRIND__EMBIT, GRIND__LIBSECP, GRIND__NONE
    screen = app.SeedReadyScreen("73c5da0a", 24, True)
    assert screen.signer is None and screen.buttons[0].label == "Tap to choose", "no default signer"
    assert screen.on_event(Event(Event.TAP, *SIGNER_BUTTON[1:])) == "__redraw__"
    seen = []
    for _ in range(4):
        seen.append((screen.signer, screen.buttons[0].label if hasattr(screen.buttons[0], "label") else None,
                     app.signer_grinding(screen.signer)))
        assert screen.on_event(Event(Event.TAP, *SIGNER_BUTTON[1:])) == "__redraw__"
    assert [s[0] for s in seen] == ["coldcard_q", "seedsigner", "trezor", "coldcard_q"]
    assert [s[2] for s in seen] == [GRIND__LIBSECP, GRIND__EMBIT, GRIND__NONE, GRIND__LIBSECP]
    assert [app.signer_name(s[0]) for s in seen] == ["Coldcard Q", "SeedSigner", "Trezor", "Coldcard Q"]
    assert app.SeedReadyScreen("73c5da0a", 12, False, "bogus").signer is None, "unknown -> must choose"


def _trezor_like_psbt(n_inputs=8):
    """ Signed with the first RFC 6979 nonce, no grinding: at least one r is high """
    from embit.psbt import PSBT as EPSBT
    import nonceverify.crypto as crypto
    from test_nonceverify import libsecp_sign_psbt
    psbt, keys, amounts = p2wpkh_psbt(n_inputs)
    psbt = libsecp_sign_psbt(EPSBT.parse(psbt.serialize()), keys, amounts, grind=False)
    rs = [crypto.der_decode(sig[:-1])[0] for inp in psbt.inputs for sig in inp.partial_sigs.values()]
    assert any(r >= (1 << 255) for r in rs), "fixture: some first nonce must have a high r"
    return psbt


def _run_trezor_flow(taps_on_signer, monkeypatch, expect_green=False):
    shown = []
    real = app.ResultScreen

    class Spy(real):
        def __init__(self, report, signer=None, **kwargs):
            shown.append(signer)
            super().__init__(report, signer, **kwargs)
    monkeypatch.setattr(app, "ResultScreen", Spy)
    psbt = _trezor_like_psbt()
    script = load_seed_script(shots=False) + [SIGNER_BUTTON] * taps_on_signer
    script += [("shot", f"16_signer_{taps_on_signer}")]
    script += [RIGHT, ("blank",)] + ur_parts(psbt.serialize())
    script += ([SAME_TXID] if expect_green else []) + [("shot", "17_result"), ("stop",)]
    hw = FakeHardware(script)
    with pytest.raises(Stop):
        app.run(hw)
    hw.result_signers = shown
    return hw


def test_trezor_signature_is_green_with_trezor_and_red_with_coldcard(monkeypatch):
    """
    The same no-grinding signatures: "Trezor" (first nonce, no retry) verifies them; the
    default "Coldcard Q" (libsecp, low R) must reject them, or a signer could pick either.
    """
    hw = _run_trezor_flow(3, monkeypatch, True)  # (none) -> Coldcard Q -> SeedSigner -> Trezor
    assert hw.shots["17_result"].getpixel((240, 34)) == ui.GREEN
    assert hw.result_signers == ["trezor"], "the result names the signer chosen"
    hw = _run_trezor_flow(1, monkeypatch)        # Coldcard Q
    assert hw.shots["17_result"].getpixel((240, 34)) == ui.RED
    assert hw.result_signers == ["coldcard_q"]


def test_result_names_the_signer():
    from nonceverify.verify import GRIND__NONE, Report
    import inspect
    params = inspect.signature(Report).parameters
    report = Report(**{k: v for k, v in dict(fingerprint="73c5da0a", txid="ab" * 32, grinding=GRIND__NONE,
                                             locktime=0, version=2).items() if k in params})
    screen = app.ResultScreen(report, "trezor")
    texts = [line[0] for line in screen.list.lines]
    assert any("Trezor (none)" in t for t in texts), texts


def test_scan_needs_a_signer_after_every_seed_load():
    """ No default: "Scansiona PSBT" does nothing until a device is named, after every seed load """
    psbt, keys, amounts = p2wpkh_psbt(1)
    psbt.sign_with(embit_root())
    script = load_seed_script(shots=False)
    script += [RIGHT, ("shot", "18_must_choose")]                 # nothing chosen: stays here
    script += SEEDSIGNER + [RIGHT, ("blank",)] + ur_parts(psbt.serialize()) + [SAME_TXID, ("shot", "19_green")]
    script += [LEFT, FULL]                                        # Fine: seed wiped, home
    script += load_seed_script(shots=False)[1:] + [RIGHT, ("shot", "20_must_choose_again"), ("stop",)]
    hw = FakeHardware(script)
    with pytest.raises(Stop):
        app.run(hw)
    assert hw.shots["19_green"].getpixel((240, 34)) == ui.GREEN
    for name in ("18_must_choose", "20_must_choose_again"):
        shot = hw.shots[name]
        assert shot.getpixel((240, 34)) != ui.GREEN, name          # still the seed screen, no result
        assert hint_shown(shot), f"{name}: hint shown"


def test_the_signer_cannot_change_within_a_seed_session(monkeypatch):
    """
    A Coldcard that skipped its grinding: red as "Coldcard Q". Going back to name "Trezor"
    (whose rule those signatures happen to satisfy) must not work, neither for the same
    transaction nor for the next one from the same device (reviewer's bypass: a lock per
    txid let a second transaction through). The signer is fixed after the first
    verification; only "Fine" / "Cambia seed" start over.
    """
    psbt = _trezor_like_psbt()
    second = _trezor_like_psbt(n_inputs=9)
    assert second.tx.txid() != psbt.tx.txid()
    shown = []
    real = app.ResultScreen

    class Spy(real):
        def __init__(self, report, signer=None, **kwargs):
            shown.append((signer, report.txid, report.verdict))
            super().__init__(report, signer, **kwargs)
    monkeypatch.setattr(app, "ResultScreen", Spy)
    script = load_seed_script(shots=False) + COLDCARD_Q + [RIGHT, ("blank",)] + ur_parts(psbt.serialize())
    script += [("shot", "21_red")]
    script += [RIGHT, ("blank",), ("tap", 40, 240)]               # Altra PSBT, then back to the seed screen
    script += [SIGNER_BUTTON, SIGNER_BUTTON, ("shot", "22_locked")]
    script += [RIGHT, ("blank",)] + ur_parts(psbt.serialize()) + [("shot", "23_same_tx_red")]
    script += [RIGHT, ("blank",)] + ur_parts(second.serialize()) + [("shot", "24_next_tx_red")]
    script += [LEFT, FULL] + load_seed_script(shots=False)[1:] + TREZOR
    script += [RIGHT, ("blank",)] + ur_parts(second.serialize()) + [("shot", "25_new_session"), ("stop",)]
    hw = FakeHardware(script)
    with pytest.raises(Stop):
        app.run(hw)
    for name in ("21_red", "23_same_tx_red", "24_next_tx_red"):
        assert hw.shots[name].getpixel((240, 34)) == ui.RED, name
    assert [s[0] for s in shown[:3]] == ["coldcard_q"] * 3, f"the signer stayed Coldcard Q: {shown}"
    assert hint_shown(hw.shots["22_locked"]), "locked hint"
    # A new seed session lets the user name a device again, but (v7) a transaction that came
    # out red stays red until power-off, under any name
    assert shown[3][0] == "trezor" and hw.shots["25_new_session"].getpixel((240, 34)) == ui.RED


def test_locked_seed_screen_unit():
    screen = app.SeedReadyScreen("73c5da0a", 24, True, "coldcard_q", locked=True)
    for _ in range(3):
        assert screen.on_event(Event(Event.TAP, *SIGNER_BUTTON[1:])) == "__redraw__"
    assert screen.signer == "coldcard_q" and screen.locked_hint
    assert screen.on_event(Event(Event.TAP, *RIGHT[1:])) == "scan_psbt"
    assert app.SeedReadyScreen("73c5da0a", 24, True, None, locked=True).locked is False, "nothing to lock yet"


def test_lock_after_red_unit():
    from nonceverify.verify import VERDICT__FAIL, VERDICT__OK, Report
    session = app.Session()
    session.signer = "coldcard_q"
    red = Report(fingerprint="73c5da0a", txid="ab" * 32, verdict=VERDICT__FAIL, seed_matches=True)
    app.lock_after_red(session, red)
    assert session.failed == {"ab" * 32: "coldcard_q"}
    session.signer = "trezor"
    green = Report(fingerprint="73c5da0a", txid="ab" * 32, verdict=VERDICT__OK)
    app.lock_after_red(session, green)
    assert green.verdict == VERDICT__FAIL and "Coldcard Q" in green.tx_problem
    other = Report(fingerprint="73c5da0a", txid="cd" * 32, verdict=VERDICT__OK)
    app.lock_after_red(session, other)
    assert other.verdict == VERDICT__OK, "another transaction is not affected"
    session.signer = "coldcard_q"
    again = Report(fingerprint="73c5da0a", txid="ab" * 32, verdict=VERDICT__OK)
    app.lock_after_red(session, again)
    assert again.verdict == VERDICT__OK, "same signer as the red: no override (its own verdict stands)"
    session.wipe()
    assert session.signer is None
    assert session.failed == {"ab" * 32: "coldcard_q"}, "v7: a red txid stays red until power-off"


# ---------------------------------------------------------------------------------------
# v5: the seed typed word by word
# ---------------------------------------------------------------------------------------
from nonceverify import wordentry                                                      # noqa: E402
from nonceverify.wordentry import SeedReviewScreen, WordEntryScreen, matches, next_letters  # noqa: E402

TYPE_SEED = ("tap", 357, 288)            # home: "Parole"
WORDS_12, WORDS_24 = ("tap", 122, 180), ("tap", 357, 180)


def _center(rect):
    return ("tap", (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)


def type_word(word):
    """ Taps for one word: up to four letters, then the word among the candidates """
    prefix = word[:4]
    taps = [_center(WordEntryScreen.key_rect(c)) for c in prefix]
    slot = matches(prefix).index(word)
    assert slot < WordEntryScreen.CAND_SLOTS, word
    return taps + [_center(WordEntryScreen.candidate_rect(slot))]


CONTROL = {k: _center(r) for k, r in WordEntryScreen.control_rects().items()}


def review_tap(words, i):
    return _center(SeedReviewScreen(list(words)).layout()[i])


def tap_event(t):
    return Event(Event.TAP, t[1], t[2])


def test_every_bip39_word_is_reachable_with_four_letters():
    from nonceverify import seedqr
    for w in seedqr.wordlist():
        found = matches(w[:4])
        assert w in found[:WordEntryScreen.CAND_SLOTS], (w, found[:6])
        assert all(c in next_letters(w[:i]) for i, c in enumerate(w[:4])), w
    assert "x" not in next_letters(""), "no BIP39 word starts with x"
    assert next_letters("zoo") == set() and matches("zoo") == ["zoo"]


def test_word_entry_screen_unit():
    screen = WordEntryScreen(0, 12)
    assert screen.on_event(tap_event(_center(WordEntryScreen.key_rect("x")))) is None, "dimmed key"
    assert screen.text == ""
    for c in "ab":
        assert screen.on_event(tap_event(_center(WordEntryScreen.key_rect(c)))) == "__redraw__"
    assert screen.text == "ab" and screen.candidates()[:2] == ["abandon", "ability"]
    assert screen.on_event(tap_event(_center(WordEntryScreen.key_rect("q")))) is None, "no 'abq...'"
    assert screen.on_event(tap_event(CONTROL["erase"])) == "__redraw__" and screen.text == "a"
    assert screen.on_event(tap_event(_center(WordEntryScreen.candidate_rect(0)))) == ("word", "abandon")
    assert screen.on_event(tap_event(CONTROL["prev"])) == ("prev",)
    assert screen.on_event(tap_event(CONTROL["cancel"])) == ("cancel",)
    empty = WordEntryScreen(0, 12)
    assert empty.on_event(tap_event(_center(WordEntryScreen.candidate_rect(0)))) is None, "no candidates yet"
    assert empty.on_event(tap_event(CONTROL["erase"])) is None


def test_review_needs_a_valid_checksum():
    good = MNEMONIC.split()
    screen = SeedReviewScreen(good)
    assert screen.valid and screen.on_event(tap_event(RIGHT)) == "ok"
    bad = list(good)
    bad[2] = "zoo" if bad[2] != "zoo" else "abandon"
    screen = SeedReviewScreen(bad)
    assert not screen.valid
    assert screen.on_event(tap_event(RIGHT)) is None, "Conferma does nothing on a bad checksum"
    assert screen.on_event(tap_event(review_tap(bad, 2))) == ("edit", 2)
    assert screen.on_event(tap_event(LEFT)) == "cancel"
    for n in (12, 24):
        cells = SeedReviewScreen(["abandon"] * (n - 1) + ["art"]).layout()
        assert len(cells) == n and all(c[3] < 288 - 22 for c in cells), "the grid stays above the bottom buttons"


def typed_seed_script(words, shots=True):
    script = [("tap", 240, 160)]
    script += [TYPE_SEED] + ([("shot", "30_word_count")] if shots else [])
    script += [WORDS_12 if len(words) == 12 else WORDS_24]
    for i, w in enumerate(words):
        taps = type_word(w)
        script += taps[:2]
        if shots and i == 0:
            script += [("shot", "31_word_typing")]
        script += taps[2:]
    if shots:
        script += [("shot", "32_review")]
    return script


def _after_review(passphrase=PASSPHRASE):
    return [RIGHT, RIGHT] + type_text(passphrase) + [("tap", 425, 280), RIGHT]


def test_typed_seed_gives_the_same_wallet_and_verifies():
    """ The seed typed word by word: same XFP as the SeedQR, then a green verification """
    psbt, keys, amounts = p2wpkh_psbt(2)
    psbt.sign_with(embit_root())
    script = typed_seed_script(MNEMONIC.split()) + _after_review() + [("shot", "33_seed_ready")]
    script += SEEDSIGNER + [RIGHT, ("blank",)] + ur_parts(psbt.serialize()) + [SAME_TXID, ("shot", "34_result"), ("stop",)]
    hw = FakeHardware(script)
    session = app.Session()
    with pytest.raises(Stop):
        app.run(hw, session)
    assert hw.shots["34_result"].getpixel((240, 34)) == ui.GREEN
    assert session.master.fingerprint.hex() == embit_root().my_fingerprint.hex()


def test_typed_24_words():
    from nonceverify import seedqr
    words = seedqr.entropy_to_mnemonic(bytes(range(32))).split()
    script = typed_seed_script(words, shots=False) + [("shot", "35_review24"), RIGHT, LEFT, ("shot", "36_ready"), ("stop",)]
    hw = FakeHardware(script)
    session = app.Session()
    with pytest.raises(Stop):
        app.run(hw, session)
    assert session.words == 24
    assert session.master.fingerprint.hex() == app.Session.fingerprint_of(" ".join(words), "")


def test_a_wrong_word_is_caught_and_fixed_from_the_review():
    words = MNEMONIC.split()
    wrong = list(words)
    wrong[5] = "zoo" if words[5] != "zoo" else "abandon"
    assert not SeedReviewScreen(wrong).valid
    script = [("tap", 240, 160), TYPE_SEED, WORDS_12]
    for w in wrong:
        script += type_word(w)
    script += [("shot", "37_bad_checksum"), RIGHT]                          # Conferma blocked
    script += [review_tap(wrong, 5)] + type_word(words[5]) + [("shot", "38_fixed"), RIGHT]
    script += [("shot", "39_passphrase_question"), ("stop",)]
    hw = FakeHardware(script)
    with pytest.raises(Stop):
        app.run(hw)
    assert hw.shots["37_bad_checksum"].getpixel((252, 272)) == ui.RED
    shot = hw.shots["39_passphrase_question"]
    assert any(shot.getpixel((x, y)) == ui.GREEN for x in range(ui.PAD, ui.PAD + 28) for y in range(ui.TOP + 4, ui.TOP + 28)), \
        "fixed: the passphrase question opens with the green 'checksum corretto'"


def test_back_goes_to_the_previous_word_and_cancel_goes_home():
    words = MNEMONIC.split()
    script = [("tap", 240, 160), TYPE_SEED, WORDS_12] + type_word("zoo")       # word 1 wrong
    script += [CONTROL["prev"]]                                                 # back to word 1, prefilled "zoo"
    script += [CONTROL["erase"]] * 3 + type_word(words[0])
    for w in words[1:]:
        script += type_word(w)
    script += [("shot", "40_review_ok"), LEFT, ("shot", "40b_confirm"), RIGHT, ("shot", "41_home"), ("stop",)]  # Annulla tutto
    hw = FakeHardware(script)
    session = app.Session()
    with pytest.raises(Stop):
        app.run(hw, session)
    assert session.master is None, "cancelled: no seed loaded"
    assert hw.shots["40_review_ok"].getpixel((252, 272)) == ui.ACCENT, "valid checksum: Conferma"


def test_word_list_is_cleared_after_entry(monkeypatch):
    """ The list holding the words is emptied as soon as the mnemonic is handed over """
    kept = []
    real = wordentry.SeedReviewScreen

    class Spy(real):
        def __init__(self, words):
            kept.append(words)
            super().__init__(words)
    monkeypatch.setattr(wordentry, "SeedReviewScreen", Spy)
    script = typed_seed_script(MNEMONIC.split(), shots=False) + [RIGHT, ("stop",)]
    hw = FakeHardware(script)
    with pytest.raises(Stop):
        app.run(hw)
    assert kept and all(w == "" for w in kept[0]), kept[0]


def test_cancel_asks_before_discarding_typed_words():
    """ "Annulla" sits next to "Cancella": with words typed it asks; "Continua" keeps everything """
    words = MNEMONIC.split()
    script = [("tap", 240, 160), TYPE_SEED, WORDS_12]
    for w in words[:5]:
        script += type_word(w)
    script += [CONTROL["cancel"], ("shot", "42_confirm"), LEFT]                 # Continua
    for w in words[5:]:
        script += type_word(w)
    script += [RIGHT, ("shot", "43_passphrase_question"), ("stop",)]            # Conferma on the review
    hw = FakeHardware(script)
    with pytest.raises(Stop):
        app.run(hw)
    shot = hw.shots["43_passphrase_question"]
    assert any(shot.getpixel((x, y)) == ui.GREEN for x in range(ui.PAD, ui.PAD + 28) for y in range(ui.TOP + 4, ui.TOP + 28))
    # Annulla on the very first word, nothing typed yet: straight home, no question
    hw = FakeHardware([("tap", 240, 160), TYPE_SEED, WORDS_12, CONTROL["cancel"], ("shot", "44_home"), ("stop",)])
    with pytest.raises(Stop):
        app.run(hw)
    assert any(hw.shots["44_home"].getpixel((x, 288)) == ui.TEXT for x in range(60, 200)), "home buttons"


def test_rivedi_goes_back_to_the_review_unchanged():
    words = MNEMONIC.split()
    script = [("tap", 240, 160), TYPE_SEED, WORDS_12]
    for w in words:
        script += type_word(w)
    script += [review_tap(words, 3), CONTROL["prev"], ("shot", "45_back_in_review"), RIGHT, ("stop",)]
    hw = FakeHardware(script)
    session = app.Session()
    kept = []
    real = wordentry.SeedReviewScreen

    class Spy(real):
        def __init__(self, w):
            kept.append(list(w))
            super().__init__(w)
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(wordentry, "SeedReviewScreen", Spy)
    try:
        with pytest.raises(Stop):
            app.run(hw, session)
    finally:
        mp.undo()
    assert kept == [words, words], "the second review shows the same words"


def test_word_entry_matches_a_reference_model(monkeypatch):
    """
    Reviewer's model check (v5): random sequences of words, Indietro/Rivedi, Annulla (with
    the question answered either way), edits from the review and Conferma, for 12 and 24
    words. Index and prefill always as the model says; what is returned is exactly the
    last review shown, with a valid checksum and the chosen length, or nothing.
    """
    import random
    from nonceverify import seedqr
    wl = seedqr.wordlist()
    loaded = 0
    for seed in range(1500):
        rnd = random.Random(seed)
        count = rnd.choice((12, 24))
        model = {"words": [""] * count, "index": 0, "rev": False}
        shown = []

        def we_run(self, hw):
            expected = "" if model["rev"] else model["words"][self.index]
            assert self.index == model["index"] and self.text == expected, (self.index, model, self.text)
            a = rnd.random()
            if a < 0.75:
                w = rnd.choice(wl[:3]) if rnd.random() < .5 else rnd.choice(wl)
                model["words"][self.index] = w
                model["index"] = count if model["rev"] else self.index + 1
                return ("word", w)
            if a < 0.97:
                if model["rev"]:
                    model["index"] = count
                elif self.index > 0:
                    model["index"] -= 1
                else:
                    model["pending"] = True
                return ("prev",)
            model["pending"] = True
            return ("cancel",)

        def confirm(hw, typed):
            assert typed == sum(1 for w in model["words"] if w)
            if model.pop("pending", None) is None and typed:
                raise AssertionError("asked without a cancel")
            drop = rnd.random() < 0.5 or typed == 0
            if drop:
                model["done"] = None
            return drop

        def rv_run(self, hw):
            assert self.words == model["words"] and all(self.words)
            shown.append(list(self.words))
            if self.valid and rnd.random() < .3:
                model["done"] = " ".join(self.words)
                return "ok"
            if rnd.random() < .03:
                model["pending"] = True
                return "cancel"
            i = rnd.randrange(count)
            model["index"], model["rev"] = i, True
            return ("edit", i)

        monkeypatch.setattr(wordentry.WordCountScreen, "run", lambda self, hw: count)
        monkeypatch.setattr(wordentry.WordEntryScreen, "run", we_run)
        monkeypatch.setattr(wordentry.SeedReviewScreen, "run", rv_run)
        monkeypatch.setattr(wordentry, "confirm_discard", confirm)
        out = wordentry.enter_mnemonic(None)
        assert out == model.get("done", "MISSING"), (seed, out, model)
        if out is not None:
            loaded += 1
            assert seedqr.mnemonic_is_valid(out) and len(out.split()) == count and out.split() == shown[-1]
    assert loaded > 100
