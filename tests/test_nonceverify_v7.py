"""
v7 of the Anti-Klepto Check (8 Nov 2026, after the first tests on the device):
- the signer is fixed as soon as "Scan PSBT" is pressed (Ollen's report: going back from
  the camera, the name could still be changed);
- no green before the txid has been compared with Sparrow's (forgotten twice in the field);
- "Check address": single-sig from the seed, multisig from the descriptor backup;
- two-line passphrase field, battery icon, English UI, no "non firma nulla" on the splash.
"""
import inspect
import io
import os
import re
import tokenize
from dataclasses import dataclass

import pytest

from nonceverify import app, hw as hwmod, ui
from nonceverify.hw import BatteryGauge, Event

pytest.importorskip("embit")
pytest.importorskip("qrcode")
pytest.importorskip("pyzbar.pyzbar")

from embit import script                                                          # noqa: E402
from embit.networks import NETWORKS                                               # noqa: E402
from test_nonceverify import embit_root, p2wpkh_psbt                              # noqa: E402
from test_nonceverify_ui import (WIPE, DESCRIPTOR_CONFIRM, COLDCARD_Q, FULL, LEFT, MIDDLE, RIGHT, SAME_TXID, ENDING_OK, SEEDSIGNER,  # noqa: E402
    SIGNER_BUTTON, TREZOR, FakeHardware, Stop, hint_shown, load_seed_script, text_mode_zbar, ur_parts)
from test_nonceverify_address import OTHER1, multisig_descriptor                  # noqa: E402

SINGLE = ("tap", 122, 194)          # AddressModeScreen
MULTI = ("tap", 240, 194)
BACK_FROM_CAMERA = ("tap", 40, 240)


def banner(shot):
    return shot.getpixel((240, 34))


def signers_spy(monkeypatch):
    shown = []
    real = app.ResultScreen

    class Spy(real):
        def __init__(self, report, signer=None, **kwargs):
            shown.append((signer, report.verdict, kwargs.get("txid_confirmed")))
            super().__init__(report, signer, **kwargs)
    monkeypatch.setattr(app, "ResultScreen", Spy)
    return shown


# ---------------------------------------------------------------------------------------
# Signer lock
# ---------------------------------------------------------------------------------------
def test_signer_fixed_as_soon_as_a_scan_starts(monkeypatch):
    """
    Ollen's path on the device: name the signer, Scan PSBT, back from the camera before any
    result, tap the signer button. In v5 the name changed (the lock came with the first
    verdict); in v7 it is fixed. "Change seed" starts a new choice.
    """
    shown = signers_spy(monkeypatch)
    psbt, keys, amounts = p2wpkh_psbt(1)
    psbt.sign_with(embit_root())
    script_ = load_seed_script(shots=False) + TREZOR + [RIGHT, ("blank",), BACK_FROM_CAMERA]
    script_ += [SIGNER_BUTTON, SIGNER_BUTTON, ("shot", "50_locked_before_any_result")]
    script_ += [RIGHT, ("blank",)] + ur_parts(psbt.serialize()) + [SAME_TXID, ENDING_OK, ("shot", "51_result")]
    script_ += [RIGHT, ("blank",), BACK_FROM_CAMERA, LEFT, WIPE]                  # Next PSBT, back, Change seed, Wipe
    script_ += load_seed_script(shots=False)[1:] + [("shot", "52_new_session"), ("stop",)]
    hw = FakeHardware(script_)
    session = app.Session()
    with pytest.raises(Stop):
        app.run(hw, session)
    assert hint_shown(hw.shots["50_locked_before_any_result"]), "locked hint"
    assert shown[0][0] == "trezor", "still Trezor after the taps"
    assert session.signer is None and not session.signer_locked, "Change seed: a new choice"


def test_locked_screen_ignores_the_signer_button():
    screen = app.SeedReadyScreen("73c5da0a", 24, True, "trezor", locked=True)
    for _ in range(4):
        screen.on_event(Event(Event.TAP, *SIGNER_BUTTON[1:]))
    assert screen.signer == "trezor" and screen.locked_hint


# ---------------------------------------------------------------------------------------
# Txid step
# ---------------------------------------------------------------------------------------
def test_txid_rows_cover_the_whole_txid():
    txid = "".join(f"{i:02x}" for i in range(32))
    rows = app.txid_rows(txid)
    assert len(rows) == 4 and all(len(r) == 19 for r in rows)
    assert "".join(rows).replace(" ", "") == txid


def test_green_needs_the_txid_and_a_different_txid_is_red(monkeypatch):
    shown = signers_spy(monkeypatch)
    psbt, keys, amounts = p2wpkh_psbt(2)
    psbt.sign_with(embit_root())
    script_ = load_seed_script(shots=False) + SEEDSIGNER + [RIGHT, ("blank",)] + ur_parts(psbt.serialize())
    script_ += [("shot", "53_txid_step"), SAME_TXID, ENDING_OK, ("shot", "54_green")]
    script_ += [RIGHT, ("blank",)] + ur_parts(psbt.serialize()) + [LEFT, ("shot", "55_txid_different"), ("stop",)]
    hw = FakeHardware(script_)
    with pytest.raises(Stop):
        app.run(hw)
    assert banner(hw.shots["53_txid_step"]) != ui.GREEN, "no green banner before the txid is confirmed"
    assert banner(hw.shots["54_green"]) == ui.GREEN
    assert banner(hw.shots["55_txid_different"]) == ui.RED
    assert [s[1:] for s in shown] == [("ok", True), ("fail", False)]


def test_result_screen_shows_the_full_txid():
    from nonceverify.verify import Report, VERDICT__OK
    txid = "ab12" * 16
    screen = app.ResultScreen(Report(fingerprint="73c5da0a", txid=txid, verdict=VERDICT__OK), "trezor", txid_confirmed=True)
    texts = "".join(line[0] for line in screen.list.lines).replace(" ", "")
    assert txid in texts
    assert screen.banner()[0] == ui.GREEN
    assert app.ResultScreen(Report(fingerprint="73c5da0a", txid=txid, verdict=VERDICT__OK), "trezor").banner()[0] != ui.GREEN


# ---------------------------------------------------------------------------------------
# Check address
# ---------------------------------------------------------------------------------------
def _addr(spk):
    return spk.address(NETWORKS["main"])


def test_address_single_sig_flow():
    root = embit_root()
    mine = _addr(script.p2wpkh(root.derive("m/84h/0h/0h/0/17").key.get_public_key()))
    foreign = _addr(script.p2wpkh(embit_root(OTHER1, "").derive("m/84h/0h/0h/0/0").key.get_public_key()))
    script_ = load_seed_script(shots=False) + [MIDDLE, ("shot", "56_address_mode"), SINGLE, ("blank",), ("qr", mine.upper())]
    script_ += [("shot", "57_address_mine"), RIGHT, ("qr", "bitcoin:" + foreign + "?amount=0.01"), ("shot", "58_address_foreign")]
    script_ += [LEFT, ("shot", "59_back_to_seed"), ("stop",)]
    hw = FakeHardware(script_)
    with pytest.raises(Stop):
        app.run(hw)
    assert banner(hw.shots["57_address_mine"]) == ui.GREEN
    assert banner(hw.shots["58_address_foreign"]) == ui.RED
    assert banner(hw.shots["59_back_to_seed"]) != ui.RED


def test_address_multisig_flow_with_the_descriptor_backup():
    from embit.descriptor import Descriptor as EDescriptor
    text, roots = multisig_descriptor()
    ed = EDescriptor.from_string(text.split("#")[0])
    receive = _addr(ed.derive(4, branch_index=0).script_pubkey())
    change = _addr(ed.derive(2, branch_index=1).script_pubkey())
    script_ = load_seed_script(shots=False) + [MIDDLE, MULTI, ("blank",), ("qr", text)]
    script_ += [("shot", "60_descriptor")] + DESCRIPTOR_CONFIRM + [("blank",), ("qr", receive), ("shot", "61_multisig_receive")]
    script_ += [LEFT, MIDDLE, ("shot", "62_mode_with_descriptor"), MULTI, ("qr", change), ("shot", "63_multisig_change")]
    script_ += [LEFT, ("stop",)]
    hw = FakeHardware(script_)
    session = app.Session()
    with pytest.raises(Stop):
        app.run(hw, session)
    assert banner(hw.shots["61_multisig_receive"]) == ui.GREEN
    assert banner(hw.shots["63_multisig_change"]) == ui.GREEN, "the descriptor stays for the session"
    assert session.descriptor is not None and session.descriptor.threshold == 2
    session.wipe()
    # v7.6: kept across "Change seed" (public, every key verified): the cosigner's seed
    # checks the same multisig change
    assert session.descriptor is not None and session.master is None


def test_descriptor_of_another_wallet_is_refused():
    text, roots = multisig_descriptor()
    psbt_seed_script = load_seed_script(passphrase="wrong", shots=False)
    script_ = psbt_seed_script + [MIDDLE, MULTI, ("blank",), ("qr", text), ("shot", "64_descriptor_red"), FULL,
                                  ("shot", "65_back_to_mode"), ("stop",)]
    hw = FakeHardware(script_)
    session = app.Session()
    with pytest.raises(Stop):
        app.run(hw, session)
    shot = hw.shots["64_descriptor_red"]
    assert any(shot.getpixel((x, y)) == ui.RED for x in range(10, 200) for y in range(36, 64)), "NOT USABLE"
    assert session.descriptor is None


def test_multisig_address_in_single_sig_mode_says_so():
    from embit.descriptor import Descriptor as EDescriptor
    text, roots = multisig_descriptor()
    receive = _addr(EDescriptor.from_string(text.split("#")[0]).derive(0, branch_index=0).script_pubkey())
    script_ = load_seed_script(shots=False) + [MIDDLE, SINGLE, ("blank",), ("qr", receive), ("shot", "66_not_checked"), ("stop",)]
    hw = FakeHardware(script_)
    with pytest.raises(Stop):
        app.run(hw)
    assert banner(hw.shots["66_not_checked"]) == ui.RED


# ---------------------------------------------------------------------------------------
# Passphrase on two lines, battery, texts
# ---------------------------------------------------------------------------------------
def test_passphrase_field_has_two_lines():
    kb = ui.KeyboardScreen("x" * 10)
    assert kb.field_lines() == ["x" * 10 + "_"]
    n = kb.per_line()
    assert n >= 40, n
    kb.text = "a" * (n + 5)
    lines = kb.field_lines()
    assert len(lines) == 2 and lines[0] == "a" * n
    kb.text = "b" * (3 * n)
    lines = kb.field_lines()
    assert len(lines) == 2 and lines[0].startswith("…") and lines[1].endswith("b_")
    kb.render()                                       # keys and field fit the screen
    assert all(b.rect[3] <= 320 for b in kb.keys())


@dataclass
class Reading:
    voltage: float
    current_ma: float
    percent: int

    @property
    def on_external_power(self):
        return self.current_ma > -60

    @property
    def charging(self):
        return self.current_ma > 80


class FakeGauge:
    def __init__(self, readings):
        self.readings = list(readings)

    def read(self):
        return self.readings.pop(0) if self.readings else None


def test_battery_gauge_median_cable_and_staleness():
    now = [0.0]
    gauge = BatteryGauge(FakeGauge([Reading(3.9, -450, 70), Reading(3.2, -450, 0), Reading(3.88, -450, 68)]),
                         clock=lambda: now[0])
    for _ in range(3):
        gauge.tick()
        now[0] += BatteryGauge.EVERY_S
    assert gauge.display() == (68, 68, False), "the median drops the sag"
    gauge.gauge.readings = [Reading(4.17, 320, 99)]
    gauge.tick()
    assert gauge.display() == (None, 68, True), "charging: no number, the last level, the bolt"
    now[0] += BatteryGauge.EVERY_S
    gauge.gauge.readings = [Reading(4.19, 40, 100)]
    gauge.tick()
    assert gauge.display() == (100, 100, True), "charged"
    now[0] += BatteryGauge.STALE_S + 1
    gauge.gauge.readings = []
    gauge.tick()
    assert gauge.display() is None, "no fresh reading: no icon"


def test_battery_gauge_gives_up_without_a_chip():
    now = [0.0]
    gauge = BatteryGauge(FakeGauge([]), clock=lambda: now[0])
    for _ in range(BatteryGauge.GIVE_UP_AFTER):
        gauge.tick()
        now[0] += BatteryGauge.EVERY_S
    assert not gauge.enabled and gauge.display() is None


def test_battery_start_without_the_driver_means_no_icon(monkeypatch):
    gauge = BatteryGauge()
    assert gauge.start() is False and not gauge.enabled and gauge.tick() is False


def test_header_battery_icon(monkeypatch):
    image, d = ui.new_canvas()
    ui.header(d, 1)
    before = image.crop((400, 0, 480, 30)).getcolors(10000)
    monkeypatch.setattr(ui, "battery_provider", lambda: (80, 80, False))
    image, d = ui.new_canvas()
    ui.header(d, 1)
    colors = {c for _n, c in image.crop((420, 0, 480, 30)).getcolors(10000)}
    assert ui.GREEN in colors, "green battery at the top right"
    monkeypatch.setattr(ui, "battery_provider", lambda: (10, 10, False))
    image, d = ui.new_canvas()
    ui.header(d, 1)
    assert ui.RED in {c for _n, c in image.crop((420, 0, 480, 30)).getcolors(10000)}
    monkeypatch.setattr(ui, "battery_provider", lambda: 1 / 0)
    image, d = ui.new_canvas()
    ui.header(d, 1)                                   # a failing provider: no icon, no crash
    assert image.crop((400, 0, 480, 30)).getcolors(10000) == before


def test_screens_redraw_when_the_battery_changes():
    class HW:
        def __init__(self):
            self.ticks, self.polls, self.shown = 0, 0, []

        def show(self, image):
            self.shown.append(image)

        def poll(self):
            self.polls += 1
            return [Event(Event.TAP, 240, 288)] if self.polls == 5 else []

        def wait(self, seconds=0):
            pass

        def battery_tick(self):
            self.ticks += 1
            return self.ticks == 3
    hw = HW()
    assert ui.MessageScreen("x", "y").run(hw) == "OK"
    assert len(hw.shown) == 2, "drawn once, redrawn once when the battery changed"


def test_splash_and_texts_are_english():
    assert "non firma" not in inspect.getsource(app)
    italian = re.compile(r"\b(non(?!-)|della|delle|questo|questa|firma|firmatario|parole|tocca|scegli|trasmettere|"
                         r"esito|annulla|conferma|indietro|cancella|spegni|errore|verifica)\b|[àèéìòù]", re.I)
    src = os.path.join(os.path.dirname(app.__file__))
    for name in sorted(os.listdir(src)):
        if not name.endswith(".py"):
            continue
        code = open(os.path.join(src, name), encoding="utf-8").read()
        for tok in tokenize.generate_tokens(io.StringIO(code).readline):
            if tok.type == tokenize.STRING and italian.search(tok.string):
                pytest.fail(f"{name}:{tok.start[0]}: {tok.string[:80]}")


def test_battery_bad_readings_never_break_a_screen(monkeypatch):
    now = [0.0]

    @dataclass
    class Bad:
        voltage: float = 3.9
        current_ma: float = -400
        percent: object = None
        on_external_power: bool = False
    gauge = BatteryGauge(FakeGauge([Bad(), Bad(percent=250), Bad(voltage=9.0, percent=50)]), clock=lambda: now[0])
    for _ in range(3):
        gauge.tick()
        now[0] += BatteryGauge.EVERY_S
    assert gauge.display() is None, "implausible readings are dropped"
    monkeypatch.setattr(ui, "battery_provider", lambda: (None, "x", False))
    image, d = ui.new_canvas()
    ui.header(d, 1)                                   # no exception


def test_red_txid_survives_change_seed():
    from nonceverify.verify import Report, VERDICT__FAIL, VERDICT__OK
    session = app.Session()
    session.signer = "coldcard_q"
    app.lock_after_red(session, Report(fingerprint="x", txid="cd" * 32, verdict=VERDICT__FAIL, seed_matches=True))
    session.wipe()
    session.signer = "trezor"
    later = Report(fingerprint="x", txid="cd" * 32, verdict=VERDICT__OK)
    app.lock_after_red(session, later)
    assert later.verdict == VERDICT__FAIL


def test_seed_mismatch_is_not_kept_as_red():
    from nonceverify.verify import Report, VERDICT__FAIL
    session = app.Session()
    session.signer = "coldcard_q"
    app.lock_after_red(session, Report(fingerprint="x", txid="ef" * 32, verdict=VERDICT__FAIL, seed_matches=False))
    assert session.failed == {}
