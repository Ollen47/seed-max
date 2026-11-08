"""
QR transports for a PSBT, decoded from scratch (standard library only):

- UR (BCR-2020-005), "ur:crypto-psbt/...", single part or animated with the fountain
  code (BCR-2020-005 multipart: Xoshiro256**, alias sampler, XOR of fragments), as
  Sparrow and most wallets show it;
- BBQr (Coinkite), "B$...", as the COLDCARD Q shows it: hex, base32 or zlib + base32;
- a single QR with the PSBT in base64.

`PSBTCollector` takes the payload of every QR read by the camera, in any order and with
repeats, and yields the PSBT bytes once complete. Every format is integrity-checked
(UR: CRC32 of the whole message; PSBT: its own structure, parsed afterwards).
"""
import base64
import binascii
import hashlib
import re
import zlib


class TransportError(ValueError):
    pass


FINAL_TX_MESSAGE = ("this QR is a final transaction, without amounts or paths: the signed PSBT is "
                    "needed (Sparrow: File > Show PSBT as QR, before broadcasting)")


def looks_like_final_tx(raw: bytes) -> bool:
    """ A serialized transaction (version 1 or 2) rather than a PSBT """
    return len(raw) > 60 and raw[:4] in (b"\x01\x00\x00\x00", b"\x02\x00\x00\x00")


# Limits: a PSBT for a hand-held signer is small. A malformed or hostile QR must not be
# able to make the Pi Zero allocate or decompress without bound.
MAX_MESSAGE_BYTES = 1_000_000
MAX_UR_PARTS = 5_000
MAX_CBOR_DEPTH = 4


# ---------------------------------------------------------------------------------------
# Bytewords (BCR-2020-012), minimal encoding: first and last letter of each word
# ---------------------------------------------------------------------------------------
_BYTEWORDS = (
    "ableacidalsoapexaquaarchatomauntawayaxisbackbaldbarnbeltbetabiasbluebodybragbrewbulbbuzz"
    "calmcashcatschefcityclawcodecolacookcostcruxcurlcuspcyandarkdatadaysdelidicedietdoordown"
    "drawdropdrumdulldutyeacheasyechoedgeepicevenexamexiteyesfactfairfernfigsfilmfishfizzflap"
    "flewfluxfoxyfreefrogfuelfundgalagamegeargemsgiftgirlglowgoodgraygrimgurugushgyrohalfhang"
    "hardhawkheathelphighhillholyhopehornhutsicedideaidleinchinkyintoirisironitemjadejazzjoin"
    "joltjowljudojugsjumpjunkjurykeepkenokeptkeyskickkilnkingkitekiwiknoblamblavalazyleaf"
    "legsliarlimplionlistlogoloudloveluaulucklungmainmanymathmazememomenumeowmildmintmissmonk"
    "nailnavyneednewsnextnoonnotenumbobeyoboeomitonyxopenovalowlspaidpartpeckplaypluspoempool"
    "posepuffpumapurrquadquizraceramprealredorichroadrockroofrubyruinrunsrustsafesagascarsets"
    "silkskewslotsoapsolosongstubsurfswantacotasktaxitenttiedtimetinytoiltombtoystriptunatwin"
    "uglyundouniturgeuservastveryvetovialvibeviewvisavoidvowswallwandwarmwaspwavewaxywebswhat"
    "whenwhizwolfworkyankyawnyellyogayurtzapszerozestzinczonezoom"
)
_WORDS = [_BYTEWORDS[i:i + 4] for i in range(0, len(_BYTEWORDS), 4)]
_MINIMAL = {w[0] + w[3]: i for i, w in enumerate(_WORDS)}


def bytewords_decode_minimal(text: str) -> bytes:
    text = text.lower()
    if len(text) % 2:
        raise TransportError("bytewords of odd length")
    try:
        data = bytes(_MINIMAL[text[i:i + 2]] for i in range(0, len(text), 2))
    except KeyError:
        raise TransportError("invalid bytewords")
    if len(data) < 5:
        raise TransportError("bytewords too short")
    body, checksum = data[:-4], data[-4:]
    if binascii.crc32(body).to_bytes(4, "big") != checksum:
        raise TransportError("UR checksum wrong")
    return body


# ---------------------------------------------------------------------------------------
# Minimal CBOR: unsigned integers, byte strings, arrays (all a UR part needs)
# ---------------------------------------------------------------------------------------
def _cbor_head(data: bytes, i: int):
    if i >= len(data):
        raise TransportError("CBOR truncated")
    major, info = data[i] >> 5, data[i] & 31
    i += 1
    if info < 24:
        return major, info, i
    size = {24: 1, 25: 2, 26: 4, 27: 8}.get(info)
    if size is None or i + size > len(data):
        raise TransportError("CBOR not supported")
    return major, int.from_bytes(data[i:i + size], "big"), i + size


def cbor_decode(data: bytes, i: int = 0, depth: int = 0):
    """ (value, next index) """
    if depth > MAX_CBOR_DEPTH:
        raise TransportError("CBOR nested too deep")
    major, arg, i = _cbor_head(data, i)
    if major == 0:
        return arg, i
    if major == 2:
        if i + arg > len(data):
            raise TransportError("CBOR truncated")
        return data[i:i + arg], i + arg
    if major == 4:
        if arg > 16:
            raise TransportError("CBOR not supported")
        items = []
        for _ in range(arg):
            item, i = cbor_decode(data, i, depth + 1)
            items.append(item)
        return items, i
    if major == 6:
        # tag: return the tagged item as is
        return cbor_decode(data, i, depth + 1)
    raise TransportError("CBOR not supported")


def cbor_bytes(data: bytes) -> bytes:
    value, end = cbor_decode(data)
    if not isinstance(value, (bytes, bytearray)) or end != len(data):
        raise TransportError("the UR content is not a PSBT")
    return bytes(value)


# ---------------------------------------------------------------------------------------
# Fountain code (BCR-2020-005)
# ---------------------------------------------------------------------------------------
_MASK64 = (1 << 64) - 1


def _rotl(x: int, k: int) -> int:
    return ((x << k) | (x >> (64 - k))) & _MASK64


class Xoshiro256:
    def __init__(self, seed32: bytes):
        self.s = [int.from_bytes(seed32[i * 8:(i + 1) * 8], "big") for i in range(4)]

    def next(self) -> int:
        s = self.s
        result = (_rotl((s[1] * 5) & _MASK64, 7) * 9) & _MASK64
        t = (s[1] << 17) & _MASK64
        s[2] ^= s[0]
        s[3] ^= s[1]
        s[1] ^= s[2]
        s[0] ^= s[3]
        s[2] ^= t
        s[3] = _rotl(s[3], 45)
        return result

    def next_double(self) -> float:
        return self.next() / (_MASK64 + 1)

    def next_int(self, low: int, high: int) -> int:
        return int(self.next_double() * (high - low + 1)) + low


class _AliasSampler:
    """ Walker/Vose alias method, built exactly as the reference implementation """
    def __init__(self, probs):
        n = len(probs)
        total = sum(probs)
        P = [p * n / total for p in probs]
        small, large = [], []
        for i in range(n - 1, -1, -1):
            (small if P[i] < 1 else large).append(i)
        self.probs = [0.0] * n
        self.aliases = [0] * n
        while small and large:
            a = small.pop()
            g = large.pop()
            self.probs[a] = P[a]
            self.aliases[a] = g
            P[g] += P[a] - 1
            (small if P[g] < 1 else large).append(g)
        while large:
            self.probs[large.pop()] = 1.0
        while small:
            self.probs[small.pop()] = 1.0

    def next(self, rng: Xoshiro256) -> int:
        r1 = rng.next_double()
        r2 = rng.next_double()
        i = int(len(self.probs) * r1)
        return i if r2 < self.probs[i] else self.aliases[i]


def choose_fragments(seq_num: int, seq_len: int, checksum: int) -> frozenset:
    if seq_num <= seq_len:
        return frozenset([seq_num - 1])
    seed = hashlib.sha256(seq_num.to_bytes(4, "big") + checksum.to_bytes(4, "big")).digest()
    rng = Xoshiro256(seed)
    degree = _AliasSampler([1.0 / i for i in range(1, seq_len + 1)]).next(rng) + 1
    remaining = list(range(seq_len))
    shuffled = []
    while remaining:
        shuffled.append(remaining.pop(rng.next_int(0, len(remaining) - 1)))
    return frozenset(shuffled[:degree])


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


class FountainDecoder:
    def __init__(self):
        self.params = None          # (seq_len, message_len, checksum, fragment_len)
        self.solved = {}            # index -> fragment
        self.mixed = {}             # frozenset(indexes) -> xor of those fragments
        self.seen = set()
        self.message = None

    def add(self, seq_num: int, seq_len: int, message_len: int, checksum: int, fragment: bytes) -> bool:
        params = (seq_len, message_len, checksum, len(fragment))
        if self.params is None:
            if not (1 <= seq_len <= MAX_UR_PARTS and 1 <= message_len <= MAX_MESSAGE_BYTES) \
                    or len(fragment) * seq_len < message_len or len(fragment) * (seq_len - 1) >= message_len:
                raise TransportError("invalid UR part")
            self.params = params
        elif params != self.params:
            raise TransportError("UR parts of different messages")
        if seq_num < 1 or seq_num in self.seen or self.message is not None:
            return False
        if len(self.seen) > 20 * seq_len + 100:
            raise TransportError("too many UR parts without completing the message")
        self.seen.add(seq_num)
        self._reduce(choose_fragments(seq_num, seq_len, checksum), fragment)
        if len(self.solved) == seq_len:
            message = b"".join(self.solved[i] for i in range(seq_len))[:message_len]
            if binascii.crc32(message) != checksum:
                raise TransportError("UR message checksum wrong")
            self.message = message
        return True

    def _reduce(self, indexes: frozenset, data: bytes):
        queue = [(indexes, data)]
        while queue:
            idx, data = queue.pop()
            for i in [i for i in idx if i in self.solved]:
                data = _xor(data, self.solved[i])
                idx = idx - {i}
            if not idx or idx in self.mixed:
                continue
            if len(idx) == 1:
                (i,) = idx
                self.solved[i] = data
                # every mixed part containing i can now be reduced further
                for other in [m for m in self.mixed if i in m]:
                    queue.append((other, self.mixed.pop(other)))
            else:
                # reduce stored mixed parts that are supersets of this one
                for other in [m for m in self.mixed if idx < m]:
                    queue.append((other - idx, _xor(self.mixed.pop(other), data)))
                self.mixed[idx] = data

    @property
    def progress(self) -> float:
        if self.params is None:
            return 0.0
        if self.message is not None:
            return 1.0
        return min(0.99, len(self.solved) / self.params[0])


# ---------------------------------------------------------------------------------------
# BBQr
# ---------------------------------------------------------------------------------------
_BBQR_RE = re.compile(r"^B\$([2HZ])([A-Z])([0-9A-Z]{2})([0-9A-Z]{2})")


def _bbqr_payload(encoding: str, text: str) -> bytes:
    if encoding == "H":
        return bytes.fromhex(text)
    padded = text + "=" * (-len(text) % 8)
    raw = base64.b32decode(padded)
    if encoding == "Z":
        # Coldcard compresses with a 2^10 window; -15 accepts any. Output capped.
        z = zlib.decompressobj(wbits=-15)
        out = z.decompress(raw, MAX_MESSAGE_BYTES)
        if z.unconsumed_tail or not z.eof or z.unused_data.strip(b"\x00"):
            raise TransportError("compressed BBQr too large or incomplete")
        return out
    return raw


# ---------------------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------------------
class PSBTCollector:
    def __init__(self):
        self.kind = None
        self.ur_type = None
        self.fountain = None
        self.bbqr = None                # (encoding, file type, total, {index: text})
        self.result = None
        self.restarted = False          # a part of another message reset the decoder

    @property
    def complete(self) -> bool:
        return self.result is not None

    @property
    def progress(self) -> float:
        if self.result is not None:
            return 1.0
        if self.kind == "ur" and self.fountain:
            return self.fountain.progress
        if self.kind == "bbqr" and self.bbqr:
            return min(0.99, len(self.bbqr[3]) / self.bbqr[2])
        return 0.0

    def add(self, data) -> bool:
        """ Feed one QR payload; True if it was a (new) piece of a PSBT """
        if isinstance(data, (bytes, bytearray)):
            try:
                text = bytes(data).decode("utf-8").strip()
            except UnicodeDecodeError:
                return False
        else:
            text = data.strip()

        lower = text.lower()
        if lower.startswith("ur:"):
            return self._add_ur(lower)
        m = _BBQR_RE.match(text)
        if m:
            return self._add_bbqr(m, text)
        if len(text) > 120 and re.fullmatch(r"0[12]000000[0-9a-fA-F]+", text):
            raise TransportError(FINAL_TX_MESSAGE)
        if text.startswith("cHNidP"):
            try:
                self.result = base64.b64decode(text, validate=True)
            except binascii.Error:
                raise TransportError("invalid base64 PSBT")
            self.kind = "base64"
            return True
        return False

    def _add_ur(self, text: str) -> bool:
        parts = text[3:].split("/")
        if parts[0] not in ("crypto-psbt", "psbt", "bytes"):
            raise TransportError(f"UR QR of type «{parts[0][:20]}», not a PSBT")
        if self.ur_type is not None and parts[0] != self.ur_type:
            raise TransportError("UR QRs of different types")
        self.ur_type = parts[0]
        self.kind = "ur"
        if len(parts) == 2:
            self.result = cbor_bytes(bytewords_decode_minimal(parts[1]))
            return True
        header = re.match(r"^(\d{1,10})-(\d{1,10})$", parts[1]) if len(parts) == 3 else None
        if header is None:
            raise TransportError("invalid UR QR")
        try:
            part, _ = cbor_decode(bytewords_decode_minimal(parts[2]))
        except TransportError:
            # A damaged frame (misread or bad checksum) is skipped: the fountain code
            # keeps sending parts, another one will carry the same information.
            return False
        if not (isinstance(part, list) and len(part) == 5 and isinstance(part[4], (bytes, bytearray))
                and all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 2 ** 32 for v in part[:4])):
            return False
        seq_num, seq_len, message_len, checksum, fragment = part
        if (seq_num, seq_len) != (int(header.group(1)), int(header.group(2))):
            return False                       # path header and CBOR part disagree
        if self.fountain is not None and self.fountain.params is not None and \
                self.fountain.params[:3] != (seq_len, message_len, checksum):
            # a part of another PSBT (e.g. the unsigned one, still on screen): start over
            self.fountain = None
            self.restarted = True
        if self.fountain is None:
            self.fountain = FountainDecoder()
        added = self.fountain.add(seq_num, seq_len, message_len, checksum, bytes(fragment))
        if self.fountain.message is not None:
            self.result = cbor_bytes(self.fountain.message)
        return added

    def _add_bbqr(self, m, text: str) -> bool:
        encoding, file_type, total, index = m.group(1), m.group(2), int(m.group(3), 36), int(m.group(4), 36)
        if file_type == "T":
            raise TransportError(FINAL_TX_MESSAGE)
        if file_type != "P":
            raise TransportError("this BBQr QR does not contain a PSBT")
        if total < 1 or index >= total:
            raise TransportError("invalid BBQr part")
        self.kind = "bbqr"
        if self.bbqr is not None and self.bbqr[:3] != (encoding, file_type, total):
            self.bbqr = None                 # another file: start over
            self.restarted = True
        if self.bbqr is None:
            self.bbqr = (encoding, file_type, total, {})
        parts = self.bbqr[3]
        if index in parts:
            return False
        parts[index] = text[8:]
        if len(parts) == total:
            try:
                self.result = _bbqr_payload(encoding, "".join(parts[i] for i in range(total)))
            except (ValueError, binascii.Error, zlib.error, TransportError):
                self.bbqr = None             # let a clean pass of the animation complete it
                raise TransportError("BBQr not decodable: starting over")
        return True


# ---------------------------------------------------------------------------------------
# v7: wallet descriptors (for "Check address")
# ---------------------------------------------------------------------------------------
class DescriptorCollector:
    """
    The QR of a wallet descriptor, as wallets print or show it: the descriptor as plain
    text, UR "crypto-output" (single or animated), or BBQr text ("U"). `result` is then
    a nonceverify.address.Descriptor.
    """
    def __init__(self):
        self.fountain = None
        self.bbqr = None
        self.result = None
        self.restarted = False

    @property
    def complete(self) -> bool:
        return self.result is not None

    @property
    def progress(self) -> float:
        if self.result is not None:
            return 1.0
        if self.fountain is not None:
            return self.fountain.progress
        if self.bbqr is not None:
            return min(0.99, len(self.bbqr[2]) / self.bbqr[1])
        return 0.0

    def add(self, data) -> bool:
        from nonceverify import address
        if isinstance(data, (bytes, bytearray)):
            try:
                text = bytes(data).decode("utf-8").strip()
            except UnicodeDecodeError:
                raise TransportError("this QR is not a wallet descriptor")
        else:
            text = data.strip()
        lower = text.lower()
        if lower.startswith("ur:"):
            return self._add_ur(lower)
        m = _BBQR_RE.match(text)
        if m:
            return self._add_bbqr(m, text)
        self.result = address.parse_descriptor(text)
        return True

    def _add_ur(self, text: str) -> bool:
        from nonceverify import address
        parts = text[3:].split("/")
        if parts[0] == "crypto-psbt" or parts[0] == "psbt":
            raise TransportError("this is a PSBT, not a wallet descriptor")
        if parts[0] != "crypto-output":
            raise TransportError(f"UR of type «{parts[0][:20]}»: export the wallet as an output descriptor")
        if len(parts) == 2:
            self.result = address.descriptor_from_crypto_output(bytewords_decode_minimal(parts[1]))
            return True
        header = re.match(r"^(\d{1,10})-(\d{1,10})$", parts[1]) if len(parts) == 3 else None
        if header is None:
            raise TransportError("invalid UR QR")
        try:
            part, _ = cbor_decode(bytewords_decode_minimal(parts[2]))
        except TransportError:
            return False
        if not (isinstance(part, list) and len(part) == 5 and isinstance(part[4], (bytes, bytearray))
                and all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 2 ** 32 for v in part[:4])):
            return False
        seq_num, seq_len, message_len, checksum, fragment = part
        if (seq_num, seq_len) != (int(header.group(1)), int(header.group(2))):
            return False
        if self.fountain is not None and self.fountain.params is not None and \
                self.fountain.params[:3] != (seq_len, message_len, checksum):
            self.fountain = None
            self.restarted = True
        if self.fountain is None:
            self.fountain = FountainDecoder()
        added = self.fountain.add(seq_num, seq_len, message_len, checksum, bytes(fragment))
        if self.fountain.message is not None:
            self.result = address.descriptor_from_crypto_output(self.fountain.message)
        return added

    def _add_bbqr(self, m, text: str) -> bool:
        from nonceverify import address
        encoding, file_type, total, index = m.group(1), m.group(2), int(m.group(3), 36), int(m.group(4), 36)
        if file_type != "U":
            raise TransportError("this BBQr is not a text descriptor")
        if total < 1 or index >= total:
            raise TransportError("invalid BBQr part")
        if self.bbqr is not None and self.bbqr[:2] != (encoding, total):
            self.bbqr = None
            self.restarted = True
        if self.bbqr is None:
            self.bbqr = (encoding, total, {})
        parts = self.bbqr[2]
        if index in parts:
            return False
        parts[index] = text[8:]
        if len(parts) == total:
            try:
                raw = _bbqr_payload(encoding, "".join(parts[i] for i in range(total)))
                descriptor_text = raw.decode("utf-8")
            except (ValueError, binascii.Error, zlib.error, TransportError, UnicodeDecodeError):
                self.bbqr = None
                raise TransportError("BBQr not decodable: starting over")
            self.result = address.parse_descriptor(descriptor_text)
        return True
