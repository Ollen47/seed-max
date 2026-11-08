"""
Minimal PSBT (BIP174, version 0) and transaction parsing, BIP143 sighash and address
encoding, for the nonce verifier. Written from scratch, standard library only.

Only what the verification needs is interpreted; unknown PSBT fields are skipped.
"""
from dataclasses import dataclass, field

from nonceverify import crypto


class PSBTError(ValueError):
    pass


class Reader:
    def __init__(self, b: bytes):
        self.b, self.i = b, 0

    def take(self, n: int) -> bytes:
        out = self.b[self.i:self.i + n]
        if len(out) != n:
            raise PSBTError("truncated data")
        self.i += n
        return out

    def varint(self) -> int:
        x = self.take(1)[0]
        if x < 0xFD:
            return x
        return int.from_bytes(self.take({0xFD: 2, 0xFE: 4, 0xFF: 8}[x]), "little")

    def varbytes(self) -> bytes:
        return self.take(self.varint())

    @property
    def done(self) -> bool:
        return self.i >= len(self.b)


def ser_varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xFFFFFFFF:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


def ser_varbytes(b: bytes) -> bytes:
    return ser_varint(len(b)) + b


# ---------------------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------------------
@dataclass
class TxIn:
    prevout: bytes          # txid (internal byte order) + vout, 36 bytes
    script_sig: bytes
    sequence: bytes         # 4 bytes
    witness: list = field(default_factory=list)

    @property
    def txid_hex(self) -> str:
        return self.prevout[:32][::-1].hex()

    @property
    def vout(self) -> int:
        return int.from_bytes(self.prevout[32:], "little")


@dataclass
class TxOut:
    value: int
    script_pubkey: bytes

    def serialize(self) -> bytes:
        return self.value.to_bytes(8, "little") + ser_varbytes(self.script_pubkey)


@dataclass
class Tx:
    version: bytes
    inputs: list
    outputs: list
    locktime: bytes

    def outputs_serialized(self) -> bytes:
        return b"".join(o.serialize() for o in self.outputs)

    def same_transaction(self, other: "Tx") -> bool:
        return (self.version == other.version and self.locktime == other.locktime
                and [i.prevout for i in self.inputs] == [i.prevout for i in other.inputs]
                and [i.sequence for i in self.inputs] == [i.sequence for i in other.inputs]
                and self.outputs_serialized() == other.outputs_serialized())


def parse_tx(raw: bytes) -> Tx:
    rd = Reader(raw)
    version = rd.take(4)
    segwit = raw[4:6] == b"\x00\x01"
    if segwit:
        rd.take(2)
    inputs = [TxIn(rd.take(36), rd.varbytes(), rd.take(4)) for _ in range(rd.varint())]
    outputs = [TxOut(int.from_bytes(rd.take(8), "little"), rd.varbytes()) for _ in range(rd.varint())]
    if segwit:
        for txin in inputs:
            txin.witness = [rd.varbytes() for _ in range(rd.varint())]
    locktime = rd.take(4)
    if not rd.done:
        raise PSBTError("extra bytes after the transaction")
    return Tx(version, inputs, outputs, locktime)


# ---------------------------------------------------------------------------------------
# PSBT v0
# ---------------------------------------------------------------------------------------
@dataclass
class PSBTInput:
    witness_utxo: TxOut = None
    non_witness_utxo: bytes = None
    partial_sigs: dict = field(default_factory=dict)        # pubkey -> sig + sighash byte
    sighash_type: int = None
    redeem_script: bytes = None
    witness_script: bytes = None
    bip32: dict = field(default_factory=dict)               # pubkey -> (fingerprint, path)
    final_script_sig: bytes = None
    final_witness: list = None
    taproot: bool = False                                    # any BIP371 field present
    tap_sighashes: list = field(default_factory=list)        # hashtype of each BIP371 signature (0 = DEFAULT)


@dataclass
class PSBTOutput:
    redeem_script: bytes = None
    witness_script: bytes = None
    bip32: dict = field(default_factory=dict)
    taproot_bip32: dict = field(default_factory=dict)       # x-only -> (fingerprint, path)


@dataclass
class PSBT:
    tx: Tx
    inputs: list
    outputs: list
    xpubs: dict


PSBT_MAGIC = b"psbt\xff"


MAX_PATH_DEPTH = 12                  # real wallets use 3 to 7 levels


def _parse_bip32_value(v: bytes):
    if len(v) < 4 or (len(v) - 4) % 4 or (len(v) - 4) // 4 > MAX_PATH_DEPTH:
        raise PSBTError("invalid BIP32 derivation")
    path = [int.from_bytes(v[i:i + 4], "little") for i in range(4, len(v), 4)]
    return (v[:4], path)


# Expected key lengths of the fields this parser interprets (BIP174): a field with any
# other key length, or any key seen twice in one map, makes the PSBT invalid, as in
# Bitcoin Core. Otherwise two parsers could read two different values from one PSBT.
_GLOBAL_KEYLEN = {0x00: (1,), 0x01: (79,), 0xFB: (1,)}
_INPUT_KEYLEN = {0x00: (1,), 0x01: (1,), 0x02: (34, 66), 0x03: (1,), 0x04: (1,), 0x05: (1,),
                 0x06: (34, 66), 0x07: (1,), 0x08: (1,), 0x13: (1,), 0x14: (65,),
                 0x15: tuple(34 + 32 * k for k in range(129)),     # 1 + control block 33 + 32m
                 0x16: (33,), 0x17: (1,), 0x18: (1,)}
_OUTPUT_KEYLEN = {0x00: (1,), 0x01: (1,), 0x02: (34, 66), 0x05: (1,), 0x06: (1,), 0x07: (33,)}


def parse_psbt(raw: bytes) -> PSBT:
    if raw[:5] != PSBT_MAGIC:
        raise PSBTError("not a PSBT")
    rd = Reader(raw)
    rd.take(5)

    def read_map(expected):
        entries = []
        seen = set()
        while True:
            klen = rd.varint()
            if klen == 0:
                return entries
            key = rd.take(klen)
            value = rd.varbytes()
            if key in seen:
                raise PSBTError("duplicate key in the PSBT")
            seen.add(key)
            lengths = expected.get(key[0])
            if lengths is not None and len(key) not in lengths:
                raise PSBTError(f"field 0x{key[0]:02x} with a key of the wrong length")
            entries.append((key, value))

    glob = read_map(_GLOBAL_KEYLEN)
    unsigned = None
    xpubs = {}
    for k, v in glob:
        if k == b"\x00":
            unsigned = v
        elif k[0] == 0x01:
            xpubs[k[1:]] = v
        elif k[0] == 0xFB and int.from_bytes(v, "little") != 0:
            raise PSBTError("PSBT version 2 not supported")
    if unsigned is None:
        raise PSBTError("PSBT version 2 not supported")
    tx = parse_tx(unsigned)
    if any(i.script_sig or i.witness for i in tx.inputs):
        raise PSBTError("the unsigned transaction contains signatures")

    inputs = []
    for _ in tx.inputs:
        inp = PSBTInput()
        for k, v in read_map(_INPUT_KEYLEN):
            t = k[0]
            if t == 0x00:
                inp.non_witness_utxo = v
            elif t == 0x01:
                r2 = Reader(v)
                inp.witness_utxo = TxOut(int.from_bytes(r2.take(8), "little"), r2.varbytes())
                if not r2.done:
                    raise PSBTError("invalid witness_utxo")
            elif t == 0x02:
                inp.partial_sigs[k[1:]] = v
            elif t == 0x03:
                inp.sighash_type = int.from_bytes(v, "little")
            elif t == 0x04:
                inp.redeem_script = v
            elif t == 0x05:
                inp.witness_script = v
            elif t == 0x06:
                inp.bip32[k[1:]] = _parse_bip32_value(v)
            elif t == 0x07:
                inp.final_script_sig = v
            elif t == 0x08:
                r2 = Reader(v)
                inp.final_witness = [r2.varbytes() for _ in range(r2.varint())]
                if not r2.done:
                    raise PSBTError("invalid final witness")
            elif 0x13 <= t <= 0x18:
                inp.taproot = True
                if t in (0x13, 0x14):
                    # tap key sig / tap script sig: 64 bytes (SIGHASH_DEFAULT) or 65 (+ hashtype)
                    # (a 65-byte signature with hashtype 0x00 is invalid, BIP341)
                    inp.tap_sighashes.append(0 if len(v) == 64 else v[64] if len(v) == 65 and v[64] else -1)
        inputs.append(inp)

    outputs = []
    for _ in tx.outputs:
        out = PSBTOutput()
        for k, v in read_map(_OUTPUT_KEYLEN):
            if k[0] == 0x00:
                out.redeem_script = v
            elif k[0] == 0x01:
                out.witness_script = v
            elif k[0] == 0x02:
                out.bip32[k[1:]] = _parse_bip32_value(v)
            elif k[0] == 0x07:
                # tap_bip32_derivation: leaf hashes, then fingerprint + path
                r2 = Reader(v)
                for _ in range(r2.varint()):
                    r2.take(32)
                out.taproot_bip32[k[1:]] = _parse_bip32_value(v[r2.i:])
        outputs.append(out)
    if not rd.done:
        raise PSBTError("extra bytes after the PSBT")
    return PSBT(tx, inputs, outputs, xpubs)


# ---------------------------------------------------------------------------------------
# Spent output and signature hash (BIP143)
# ---------------------------------------------------------------------------------------
def spent_output(psbt: PSBT, index: int) -> TxOut:
    """ The output being spent by input `index`, from witness_utxo or non_witness_utxo """
    inp = psbt.inputs[index]
    txin = psbt.tx.inputs[index]
    if inp.non_witness_utxo is not None:
        prev = parse_tx(inp.non_witness_utxo)
        if crypto.dsha256(_serialize_no_witness(prev)) != txin.prevout[:32]:
            raise PSBTError("non_witness_utxo does not match the input")
        if txin.vout >= len(prev.outputs):
            raise PSBTError("non_witness_utxo without that output")
        utxo = prev.outputs[txin.vout]
        if inp.witness_utxo is not None and (inp.witness_utxo.value, inp.witness_utxo.script_pubkey) != (utxo.value, utxo.script_pubkey):
            raise PSBTError("witness_utxo and non_witness_utxo disagree")
        return utxo
    if inp.witness_utxo is None:
        raise PSBTError("the PSBT does not say which coin this input spends")
    return inp.witness_utxo


def _serialize_no_witness(tx: Tx) -> bytes:
    return (tx.version + ser_varint(len(tx.inputs))
            + b"".join(i.prevout + ser_varbytes(i.script_sig) + i.sequence for i in tx.inputs)
            + ser_varint(len(tx.outputs)) + tx.outputs_serialized() + tx.locktime)


def p2pkh_script(pubkey: bytes) -> bytes:
    return b"\x76\xa9\x14" + crypto.hash160(pubkey) + b"\x88\xac"


def bip143_sighash(tx: Tx, index: int, script_code: bytes, amount: int, hashtype: int) -> bytes:
    base = hashtype & 0x1F
    anyone = hashtype & 0x80
    zero = b"\x00" * 32
    hash_prevouts = zero if anyone else crypto.dsha256(b"".join(i.prevout for i in tx.inputs))
    hash_sequence = zero if (anyone or base in (2, 3)) else crypto.dsha256(b"".join(i.sequence for i in tx.inputs))
    if base not in (2, 3):
        hash_outputs = crypto.dsha256(tx.outputs_serialized())
    elif base == 3 and index < len(tx.outputs):
        hash_outputs = crypto.dsha256(tx.outputs[index].serialize())
    else:
        hash_outputs = zero
    txin = tx.inputs[index]
    preimage = (tx.version + hash_prevouts + hash_sequence + txin.prevout + ser_varbytes(script_code)
                + amount.to_bytes(8, "little") + txin.sequence + hash_outputs + tx.locktime
                + hashtype.to_bytes(4, "little"))
    return crypto.dsha256(preimage)


# ---------------------------------------------------------------------------------------
# Script types and addresses (display only)
# ---------------------------------------------------------------------------------------
SCRIPT__P2WPKH = "P2WPKH"
SCRIPT__P2WSH = "P2WSH"
SCRIPT__P2SH = "P2SH"
SCRIPT__P2PKH = "P2PKH"
SCRIPT__P2TR = "P2TR"
SCRIPT__OTHER = "other"


def script_type(spk: bytes) -> str:
    if len(spk) == 22 and spk[:2] == b"\x00\x14":
        return SCRIPT__P2WPKH
    if len(spk) == 34 and spk[:2] == b"\x00\x20":
        return SCRIPT__P2WSH
    if len(spk) == 34 and spk[:2] == b"\x51\x20":
        return SCRIPT__P2TR
    if len(spk) == 23 and spk[0] == 0xA9 and spk[1] == 0x14 and spk[22] == 0x87:
        return SCRIPT__P2SH
    if len(spk) == 25 and spk[:3] == b"\x76\xa9\x14" and spk[23:] == b"\x88\xac":
        return SCRIPT__P2PKH
    return SCRIPT__OTHER


_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def _convertbits(data, frombits, tobits):
    acc, bits, ret = 0, 0, []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def segwit_address(hrp: str, version: int, program: bytes) -> str:
    data = [version] + _convertbits(program, 8, 5)
    const = 1 if version == 0 else 0x2BC830A3
    hrp_exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    polymod = _bech32_polymod(hrp_exp + data + [0] * 6) ^ const
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32[d] for d in data + checksum)


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def base58check(payload: bytes) -> str:
    data = payload + crypto.dsha256(payload)[:4]
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = _B58[rem] + out
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out


def network_of(psbt: PSBT) -> str:
    """ "main" or "test", from the xpub versions or BIP32 coin types (default main) """
    for key in psbt.xpubs:
        if key[:4] == bytes.fromhex("043587cf"):
            return "test"
        if key[:4] == bytes.fromhex("0488b21e"):
            return "main"
    for inp in psbt.inputs:
        for _fp, path in inp.bip32.values():
            if len(path) >= 2 and path[0] & 0x80000000:
                return "test" if path[1] == 0x80000001 else "main"
    return "main"


def address(spk: bytes, network: str = "main") -> str:
    hrp = "bc" if network == "main" else "tb"
    kind = script_type(spk)
    if kind in (SCRIPT__P2WPKH, SCRIPT__P2WSH):
        return segwit_address(hrp, 0, spk[2:])
    if kind == SCRIPT__P2TR:
        return segwit_address(hrp, 1, spk[2:])
    if kind == SCRIPT__P2SH:
        return base58check(bytes([0x05 if network == "main" else 0xC4]) + spk[2:22])
    if kind == SCRIPT__P2PKH:
        return base58check(bytes([0x00 if network == "main" else 0x6F]) + spk[3:23])
    if spk[:1] == b"\x6a":
        return "OP_RETURN " + spk[1:].hex()
    return "script " + spk.hex()


# ---------------------------------------------------------------------------------------
# Script helpers for the verifier
# ---------------------------------------------------------------------------------------
def p2wpkh_script(pubkey: bytes) -> bytes:
    return b"\x00\x14" + crypto.hash160(pubkey)


def push(data: bytes) -> bytes:
    """ Minimal push of up to 520 bytes (redeem scripts in a scriptSig) """
    n = len(data)
    if n < 0x4C:
        return bytes([n]) + data
    if n <= 0xFF:
        return b"\x4c" + bytes([n]) + data
    return b"\x4d" + n.to_bytes(2, "little") + data


def parse_multisig(ws: bytes):
    """ (m, [pubkeys]) for a standard OP_m <keys> OP_n OP_CHECKMULTISIG script, else None """
    if len(ws) < 3 or ws[-1] != 0xAE:
        return None
    m, n = ws[0] - 0x50, ws[-2] - 0x50
    if not (1 <= m <= n <= 16):
        return None
    keys, i = [], 1
    while i < len(ws) - 2:
        length = ws[i]
        if length not in (33, 65) or i + 1 + length > len(ws) - 2:
            return None
        keys.append(ws[i + 1:i + 1 + length])
        i += 1 + length
    if i != len(ws) - 2 or len(keys) != n:
        return None
    return m, keys
