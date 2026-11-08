"""
Anti-Klepto Check v7: does this address belong to my wallet?

Before receiving (or to double check a change address) the address shown by Sparrow is
scanned and searched for, by derivation, among the addresses of the loaded seed:

- single-sig: the seed alone. The address type picks the derivation (bc1q... -> BIP84,
  3... -> BIP49 P2SH-P2WPKH, 1... -> BIP44); account 0 up to index GAP_ACCOUNT0 on
  receive and change, accounts 1-9 up to GAP_OTHER_ACCOUNTS;
- multisig (or any wallet): the wallet's output descriptor, scanned from the paper
  backup, never trusted blindly. Its checksum (BIP380) must be right, and the seed loaded
  must be one of its keys, by derivation: the key labelled with this seed's fingerprint
  must BE this seed's key at that path. The cosigners' keys can only be compared by eye
  with the backup, so they are shown (fingerprint, path, start and end of the xpub).

Standard library only, as the rest of the verifier: BIP32 public derivation, multisig
scripts, bech32 / base58 decoding, BIP380 checksums, a CBOR reader for UR crypto-output.
Nothing is written anywhere: descriptors and results live in RAM for the seed session.
"""
import hashlib
import hmac
import re
from dataclasses import dataclass, field

from nonceverify import crypto
from nonceverify.psbt import _B58, _BECH32, _bech32_polymod, address as spk_address

H = 0x80000000

GAP_ACCOUNT0 = 120                   # addresses tried per branch, account 0 (and descriptors)
GAP_OTHER_ACCOUNTS = 20              # single-sig accounts 1..OTHER_ACCOUNTS
OTHER_ACCOUNTS = 9
MAX_KEYS = 15                        # a standard multisig has at most 15 keys (P2SH limit 520 bytes)
MAX_PATH_DEPTH = 10
MAX_DESCRIPTOR_LEN = 8000


class AddressError(ValueError):
    pass


class DescriptorError(ValueError):
    pass


# ---------------------------------------------------------------------------------------
# Addresses: text -> scriptPubKey
# ---------------------------------------------------------------------------------------
KIND__P2WPKH = "p2wpkh"
KIND__P2WSH = "p2wsh"
KIND__P2SH = "p2sh"
KIND__P2PKH = "p2pkh"

BECH32M_CONST = 0x2BC830A3


def _from5(data) -> bytes:
    """ 5-bit groups to bytes, strict (BIP173): the padding must be < 5 zero bits """
    acc = bits = 0
    out = []
    for v in data:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    if bits >= 5 or acc & ((1 << bits) - 1):
        raise AddressError("invalid bech32 padding")
    return bytes(out)


def _bech32_decode(text: str):
    if text.lower() != text and text.upper() != text:
        raise AddressError("bech32 address in mixed case")
    text = text.lower()
    pos = text.rfind("1")
    if pos < 1 or pos + 7 > len(text) or len(text) > 90:
        raise AddressError("not a valid bech32 address")
    hrp, data = text[:pos], text[pos + 1:]
    values = [_BECH32.find(c) for c in data]
    if -1 in values:
        raise AddressError("not a valid bech32 address")
    hrp_exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    return hrp, values[:-6], _bech32_polymod(hrp_exp + values)


def b58decode_check(text: str) -> bytes:
    n = 0
    for c in text:
        i = _B58.find(c)
        if i < 0:
            raise AddressError("not a valid base58 string")
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    raw = b"\x00" * (len(text) - len(text.lstrip("1"))) + raw
    if len(raw) < 5:
        raise AddressError("base58 string too short")
    payload, checksum = raw[:-4], raw[-4:]
    if crypto.dsha256(payload)[:4] != checksum:
        raise AddressError("base58 checksum wrong: mistyped or damaged")
    return payload


@dataclass
class Address:
    text: str               # as it will be shown (lowercase for bech32)
    script_pubkey: bytes
    kind: str
    network: str            # "main" or "test"


def parse_address(payload) -> Address:
    """
    An address from a scanned QR: bare, or a BIP21 URI ("bitcoin:ADDRESS?amount=..."),
    any case (bech32 QR codes are often uppercase). Checksum verified.
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("ascii")
        except UnicodeDecodeError:
            raise AddressError("this QR is not an address")
    text = payload.strip()
    if text.lower().startswith("bitcoin:"):
        text = text[8:].split("?", 1)[0]
    if not text or len(text) > 100 or any(c.isspace() for c in text):
        raise AddressError("this QR is not an address")
    low = text.lower()
    if low.startswith(("bc1", "tb1", "bcrt1")):
        hrp, data, const = _bech32_decode(text)
        if hrp not in ("bc", "tb", "bcrt") or not data:
            raise AddressError("not a Bitcoin address")
        version, program = data[0], _from5(data[1:])
        if version == 1:
            raise AddressError("Taproot address (bc1p): not supported by this check")
        if version != 0:
            raise AddressError("unknown segwit version")
        if const != 1:
            raise AddressError("bech32 checksum wrong: mistyped or damaged")
        if len(program) == 20:
            kind = KIND__P2WPKH
        elif len(program) == 32:
            kind = KIND__P2WSH
        else:
            raise AddressError("invalid segwit program length")
        return Address(low, bytes([0, len(program)]) + program, kind, "main" if hrp == "bc" else "test")
    payload = b58decode_check(text)
    if len(payload) != 21:
        raise AddressError("not a Bitcoin address")
    kinds = {0x00: (KIND__P2PKH, "main"), 0x05: (KIND__P2SH, "main"),
             0x6F: (KIND__P2PKH, "test"), 0xC4: (KIND__P2SH, "test")}
    if payload[0] not in kinds:
        raise AddressError("not a Bitcoin address")
    kind, network = kinds[payload[0]]
    h = payload[1:]
    spk = (b"\x76\xa9\x14" + h + b"\x88\xac") if kind == KIND__P2PKH else (b"\xa9\x14" + h + b"\x87")
    return Address(text, spk, kind, network)


def grouped(text: str, size: int = 4) -> str:
    """ "bc1q xy2k gdyg..." : groups of four, easy to compare by eye """
    return " ".join(text[i:i + size] for i in range(0, len(text), size))


# ---------------------------------------------------------------------------------------
# BIP32 public derivation
# ---------------------------------------------------------------------------------------
def ckd_pub(point, chain: bytes, index: int):
    """ Child (point, chain code) of a public node, non-hardened index only """
    if index & H:
        raise DescriptorError("hardened step after an xpub")
    data = crypto.pubkey_compressed(point) + index.to_bytes(4, "big")
    I = hmac.new(chain, data, hashlib.sha512).digest()
    tweak = int.from_bytes(I[:32], "big")
    if tweak >= crypto.N:
        raise DescriptorError("invalid BIP32 child")
    child = crypto.point_add(crypto.point_mul_g(tweak), point)
    if child is None:
        raise DescriptorError("invalid BIP32 child")
    return child, I[32:]


# ---------------------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------------------
def multisig_script(m: int, pubkeys) -> bytes:
    return bytes([0x50 + m]) + b"".join(bytes([33]) + pk for pk in pubkeys) + bytes([0x50 + len(pubkeys), 0xAE])


def p2wsh(ws: bytes) -> bytes:
    return b"\x00\x20" + crypto.sha256(ws)


def p2sh(redeem: bytes) -> bytes:
    return b"\xa9\x14" + crypto.hash160(redeem) + b"\x87"


def p2wpkh(pk: bytes) -> bytes:
    return b"\x00\x14" + crypto.hash160(pk)


def p2pkh(pk: bytes) -> bytes:
    return b"\x76\xa9\x14" + crypto.hash160(pk) + b"\x88\xac"


# ---------------------------------------------------------------------------------------
# Single-sig search
# ---------------------------------------------------------------------------------------
@dataclass
class Match:
    path: list                      # full path from the master (single-sig) or the branch suffix
    change: bool
    index: int
    account: int = None
    label: str = ""                 # e.g. "BIP84 native segwit"


SINGLESIG = {
    KIND__P2WPKH: (84, "BIP84 native segwit", p2wpkh),
    KIND__P2SH: (49, "BIP49 nested segwit", lambda pk: p2sh(p2wpkh(pk))),
    KIND__P2PKH: (44, "BIP44 legacy", p2pkh),
}


def singlesig_plan():
    """ [(account, gap)] in search order """
    return [(0, GAP_ACCOUNT0)] + [(a, GAP_OTHER_ACCOUNTS) for a in range(1, OTHER_ACCOUNTS + 1)]


def search_singlesig(master: crypto.HDKey, addr: Address, progress=None) -> Match:
    """
    The single-sig path of `addr` under this seed, or None. `progress(done, total)` is
    called as the search goes (total = addresses that would be tried in the worst case).
    """
    if addr.kind == KIND__P2WSH:
        raise AddressError("this is a multisig (P2WSH) address: scan the wallet's descriptor first")
    purpose, label, script_of = SINGLESIG[addr.kind]
    coin = 1 if addr.network == "test" else 0
    plan = singlesig_plan()
    total = sum(2 * gap for _a, gap in plan)
    done = 0
    for account, gap in plan:
        node = master.derive([purpose | H, coin | H, account | H])
        try:
            point, chain = crypto.pubkey_decode(node.pubkey), node.chain
        finally:
            node.wipe()
        for change in (0, 1):
            branch_point, branch_chain = ckd_pub(point, chain, change)
            for index in range(gap):
                if progress and index % 10 == 0:
                    progress(done, total)
                done += 1
                child, _c = ckd_pub(branch_point, branch_chain, index)
                if script_of(crypto.pubkey_compressed(child)) == addr.script_pubkey:
                    return Match([purpose | H, coin | H, account | H, change, index], bool(change), index,
                                 account, label)
    return None


# ---------------------------------------------------------------------------------------
# Output descriptors (BIP380-386)
# ---------------------------------------------------------------------------------------
INPUT_CHARSET = ("0123456789()[],'/*abcdefgh@:$%{}IJKLMNOPQRSTUVWXYZ&+-.;<=>?!^_|~"
                 "ijklmnopqrstuvwxyzABCDEFGH`#\"\\ ")
CHECKSUM_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_GEN = (0xF5DEE51989, 0xA9FDCA3312, 0x1BAB10E32D, 0x3706B1677A, 0x644D626FFD)


def descriptor_checksum(text: str) -> str:
    """ BIP380 checksum of a descriptor (without its '#'), or None if it has bad characters """
    def polymod(c, val):
        top = c >> 35
        c = ((c & 0x7FFFFFFFF) << 5) ^ val
        for i in range(5):
            if (top >> i) & 1:
                c ^= _GEN[i]
        return c
    c, cls, count = 1, 0, 0
    for ch in text:
        pos = INPUT_CHARSET.find(ch)
        if pos == -1:
            return None
        c = polymod(c, pos & 31)
        cls = cls * 3 + (pos >> 5)
        count += 1
        if count == 3:
            c = polymod(c, cls)
            cls, count = 0, 0
    if count:
        c = polymod(c, cls)
    for _ in range(8):
        c = polymod(c, 0)
    c ^= 1
    return "".join(CHECKSUM_CHARSET[(c >> (5 * (7 - i))) & 31] for i in range(8))


XPUB_VERSIONS = {
    "0488b21e": "main", "049d7cb2": "main", "04b24746": "main", "0295b43f": "main", "02aa7ed3": "main",
    "043587cf": "test", "044a5262": "test", "045f1cf6": "test", "024289ef": "test", "02575483": "test",
}
XPRV_VERSIONS = ("0488ade4", "04358394", "049d7878", "04b2430c", "0295b005", "02aa7a99",
                 "044a4e28", "045f18bc", "024285b5", "02575048")


@dataclass
class DescriptorKey:
    origin_fingerprint: bytes       # label from the descriptor (not trusted), or None
    origin_path: list               # path from the master, or None if not given
    xpub_text: str                  # as written (or rebuilt from a UR)
    pubkey: bytes                   # compressed
    chain: bytes
    network: str
    branches: list                  # [[i, j, ...], ...]: fixed non-hardened steps per branch
    wildcard: bool                  # each branch ends in /*
    inferred_change: bool = False   # "0/*" only: the change branch "1/*" is searched too

    @property
    def point(self):
        return crypto.pubkey_decode(self.pubkey)

    def suffix(self) -> str:
        """ The derivation after the xpub, as written: "/<0;1>/*", "/0/*" ... """
        if len(self.branches) > 1 and not self.inferred_change:
            steps = [f"<{';'.join(str(b[i]) for b in self.branches)}>"
                     if len({b[i] for b in self.branches}) > 1 else str(self.branches[0][i])
                     for i in range(len(self.branches[0]))]
        else:
            steps = [str(i) for i in self.branches[0]]
        text = "".join("/" + s for s in steps) + ("/*" if self.wildcard else "")
        return text + (" (+ /1/* change)" if self.inferred_change else "")


TYPE__WSH = "wsh"
TYPE__SH_WSH = "sh-wsh"
TYPE__SH = "sh"
TYPE__WPKH = "wpkh"
TYPE__SH_WPKH = "sh-wpkh"
TYPE__PKH = "pkh"

TYPE_NAMES = {
    TYPE__WSH: "P2WSH multisig", TYPE__SH_WSH: "P2SH-P2WSH multisig", TYPE__SH: "P2SH multisig (legacy)",
    TYPE__WPKH: "P2WPKH single-sig", TYPE__SH_WPKH: "P2SH-P2WPKH single-sig", TYPE__PKH: "P2PKH single-sig",
}


@dataclass
class Descriptor:
    type: str
    keys: list
    threshold: int = None           # multisig only
    sorted: bool = True
    checksum: str = None            # as given in the text, None if absent (or a UR)
    source: str = "text"            # "text" or "ur"
    network: str = "main"

    @property
    def is_multisig(self) -> bool:
        return self.threshold is not None

    def describe(self) -> str:
        if self.is_multisig:
            return f"{self.threshold}-of-{len(self.keys)} {TYPE_NAMES[self.type]}" + ("" if self.sorted else " (multi, unsorted)")
        return TYPE_NAMES[self.type]

    def branch_count(self) -> int:
        return len(self.keys[0].branches)

    def script_pubkey(self, branch: int, pubkeys) -> bytes:
        if self.type in (TYPE__WPKH, TYPE__SH_WPKH, TYPE__PKH):
            pk = pubkeys[0]
            return {TYPE__WPKH: p2wpkh, TYPE__SH_WPKH: lambda k: p2sh(p2wpkh(k)), TYPE__PKH: p2pkh}[self.type](pk)
        keys = sorted(pubkeys) if self.sorted else list(pubkeys)
        ws = multisig_script(self.threshold, keys)
        if self.type == TYPE__WSH:
            return p2wsh(ws)
        if self.type == TYPE__SH_WSH:
            return p2sh(p2wsh(ws))
        return p2sh(ws)

    def address_kind(self) -> str:
        return {TYPE__WSH: KIND__P2WSH, TYPE__SH_WSH: KIND__P2SH, TYPE__SH: KIND__P2SH,
                TYPE__WPKH: KIND__P2WPKH, TYPE__SH_WPKH: KIND__P2SH, TYPE__PKH: KIND__P2PKH}[self.type]


def _parse_index(text: str, hardened_ok: bool) -> int:
    hardened = text[-1:] in ("h", "H", "'")
    digits = text[:-1] if hardened else text
    if not digits.isdigit() or len(digits) > 10:
        raise DescriptorError(f"bad derivation step «{text[:12]}»")
    n = int(digits)
    if n >= H:
        raise DescriptorError("derivation index out of range")
    if hardened and not hardened_ok:
        raise DescriptorError("hardened step after an xpub: not derivable from a public key")
    return n | H if hardened else n


def decode_xpub(text: str):
    """ (pubkey, chain code, network) of an xpub/ypub/zpub/tpub/... string """
    if not 100 <= len(text) <= 120:
        raise DescriptorError("not an extended public key")
    try:
        raw = b58decode_check(text)
    except AddressError as e:
        raise DescriptorError(f"extended key: {e}")
    if len(raw) != 78:
        raise DescriptorError("not an extended public key")
    version = raw[:4].hex()
    if version in XPRV_VERSIONS:
        raise DescriptorError("the descriptor contains a PRIVATE key: it must never leave the signer")
    if version not in XPUB_VERSIONS:
        raise DescriptorError("unknown extended key version")
    chain, key = raw[13:45], raw[45:]
    if key[0] not in (2, 3):
        raise DescriptorError("not an extended public key")
    crypto.pubkey_decode(key)          # on the curve
    return key, chain, XPUB_VERSIONS[version]


def _parse_key(text: str) -> DescriptorKey:
    fingerprint, origin = None, None
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise DescriptorError("key origin not closed")
        parts = text[1:end].split("/")
        if len(parts[0]) != 8 or not re.fullmatch(r"[0-9a-fA-F]{8}", parts[0]):
            raise DescriptorError("key origin: bad fingerprint")
        fingerprint = bytes.fromhex(parts[0])
        origin = [_parse_index(p, True) for p in parts[1:]]
        if len(origin) > MAX_PATH_DEPTH:
            raise DescriptorError("key origin too deep")
        text = text[end + 1:]
    segments = text.split("/")
    key_text, steps = segments[0], segments[1:]
    if re.fullmatch(r"0[23][0-9a-fA-F]{64}", key_text):
        raise DescriptorError("bare public keys are not supported: use the wallet's xpub descriptor")
    pubkey, chain, network = decode_xpub(key_text)
    if len(steps) > MAX_PATH_DEPTH:
        raise DescriptorError("derivation too deep")
    branches = [[]]
    wildcard = False
    multipath_seen = False
    for n, step in enumerate(steps):
        if step in ("*", "*'", "*h", "*H"):
            if step != "*":
                raise DescriptorError("hardened wildcard after an xpub: not derivable")
            if n != len(steps) - 1:
                raise DescriptorError("wildcard must be the last step")
            wildcard = True
        elif step.startswith("<") and step.endswith(">"):
            if multipath_seen:
                raise DescriptorError("only one <a;b> group is allowed")
            multipath_seen = True
            options = [_parse_index(o, False) for o in step[1:-1].split(";")]
            if len(options) != 2 or len(set(options)) != 2:
                raise DescriptorError("bad <a;b> group")
            branches = [b + [o] for b in branches for o in options]
        else:
            i = _parse_index(step, False)
            branches = [b + [i] for b in branches]
    key = DescriptorKey(fingerprint, origin, key_text, pubkey, chain, network, branches, wildcard)
    return key


def _split_args(inner: str) -> list:
    """ Split "a,b,c" at top-level commas (none of our arguments nest brackets) """
    out, depth, start = [], 0, 0
    for i, c in enumerate(inner):
        if c in "([<":
            depth += 1
        elif c in ")]>":
            depth -= 1
            if depth < 0:
                raise DescriptorError("unbalanced brackets")
        elif c == "," and depth == 0:
            out.append(inner[start:i])
            start = i + 1
    out.append(inner[start:])
    if depth:
        raise DescriptorError("unbalanced brackets")
    return out


def _unwrap(text: str, name: str):
    prefix = name + "("
    if text.startswith(prefix) and text.endswith(")"):
        return text[len(prefix):-1]
    return None


def parse_descriptor(payload) -> Descriptor:
    """
    A descriptor from a scanned QR: plain text (with or without '#checksum'), as most
    wallets print it. A wrong checksum is an error: the backup is damaged or altered.
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            raise DescriptorError("this QR is not a descriptor")
    if len(payload) > MAX_DESCRIPTOR_LEN:
        raise DescriptorError("descriptor too long")
    lines = [l.strip() for l in payload.replace("\r", "\n").split("\n")]
    lines = [l for l in lines if l and not l.startswith("#")]
    if len(lines) != 1:
        raise DescriptorError("this QR is not a descriptor" if not lines else
                              "more than one descriptor in this QR")
    text = lines[0]
    checksum = None
    if "#" in text:
        text, checksum = text.rsplit("#", 1)
        if len(checksum) != 8 or any(c not in CHECKSUM_CHARSET for c in checksum):
            raise DescriptorError("bad descriptor checksum format")
        if descriptor_checksum(text) != checksum:
            raise DescriptorError("descriptor checksum WRONG: the backup is damaged or has been altered")
    elif descriptor_checksum(text) is None:
        raise DescriptorError("this QR is not a descriptor")
    desc = _parse_expression(text)
    desc.checksum = checksum
    desc.source = "text"
    return _finish(desc)


def _parse_expression(text: str) -> Descriptor:
    for outer, inner_name, kind in (("wsh", None, TYPE__WSH), ("sh", "wsh", TYPE__SH_WSH), ("sh", None, TYPE__SH)):
        body = _unwrap(text, outer)
        if body is None:
            continue
        if inner_name:
            body = _unwrap(body, inner_name)
            if body is None:
                continue
        for multi, is_sorted in (("sortedmulti", True), ("multi", False)):
            args = _unwrap(body, multi)
            if args is None:
                continue
            parts = _split_args(args)
            if not parts[0].isdigit():
                raise DescriptorError("multisig threshold missing")
            keys = [_parse_key(p) for p in parts[1:]]
            return Descriptor(kind, keys, int(parts[0]), is_sorted)
    for outer, inner_name, kind in (("wpkh", None, TYPE__WPKH), ("sh", "wpkh", TYPE__SH_WPKH), ("pkh", None, TYPE__PKH)):
        body = _unwrap(text, outer)
        if body is None:
            continue
        if inner_name:
            body = _unwrap(body, inner_name)
            if body is None:
                continue
        if "," in body:
            raise DescriptorError("single-key descriptor with more than one key")
        return Descriptor(kind, [_parse_key(body)])
    if text.startswith("tr("):
        raise DescriptorError("Taproot descriptor: not supported by this check")
    raise DescriptorError("unsupported descriptor (wsh/sh multisig, wpkh, sh(wpkh), pkh)")


def _finish(desc: Descriptor) -> Descriptor:
    keys = desc.keys
    if not keys or len(keys) > MAX_KEYS:
        raise DescriptorError("wrong number of keys")
    if desc.is_multisig and not 1 <= desc.threshold <= len(keys):
        raise DescriptorError("multisig threshold out of range")
    if desc.type == TYPE__SH and len(multisig_script(desc.threshold, [k.pubkey for k in keys])) > 520:
        raise DescriptorError("too many keys for P2SH")
    if len({k.pubkey for k in keys}) != len(keys):
        raise DescriptorError("the same key appears twice")
    networks = {k.network for k in keys}
    if len(networks) != 1:
        raise DescriptorError("keys of different networks")
    desc.network = networks.pop()
    shape = {(tuple(map(tuple, k.branches)), k.wildcard) for k in keys}
    if len(shape) != 1:
        raise DescriptorError("the keys have different derivations after the xpub: not a standard wallet")
    # Wallets that write only the receive branch ("/0/*"): their change is "/1/*"
    if all(k.wildcard and k.branches == [k.branches[0]] and k.branches[0][-1:] == [0] for k in keys):
        for k in keys:
            k.branches = [k.branches[0], k.branches[0][:-1] + [1]]
            k.inferred_change = True
    return desc


# ---------------------------------------------------------------------------------------
# UR crypto-output (BCR-2020-010): CBOR -> Descriptor
# ---------------------------------------------------------------------------------------
class Tag:
    def __init__(self, tag, value):
        self.tag, self.value = tag, value


MAX_CBOR_DEPTH = 12
MAX_CBOR_ITEMS = 400


def cbor_read(data: bytes):
    """ Full CBOR item (ints, bytes, text, arrays, maps, tags, booleans), strict, bounded """
    budget = [MAX_CBOR_ITEMS]

    def head(i):
        if i >= len(data):
            raise DescriptorError("CBOR truncated")
        major, info = data[i] >> 5, data[i] & 31
        i += 1
        if info < 24:
            return major, info, i
        size = {24: 1, 25: 2, 26: 4, 27: 8}.get(info)
        if size is None or i + size > len(data):
            raise DescriptorError("CBOR not supported")
        return major, int.from_bytes(data[i:i + size], "big"), i + size

    def item(i, depth):
        budget[0] -= 1
        if depth > MAX_CBOR_DEPTH or budget[0] < 0:
            raise DescriptorError("CBOR too complex")
        major, arg, i = head(i)
        if major == 0:
            return arg, i
        if major in (2, 3):
            if i + arg > len(data):
                raise DescriptorError("CBOR truncated")
            raw = data[i:i + arg]
            if major == 3:
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError:
                    raise DescriptorError("CBOR text not UTF-8")
            return raw, i + arg
        if major == 4:
            if arg > MAX_CBOR_ITEMS:
                raise DescriptorError("CBOR array too long")
            out = []
            for _ in range(arg):
                v, i = item(i, depth + 1)
                out.append(v)
            return out, i
        if major == 5:
            if arg > 32:
                raise DescriptorError("CBOR map too long")
            out = {}
            for _ in range(arg):
                k, i = item(i, depth + 1)
                v, i = item(i, depth + 1)
                if not isinstance(k, (int, str)) or k in out:
                    raise DescriptorError("CBOR map key not valid")
                out[k] = v
            return out, i
        if major == 6:
            v, i = item(i, depth + 1)
            return Tag(arg, v), i
        if major == 7 and arg in (20, 21):
            return arg == 21, i
        raise DescriptorError("CBOR not supported")

    value, end = item(0, 0)
    if end != len(data):
        raise DescriptorError("CBOR: bytes after the item")
    return value


TAG__SH, TAG__WSH, TAG__PKH, TAG__WPKH, TAG__MULTI, TAG__SORTEDMULTI = 400, 401, 403, 404, 406, 407
TAG__HDKEY, TAG__KEYPATH, TAG__COININFO = 303, 304, 305


def _keypath(tagged) -> tuple:
    """ ([(index or None for '*', hardened)], source fingerprint or None, depth or None) """
    if not isinstance(tagged, Tag) or tagged.tag != TAG__KEYPATH or not isinstance(tagged.value, dict):
        raise DescriptorError("crypto-output: bad keypath")
    comps = tagged.value.get(1, [])
    if not isinstance(comps, list) or len(comps) % 2 or len(comps) > 2 * MAX_PATH_DEPTH:
        raise DescriptorError("crypto-output: bad keypath")
    steps = []
    for i in range(0, len(comps), 2):
        index, hardened = comps[i], comps[i + 1]
        if not isinstance(hardened, bool):
            raise DescriptorError("crypto-output: bad keypath")
        if index == []:
            steps.append((None, hardened))
        elif isinstance(index, int) and not isinstance(index, bool) and 0 <= index < H:
            steps.append((index, hardened))
        else:
            raise DescriptorError("crypto-output: keypath ranges are not supported")
    fp = tagged.value.get(2)
    if fp is not None and (not isinstance(fp, int) or isinstance(fp, bool) or not 0 <= fp < 2 ** 32):
        raise DescriptorError("crypto-output: bad fingerprint")
    depth = tagged.value.get(3)
    if depth is not None and (not isinstance(depth, int) or isinstance(depth, bool) or not 0 <= depth <= 255):
        raise DescriptorError("crypto-output: bad depth")
    return steps, (fp.to_bytes(4, "big") if fp is not None else None), depth


def _ur_key(tagged) -> str:
    """
    (descriptor key text "[fp/origin]xpub/children", exact) for a crypto-hdkey. Wallets
    exporting a whole wallet (Sparrow) leave the children out: they are the standard
    receive and change branches, "<0;1>/*".
    """
    if not isinstance(tagged, Tag) or tagged.tag != TAG__HDKEY or not isinstance(tagged.value, dict):
        raise DescriptorError("crypto-output: only extended keys are supported")
    m = tagged.value
    # is-master (1) and is-private (2) may be written as explicit "false" (Sparrow does):
    # only a true value means a master or private key
    if m.get(1) is True or m.get(2) is True or (2 in m and not isinstance(m[2], bool)):
        raise DescriptorError("crypto-output: private or master key")
    key, chain = m.get(3), m.get(4)
    if not isinstance(key, bytes) or len(key) != 33 or not isinstance(chain, bytes) or len(chain) != 32:
        raise DescriptorError("crypto-output: key without chain code")
    network = 0
    if 5 in m:
        info = m[5]
        if not isinstance(info, Tag) or info.tag != TAG__COININFO or not isinstance(info.value, dict):
            raise DescriptorError("crypto-output: bad coin info")
        if info.value.get(1, 0) != 0:
            raise DescriptorError("crypto-output: not a Bitcoin key")
        network = info.value.get(2, 0)
        if network not in (0, 1):
            raise DescriptorError("crypto-output: unknown network")
    origin, fp, depth = ([], None, None) if 6 not in m else _keypath(m[6])
    if any(i is None for i, _h in origin):
        raise DescriptorError("crypto-output: wildcard in the key origin")
    if origin and 8 not in m:
        # Without it the xpub can't be rebuilt as written on the backup, and the keys
        # couldn't be compared with it: refused (Sparrow's export carries it)
        raise DescriptorError("this UR has no parent fingerprint, so its keys can't be compared with your "
                              "backup: scan the descriptor as text")
    parent = m.get(8, 0)
    if not isinstance(parent, int) or isinstance(parent, bool) or not 0 <= parent < 2 ** 32:
        raise DescriptorError("crypto-output: bad parent fingerprint")
    if depth is None:
        depth = len(origin)
    child_number = (origin[-1][0] | (H if origin[-1][1] else 0)) if origin else 0
    version = bytes.fromhex("0488b21e" if network == 0 else "043587cf")
    xpub = crypto_base58check(version + bytes([depth]) + parent.to_bytes(4, "big")
                              + child_number.to_bytes(4, "big") + chain + key)
    text = ""
    if fp is not None:
        text = "[" + fp.hex() + "".join(f"/{i}{'h' if hd else ''}" for i, hd in origin) + "]"
    text += xpub
    if 7 in m:
        children, _fp, _d = _keypath(m[7])
        text += "".join("/" + ("*" if i is None else str(i)) + ("h" if hd else "") for i, hd in children)
    else:
        text += "/<0;1>/*"
    return text


def crypto_base58check(payload: bytes) -> str:
    from nonceverify.psbt import base58check
    return base58check(payload)


def descriptor_from_crypto_output(cbor: bytes) -> Descriptor:
    item = cbor_read(cbor)
    wrappers = []
    while isinstance(item, Tag) and item.tag in (TAG__SH, TAG__WSH, TAG__PKH, TAG__WPKH):
        wrappers.append({TAG__SH: "sh", TAG__WSH: "wsh", TAG__PKH: "pkh", TAG__WPKH: "wpkh"}[item.tag])
        item = item.value
    if isinstance(item, Tag) and item.tag in (TAG__MULTI, TAG__SORTEDMULTI):
        m = item.value
        if not isinstance(m, dict) or not isinstance(m.get(1), int) or not isinstance(m.get(2), list):
            raise DescriptorError("crypto-output: bad multisig")
        name = "sortedmulti" if item.tag == TAG__SORTEDMULTI else "multi"
        if not 1 <= len(m[2]) <= MAX_KEYS:
            raise DescriptorError("crypto-output: wrong number of keys")
        inner = f"{name}({m[1]}," + ",".join(_ur_key(k) for k in m[2]) + ")"
    else:
        inner = _ur_key(item)
    text = inner
    for w in reversed(wrappers):
        text = f"{w}({text})"
    desc = _parse_expression(text)
    desc.source = "ur"
    desc.checksum = None
    return _finish(desc)


# ---------------------------------------------------------------------------------------
# Is the loaded seed one of the descriptor's keys?
# ---------------------------------------------------------------------------------------
STANDARD_ORIGINS = {
    TYPE__WSH: lambda c, a: [48 | H, c | H, a | H, 2 | H],
    TYPE__SH_WSH: lambda c, a: [48 | H, c | H, a | H, 1 | H],
    TYPE__SH: lambda c, a: [45 | H] if a == 0 else [48 | H, c | H, a | H, 0 | H],
    TYPE__WPKH: lambda c, a: [84 | H, c | H, a | H],
    TYPE__SH_WPKH: lambda c, a: [49 | H, c | H, a | H],
    TYPE__PKH: lambda c, a: [44 | H, c | H, a | H],
}


@dataclass
class SeedCheck:
    ours: dict = field(default_factory=dict)        # key index -> path from the master
    problems: list = field(default_factory=list)    # reasons for red


def check_seed_in_descriptor(desc: Descriptor, master: crypto.HDKey) -> SeedCheck:
    """
    By derivation only: the key at its origin path must equal what this seed derives
    there. A key labelled with this seed's fingerprint that doesn't match is a red flag
    (an altered descriptor); a descriptor without any key of this seed is red too.
    """
    result = SeedCheck()
    fp = master.fingerprint
    coin = 1 if desc.network == "test" else 0
    for i, key in enumerate(desc.keys):
        candidates = [key.origin_path] if key.origin_path else \
            [STANDARD_ORIGINS[desc.type](coin, a) for a in range(10)]
        for path in candidates:
            node = master.derive(path)
            try:
                if node.pubkey == key.pubkey and node.chain == key.chain:
                    result.ours[i] = list(path)
                    break
            finally:
                node.wipe()
        if i not in result.ours and key.origin_fingerprint == fp:
            result.problems.append(f"key #{i + 1} carries this seed's fingerprint {fp.hex()} but is NOT "
                                   "this seed's key at that path: the descriptor has been altered")
    if not result.ours:
        result.problems.append("none of the keys is this seed's: wrong seed or passphrase, or not this wallet")
    return result


# ---------------------------------------------------------------------------------------
# Search an address in a descriptor
# ---------------------------------------------------------------------------------------
def search_descriptor(desc: Descriptor, addr: Address, gap: int = GAP_ACCOUNT0, progress=None) -> Match:
    if addr.network != desc.network:
        raise AddressError("the address and the wallet are on different networks")
    if addr.kind != desc.address_kind():
        raise AddressError(f"this {addr.kind.upper()} address can't belong to a {desc.describe()} wallet")
    branches = desc.branch_count()
    wildcard = desc.keys[0].wildcard
    per_branch = gap if wildcard else 1
    total = branches * per_branch
    done = 0
    for b in range(branches):
        nodes = []
        for key in desc.keys:
            point, chain = key.point, key.chain
            for step in key.branches[b]:
                point, chain = ckd_pub(point, chain, step)
            nodes.append((point, chain))
        if not wildcard:
            spk = desc.script_pubkey(b, [crypto.pubkey_compressed(p) for p, _c in nodes])
            done += 1
            if spk == addr.script_pubkey:
                return Match(list(desc.keys[0].branches[b]), _is_change(desc, b), None)
            continue
        for index in range(gap):
            if progress and index % 5 == 0:
                progress(done, total)
            done += 1
            pubs = [crypto.pubkey_compressed(ckd_pub(p, c, index)[0]) for p, c in nodes]
            if desc.script_pubkey(b, pubs) == addr.script_pubkey:
                return Match(list(desc.keys[0].branches[b]) + [index], _is_change(desc, b), index)
    return None


def _is_change(desc: Descriptor, branch: int) -> bool:
    steps = desc.keys[0].branches[branch]
    return bool(steps) and steps[-1] == 1 and desc.branch_count() > 1


def format_steps(steps) -> str:
    return "/".join(str(s) for s in steps)
