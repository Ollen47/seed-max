"""
v7: "Check address", against embit as the independent reference (test environment only).
"""
import os
import random

import pytest

from nonceverify import address as A
from nonceverify import crypto, transport

embit = pytest.importorskip("embit")
from embit import bip32, bip39, script                                     # noqa: E402
from embit.descriptor import Descriptor as EDescriptor                     # noqa: E402
from embit.descriptor.checksum import add_checksum                         # noqa: E402
from embit.networks import NETWORKS                                        # noqa: E402

MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
PASSPHRASE = "tre topi ciechi!"
OTHER1 = "legal winner thank year wave sausage worth useful legal winner thank yellow"
OTHER2 = "letter advice cage absurd amount doctor acoustic avoid letter advice cage above"


def master(mnemonic=MNEMONIC, passphrase=PASSPHRASE):
    return crypto.HDKey.from_seed(crypto.bip39_seed(mnemonic, passphrase))


def eroot(mnemonic=MNEMONIC, passphrase=PASSPHRASE):
    return bip32.HDKey.from_seed(bip39.mnemonic_to_seed(mnemonic, passphrase))


def eaddr(spk, net="main"):
    return spk.address(NETWORKS[net])


# ---------------------------------------------------------------------------------------
# Address parsing
# ---------------------------------------------------------------------------------------
def test_parse_address_all_types_match_embit():
    root = eroot()
    pub = root.derive("m/84h/0h/0h/0/3").key.get_public_key()
    ws = script.multisig(2, [pub, eroot(OTHER1).derive("m/0").key.get_public_key()])
    for spk, kind in ((script.p2wpkh(pub), A.KIND__P2WPKH), (script.p2sh(script.p2wpkh(pub)), A.KIND__P2SH),
                      (script.p2pkh(pub), A.KIND__P2PKH), (script.p2wsh(ws), A.KIND__P2WSH)):
        for net in ("main", "test"):
            text = eaddr(spk, net)
            parsed = A.parse_address(text)
            assert parsed.script_pubkey == spk.data and parsed.kind == kind and parsed.network == net
            assert A.parse_address(("bitcoin:" + text + "?amount=0.1&label=x").encode()).script_pubkey == spk.data
            if text.startswith(("bc1", "tb1")):
                assert A.parse_address(text.upper()).script_pubkey == spk.data, "uppercase bech32 QR"


def test_parse_address_rejects_damage():
    good = eaddr(script.p2wpkh(eroot().derive("m/84h/0h/0h/0/0").key.get_public_key()))
    bad = good[:-1] + ("q" if good[-1] != "q" else "p")
    with pytest.raises(A.AddressError, match="checksum"):
        A.parse_address(bad)
    with pytest.raises(A.AddressError, match="mixed case"):
        A.parse_address(good[:6].upper() + good[6:])
    legacy = eaddr(script.p2pkh(eroot().derive("m/44h/0h/0h/0/0").key.get_public_key()))
    with pytest.raises(A.AddressError, match="checksum"):
        A.parse_address(legacy[:-1] + ("2" if legacy[-1] != "2" else "3"))
    taproot = eaddr(script.p2tr(eroot().derive("m/86h/0h/0h/0/0").key.get_public_key()))
    with pytest.raises(A.AddressError, match="Taproot"):
        A.parse_address(taproot)
    # a bech32 (v0 checksum) string carrying a v1 program, or bech32m on v0: rejected
    from nonceverify.psbt import segwit_address
    with pytest.raises(A.AddressError):
        A.parse_address(segwit_address("bc", 0, b"\x01" * 20)[:-6] + segwit_address("bc", 1, b"\x01" * 32)[-6:])
    for junk in ("", "hello", "ur:crypto-psbt/xyz", "bc1" + "q" * 100):
        with pytest.raises(A.AddressError):
            A.parse_address(junk)
    assert A.grouped("bc1qabcdefgh") == "bc1q abcd efgh"


# ---------------------------------------------------------------------------------------
# Single-sig
# ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("purpose,maker", [(84, script.p2wpkh), (49, lambda p: script.p2sh(script.p2wpkh(p))),
                                           (44, script.p2pkh)])
def test_singlesig_found_by_derivation(purpose, maker):
    root, m = eroot(), master()
    for path, change, index, account in ((f"m/{purpose}h/0h/0h/0/0", False, 0, 0),
                                         (f"m/{purpose}h/0h/0h/1/119", True, 119, 0),
                                         (f"m/{purpose}h/0h/3h/0/7", False, 7, 3)):
        addr = A.parse_address(eaddr(maker(root.derive(path).key.get_public_key())))
        match = A.search_singlesig(m, addr)
        assert match is not None and (match.change, match.index, match.account) == (change, index, account), path
        assert crypto.format_path(match.path) == path.replace("h", "'")


def test_singlesig_not_found():
    m = master()
    root = eroot()
    for addr_text in (eaddr(script.p2wpkh(root.derive("m/84h/0h/0h/0/120").key.get_public_key())),    # beyond the gap
                      eaddr(script.p2wpkh(eroot(OTHER1).derive("m/84h/0h/0h/0/0").key.get_public_key())),   # another seed
                      eaddr(script.p2wpkh(eroot(MNEMONIC, "").derive("m/84h/0h/0h/0/0").key.get_public_key()))):  # no passphrase
        calls = []
        assert A.search_singlesig(m, A.parse_address(addr_text), progress=lambda d, t: calls.append((d, t))) is None
        assert calls and calls[-1][1] == 2 * (A.GAP_ACCOUNT0 + A.OTHER_ACCOUNTS * A.GAP_OTHER_ACCOUNTS)
    p2wsh = eaddr(script.p2wsh(script.multisig(1, [root.derive("m/0").key.get_public_key()])))
    with pytest.raises(A.AddressError, match="descriptor"):
        A.search_singlesig(m, A.parse_address(p2wsh))


def test_singlesig_testnet_uses_coin_type_1():
    root = eroot()
    addr = A.parse_address(eaddr(script.p2wpkh(root.derive("m/84h/1h/0h/0/5").key.get_public_key()), "test"))
    assert A.search_singlesig(master(), addr).index == 5


# ---------------------------------------------------------------------------------------
# Descriptors
# ---------------------------------------------------------------------------------------
def account_xpub(root, path, net="main"):
    return root.derive(path).to_public().to_base58(NETWORKS[net]["xpub"])


def multisig_descriptor(kind="wsh", sorted_=True, children="<0;1>/*", hardened="h", order=(0, 1, 2), net="main"):
    roots = [eroot(), eroot(OTHER1, ""), eroot(OTHER2, "")]
    st = {"wsh": 2, "sh-wsh": 1}.get(kind, 2)
    coin = 1 if net == "test" else 0
    origin = f"48{hardened}/{coin}{hardened}/0{hardened}/{st}{hardened}"
    keys = []
    for i in order:
        r = roots[i]
        xp = account_xpub(r, f"m/48h/{coin}h/0h/{st}h", net)
        keys.append(f"[{r.my_fingerprint.hex()}/{origin}]{xp}/{children}")
    inner = f"{'sortedmulti' if sorted_ else 'multi'}(2,{','.join(keys)})"
    body = {"wsh": f"wsh({inner})", "sh-wsh": f"sh(wsh({inner}))", "sh": f"sh({inner})"}[kind]
    return add_checksum(body), roots


def test_descriptor_checksum_matches_embit():
    text, _ = multisig_descriptor()
    body, checksum = text.split("#")
    assert A.descriptor_checksum(body) == checksum
    for _ in range(20):
        s = "".join(random.choice("abcxyz0123()[],/*h'") for _ in range(random.randint(1, 60)))
        assert A.descriptor_checksum(s) == add_checksum(s).split("#")[1]


@pytest.mark.parametrize("kind", ["wsh", "sh-wsh", "sh"])
@pytest.mark.parametrize("sorted_", [True, False])
def test_multisig_descriptor_addresses_match_embit(kind, sorted_):
    text, roots = multisig_descriptor(kind, sorted_)
    desc = A.parse_descriptor(text)
    assert desc.is_multisig and desc.threshold == 2 and len(desc.keys) == 3 and desc.sorted == sorted_
    check = A.check_seed_in_descriptor(desc, master())
    assert check.ours == {0: [48 | A.H, A.H, A.H, {"wsh": 2, "sh-wsh": 1, "sh": 2}[kind] | A.H]} and not check.problems
    ed = EDescriptor.from_string(text.split("#")[0])
    for change, index in ((0, 0), (1, 4), (0, 37)):
        spk = ed.derive(index, branch_index=change).script_pubkey()
        match = A.search_descriptor(desc, A.parse_address(eaddr(spk)))
        assert match is not None and match.index == index and match.change == bool(change), (kind, change, index)


def test_descriptor_receive_only_branch_infers_change():
    text, roots = multisig_descriptor(children="0/*", hardened="'")
    desc = A.parse_descriptor(text)
    assert desc.keys[0].inferred_change and desc.branch_count() == 2
    change_text = text.split("#")[0].replace("/0/*", "/1/*")
    spk = EDescriptor.from_string(change_text).derive(9).script_pubkey()
    match = A.search_descriptor(desc, A.parse_address(eaddr(spk)))
    assert match.change and match.index == 9


def test_descriptor_rejections():
    text, roots = multisig_descriptor()
    body, checksum = text.split("#")
    bad_checksum = body + "#" + ("q" if checksum[0] != "q" else "p") + checksum[1:]
    with pytest.raises(A.DescriptorError, match="WRONG"):
        A.parse_descriptor(bad_checksum)
    # an xpub swapped (the attacker's key), checksum recomputed: parses, but the seed check
    # and the comparison with the backup are what catch it
    evil = account_xpub(eroot("zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo wrong", ""), "m/48h/0h/0h/2h")
    xp_other = account_xpub(roots[1], "m/48h/0h/0h/2h")
    swapped = A.parse_descriptor(add_checksum(body.replace(xp_other, evil)))
    assert swapped.keys[1].xpub_text != A.parse_descriptor(text).keys[1].xpub_text
    # this seed's fingerprint, another key: red
    xp_ours = account_xpub(roots[0], "m/48h/0h/0h/2h")
    relabelled = A.parse_descriptor(add_checksum(body.replace(xp_ours, evil)))
    check = A.check_seed_in_descriptor(relabelled, master())
    assert not check.ours and any("altered" in p for p in check.problems)
    # wrong seed / passphrase
    assert A.check_seed_in_descriptor(A.parse_descriptor(text), master(MNEMONIC, "")).problems
    for broken in (body.replace("sortedmulti(2,", "sortedmulti(4,"),
                   body.replace("/<0;1>/*", "/<0;1>/*h"),
                   body.replace("/<0;1>/*", "/0h/*"),
                   "tr(" + xp_ours + ")",
                   body.replace(xp_ours, xp_ours[:-1] + ("A" if xp_ours[-1] != "A" else "B")),
                   body.replace(xp_other, xp_ours),
                   "wsh(sortedmulti(2,[deadbeef/48h/0h/0h/2h]" +
                   roots[0].derive("m/48h/0h/0h/2h").to_base58() + "/0/*))",       # an xprv
                   ):
        with pytest.raises(A.DescriptorError):
            A.parse_descriptor(broken)


def test_singlesig_descriptor():
    root = eroot()
    xp = account_xpub(root, "m/84h/0h/0h")
    text = add_checksum(f"wpkh([{root.my_fingerprint.hex()}/84h/0h/0h]{xp}/<0;1>/*)")
    desc = A.parse_descriptor(text)
    assert not desc.is_multisig and A.check_seed_in_descriptor(desc, master()).ours == {0: [84 | A.H, A.H, A.H]}
    spk = script.p2wpkh(root.derive("m/84h/0h/0h/1/11").key.get_public_key())
    assert A.search_descriptor(desc, A.parse_address(eaddr(spk))).index == 11
    wrong_type = script.p2pkh(root.derive("m/84h/0h/0h/0/0").key.get_public_key())
    with pytest.raises(A.AddressError, match="can't belong"):
        A.search_descriptor(desc, A.parse_address(eaddr(wrong_type)))


def test_descriptor_without_origin_found_at_standard_path():
    root = eroot()
    others = [account_xpub(eroot(OTHER1, ""), "m/48h/0h/0h/2h"), account_xpub(eroot(OTHER2, ""), "m/48h/0h/0h/2h")]
    mine = account_xpub(root, "m/48h/0h/3h/2h")
    desc = A.parse_descriptor(f"wsh(sortedmulti(2,{mine}/<0;1>/*,{others[0]}/<0;1>/*,{others[1]}/<0;1>/*))")
    assert A.check_seed_in_descriptor(desc, master()).ours == {0: [48 | A.H, A.H, 3 | A.H, 2 | A.H]}


# ---------------------------------------------------------------------------------------
# UR crypto-output and the collector
# ---------------------------------------------------------------------------------------
def _crypto_output_ur(text, fragment=None, children=True, parent=True, names=False):
    from urtypes.crypto import Output
    from urtypes.crypto.hd_key import HDKey as URHDKey
    from urtypes.crypto.keypath import Keypath, PathComponent
    from urtypes.crypto.multi_key import MultiKey
    from urtypes.crypto.output import SCRIPT_EXPRESSION_TAG_MAP
    from urtypes.crypto.coin_info import CoinInfo
    ed = EDescriptor.from_string(text.split("#")[0])

    def hdkey(k):
        origin = Keypath([PathComponent(i & 0x7FFFFFFF, bool(i & A.H)) for i in k.origin.derivation],
                         k.origin.fingerprint, len(k.origin.derivation))
        props = {"key": k.key.key.sec(), "chain_code": k.key.chain_code, "origin": origin, "use_info": CoinInfo(0, 0)}
        if children:
            props["children"] = Keypath([PathComponent(0, False), PathComponent(None, False)], None, None)
        if parent:
            props["parent_fingerprint"] = k.key.fingerprint
        if names:
            props["name"] = "Coldcard Q"
        return URHDKey(props)
    keys = ed.miniscript.args[1:] if ed.miniscript else None
    multi = MultiKey(2, [], [hdkey(k) for k in keys])
    output = Output([SCRIPT_EXPRESSION_TAG_MAP[401], SCRIPT_EXPRESSION_TAG_MAP[407]], multi)
    from seedsigner.helpers.ur2.ur import UR
    from seedsigner.helpers.ur2.ur_encoder import UREncoder
    enc = UREncoder(UR("crypto-output", output.to_cbor()), max_fragment_len=fragment or 10_000)
    return enc, output


def test_crypto_output_ur_single_and_animated():
    text, roots = multisig_descriptor(children="0/*")
    enc, output = _crypto_output_ur(text)
    c = transport.DescriptorCollector()
    c.add(enc.next_part().encode())
    desc = c.result
    assert desc.source == "ur" and desc.threshold == 2 and len(desc.keys) == 3
    assert [k.xpub_text for k in desc.keys] == [k.xpub_text for k in A.parse_descriptor(text).keys], \
        "the xpubs rebuilt from the UR are the ones written in the descriptor"
    assert A.check_seed_in_descriptor(desc, master()).ours
    spk = EDescriptor.from_string(text.split("#")[0]).derive(3).script_pubkey()
    assert A.search_descriptor(desc, A.parse_address(eaddr(spk))).index == 3
    enc, _ = _crypto_output_ur(text, fragment=60)
    c = transport.DescriptorCollector()
    for _ in range(200):
        c.add(enc.next_part().upper().encode())
        if c.complete:
            break
    assert c.complete and c.result.threshold == 2


def test_descriptor_collector_text_and_bbqr_and_wrong_qrs():
    from test_nonceverify import bbqr_parts
    text, _ = multisig_descriptor()
    c = transport.DescriptorCollector()
    c.add(("# Sparrow\n" + text + "\n").encode())
    assert c.complete and c.result.checksum == text.split("#")[1]
    parts = bbqr_parts(text.encode(), "U", "Z", chunk=200)
    c = transport.DescriptorCollector()
    for p in parts:
        c.add(p.encode())
    assert c.complete and c.result.threshold == 2
    with pytest.raises(transport.TransportError, match="PSBT"):
        transport.DescriptorCollector().add(b"ur:crypto-psbt/hdcxlkahssqzwfvslofzoxwkrewngotktbmwjkwdcmnefsaaehrlolkskncnktlbaypkvoonhknt")
    with pytest.raises((A.DescriptorError, transport.TransportError)):
        transport.DescriptorCollector().add(b"bc1qnotadescriptor")


def test_crypto_output_like_sparrow_pdf():
    """
    Sparrow's PDF export: ur:crypto-output, one static QR, keystore labels, no children
    (the wallet's receive and change branches are implied: <0;1>/*)
    """
    from embit.descriptor import Descriptor as ED
    text, roots = multisig_descriptor()
    enc, _ = _crypto_output_ur(text, children=False, names=True)
    c = transport.DescriptorCollector()
    c.add(enc.next_part().encode())
    desc = c.result
    assert desc.branch_count() == 2 and not desc.keys[0].inferred_change
    assert [k.xpub_text for k in desc.keys] == [k.xpub_text for k in A.parse_descriptor(text).keys], \
        "the xpubs shown are exactly the ones written on the backup"
    assert desc.keys[0].suffix() == "/<0;1>/*"
    ed = ED.from_string(text.split("#")[0])
    for change, index in ((0, 8), (1, 3)):
        spk = ed.derive(index, branch_index=change).script_pubkey()
        match = A.search_descriptor(desc, A.parse_address(eaddr(spk)))
        assert match.index == index and match.change == bool(change)
    enc, _ = _crypto_output_ur(text, children=False, parent=False)
    with pytest.raises(A.DescriptorError, match="parent fingerprint"):
        transport.DescriptorCollector().add(enc.next_part().encode())


def test_review_fixes_derivation_shown_and_mixed_branches_refused():
    text, roots = multisig_descriptor()
    body = text.split("#")[0]
    desc = A.parse_descriptor(text)
    assert [k.suffix() for k in desc.keys] == ["/<0;1>/*"] * 3
    assert A.parse_descriptor(multisig_descriptor(children="0/*")[0]).keys[0].suffix() == "/0/* (+ /1/* change)"
    first = body.index("/<0;1>/*")
    with pytest.raises(A.DescriptorError, match="different derivations"):
        A.parse_descriptor(body[:first] + "/<1;0>/*" + body[first + 8:])
    with pytest.raises(A.DescriptorError):
        A.parse_descriptor(body.replace("<0;1>", "<0;1;2>"))


def test_crypto_output_with_explicit_false_flags_like_sparrow(monkeypatch):
    """
    Device test, 8 Nov 2026: Sparrow's PDF QR was refused as "private or master key".
    Its crypto-hdkey writes is-master / is-private as explicit false: those are fine;
    only true (or a non-boolean) is refused.
    """
    from urtypes.crypto.hd_key import HDKey as URHDKey
    real = URHDKey.to_data_item

    def with_flags(self):
        item = real(self)
        item[1] = False
        item[2] = False
        return item
    monkeypatch.setattr(URHDKey, "to_data_item", with_flags)
    text, roots = multisig_descriptor()
    enc, _ = _crypto_output_ur(text, children=False, names=True)
    c = transport.DescriptorCollector()
    c.add(enc.next_part().encode())
    assert c.result.threshold == 2 and A.check_seed_in_descriptor(c.result, master()).ours

    def private(self):
        item = real(self)
        item[2] = True
        return item
    monkeypatch.setattr(URHDKey, "to_data_item", private)
    enc, _ = _crypto_output_ur(text, children=False)
    with pytest.raises(A.DescriptorError, match="private"):
        transport.DescriptorCollector().add(enc.next_part().encode())
