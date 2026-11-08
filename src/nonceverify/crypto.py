"""
Cryptography of the nonce verifier, written from scratch on the Python standard library.

Deliberately independent of the signer: no embit, no libsecp256k1, no SeedSigner code.
Everything here is plain integer arithmetic and hashlib/hmac, short enough to read in
one sitting:

- secp256k1 in Jacobian coordinates, with a precomputed table for multiples of G;
- RFC 6979 deterministic nonces exactly as libsecp256k1 derives them (optional 32-byte
  extra data, which is how Bitcoin Core, Electrum, embit and Coldcard grind for low R);
- BIP39 seed (PBKDF2-HMAC-SHA512) and BIP32 private derivation;
- ECDSA verification, DER parsing, hash160 (with a pure-Python RIPEMD-160 for OpenSSL
  builds that dropped it).
"""
import hashlib
import hmac
import unicodedata

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
G = (GX, GY)


# ---------------------------------------------------------------------------------------
# secp256k1 (y^2 = x^3 + 7), Jacobian coordinates (X, Y, Z): x = X/Z^2, y = Y/Z^3
# ---------------------------------------------------------------------------------------
_INF = (0, 1, 0)


def _jdouble(p):
    X, Y, Z = p
    if Z == 0 or Y == 0:
        return _INF
    YY = Y * Y % P
    S = 4 * X * YY % P
    M = 3 * X * X % P
    X3 = (M * M - 2 * S) % P
    Y3 = (M * (S - X3) - 8 * YY * YY) % P
    Z3 = 2 * Y * Z % P
    return (X3, Y3, Z3)


def _jadd(p, q):
    if p[2] == 0:
        return q
    if q[2] == 0:
        return p
    X1, Y1, Z1 = p
    X2, Y2, Z2 = q
    Z1Z1 = Z1 * Z1 % P
    Z2Z2 = Z2 * Z2 % P
    U1 = X1 * Z2Z2 % P
    U2 = X2 * Z1Z1 % P
    S1 = Y1 * Z2 * Z2Z2 % P
    S2 = Y2 * Z1 * Z1Z1 % P
    if U1 == U2:
        if S1 != S2:
            return _INF
        return _jdouble(p)
    H = (U2 - U1) % P
    R = (S2 - S1) % P
    HH = H * H % P
    HHH = H * HH % P
    V = U1 * HH % P
    X3 = (R * R - HHH - 2 * V) % P
    Y3 = (R * (V - X3) - S1 * HHH) % P
    Z3 = H * Z1 * Z2 % P
    return (X3, Y3, Z3)


def _to_affine(p):
    if p[2] == 0:
        return None
    zi = pow(p[2], -1, P)
    zi2 = zi * zi % P
    return (p[0] * zi2 % P, p[1] * zi2 * zi % P)


# Fixed-base table: _GTABLE[i][d] = d * 16^i * G (affine, as Jacobian with Z = 1), for
# 64 windows of 4 bits. A multiplication by G is then 64 additions, no doublings.
_GTABLE = None


def _gtable():
    global _GTABLE
    if _GTABLE is None:
        table = []
        base = (GX, GY, 1)
        for _ in range(64):
            row = [_INF]
            acc = _INF
            for _d in range(1, 16):
                acc = _jadd(acc, base)
                row.append(acc)
            # normalize the row to Z = 1 (cheaper additions later)
            row = [_INF] + [(a[0], a[1], 1) for a in (_to_affine(x) for x in row[1:])]
            table.append(row)
            for _k in range(4):
                base = _jdouble(base)
            a = _to_affine(base)
            base = (a[0], a[1], 1)
        _GTABLE = table
    return _GTABLE


def point_mul_g(k: int):
    """ k * G, affine (x, y), or None for the point at infinity """
    k %= N
    table = _gtable()
    acc = _INF
    i = 0
    while k:
        d = k & 15
        if d:
            acc = _jadd(acc, table[i][d])
        k >>= 4
        i += 1
    return _to_affine(acc)


def point_mul(k: int, point):
    """ k * point for any affine point (4-bit window, left to right) """
    k %= N
    if k == 0 or point is None:
        return None
    base = (point[0], point[1], 1)
    window = [_INF, base]
    for _ in range(14):
        window.append(_jadd(window[-1], base))
    acc = _INF
    for shift in range(252, -1, -4):
        if acc[2]:
            for _ in range(4):
                acc = _jdouble(acc)
        d = (k >> shift) & 15
        if d:
            acc = _jadd(acc, window[d])
    return _to_affine(acc)


def point_add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    return _to_affine(_jadd((p[0], p[1], 1), (q[0], q[1], 1)))


def pubkey_compressed(point) -> bytes:
    return bytes([2 + (point[1] & 1)]) + point[0].to_bytes(32, "big")


def pubkey_decode(b: bytes):
    if len(b) == 33 and b[0] in (2, 3):
        x = int.from_bytes(b[1:], "big")
        if x >= P:
            raise ValueError("invalid public key")
        y = pow((x * x * x + 7) % P, (P + 1) // 4, P)
        if (y * y - x * x * x - 7) % P:
            raise ValueError("public key not on the curve")
        if y & 1 != b[0] & 1:
            y = P - y
        return (x, y)
    if len(b) == 65 and b[0] == 4:
        x, y = int.from_bytes(b[1:33], "big"), int.from_bytes(b[33:], "big")
        if (y * y - x * x * x - 7) % P:
            raise ValueError("public key not on the curve")
        return (x, y)
    raise ValueError("invalid public key")


def privkey_to_pubkey(priv: int) -> bytes:
    return pubkey_compressed(point_mul_g(priv))


def ecdsa_verify(pubkey_point, z: int, r: int, s: int) -> bool:
    if not (1 <= r < N and 1 <= s < N):
        return False
    w = pow(s, -1, N)
    pt = point_add(point_mul_g(z * w % N), point_mul(r * w % N, pubkey_point))
    return pt is not None and pt[0] % N == r


# ---------------------------------------------------------------------------------------
# Hashes
# ---------------------------------------------------------------------------------------
def sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def dsha256(b: bytes) -> bytes:
    return sha256(sha256(b))


def ripemd160_pure(msg: bytes) -> bytes:
    """ Reference RIPEMD-160, used only if OpenSSL doesn't provide it """
    def rol(x, n):
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF
    f = [lambda x, y, z: x ^ y ^ z,
         lambda x, y, z: (x & y) | (~x & z),
         lambda x, y, z: (x | ~y) ^ z,
         lambda x, y, z: (x & z) | (y & ~z),
         lambda x, y, z: x ^ (y | ~z)]
    KL = [0, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E]
    KR = [0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0]
    RL = [list(range(16)),
          [7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8],
          [3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12],
          [1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2],
          [4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13]]
    RR = [[5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12],
          [6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2],
          [15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13],
          [8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14],
          [12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11]]
    SL = [[11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8],
          [7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12],
          [11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5],
          [11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12],
          [9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6]]
    SR = [[8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6],
          [9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11],
          [9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5],
          [15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8],
          [8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11]]
    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    ml = len(msg) * 8
    msg = msg + b"\x80" + b"\x00" * ((55 - len(msg)) % 64) + ml.to_bytes(8, "little")
    for i in range(0, len(msg), 64):
        X = [int.from_bytes(msg[i + 4 * j:i + 4 * j + 4], "little") for j in range(16)]
        al, bl, cl, dl, el = h
        ar, br, cr, dr, er = h
        for rnd in range(5):
            for j in range(16):
                t = rol((al + (f[rnd](bl, cl, dl) & 0xFFFFFFFF) + X[RL[rnd][j]] + KL[rnd]) & 0xFFFFFFFF, SL[rnd][j]) + el
                al, el, dl, cl, bl = el, dl, rol(cl, 10), bl, t & 0xFFFFFFFF
                t = rol((ar + (f[4 - rnd](br, cr, dr) & 0xFFFFFFFF) + X[RR[rnd][j]] + KR[rnd]) & 0xFFFFFFFF, SR[rnd][j]) + er
                ar, er, dr, cr, br = er, dr, rol(cr, 10), br, t & 0xFFFFFFFF
        t = (h[1] + cl + dr) & 0xFFFFFFFF
        h[1] = (h[2] + dl + er) & 0xFFFFFFFF
        h[2] = (h[3] + el + ar) & 0xFFFFFFFF
        h[3] = (h[4] + al + br) & 0xFFFFFFFF
        h[4] = (h[0] + bl + cr) & 0xFFFFFFFF
        h[0] = t
    return b"".join(x.to_bytes(4, "little") for x in h)


def ripemd160(b: bytes) -> bytes:
    try:
        return hashlib.new("ripemd160", b).digest()
    except ValueError:
        return ripemd160_pure(b)


def hash160(b: bytes) -> bytes:
    return ripemd160(sha256(b))


# ---------------------------------------------------------------------------------------
# RFC 6979, as libsecp256k1's nonce_function_rfc6979
# ---------------------------------------------------------------------------------------
def rfc6979_k(priv: int, msg32: bytes, extra: bytes = b"") -> int:
    x = priv.to_bytes(32, "big")
    h1 = (int.from_bytes(msg32, "big") % N).to_bytes(32, "big")
    V, K = b"\x01" * 32, b"\x00" * 32
    K = hmac.new(K, V + b"\x00" + x + h1 + extra, hashlib.sha256).digest()
    V = hmac.new(K, V, hashlib.sha256).digest()
    K = hmac.new(K, V + b"\x01" + x + h1 + extra, hashlib.sha256).digest()
    V = hmac.new(K, V, hashlib.sha256).digest()
    while True:
        V = hmac.new(K, V, hashlib.sha256).digest()
        k = int.from_bytes(V, "big")
        if 1 <= k < N:
            return k
        K = hmac.new(K, V + b"\x00", hashlib.sha256).digest()
        V = hmac.new(K, V, hashlib.sha256).digest()


# Low-R grinding retries with a counter in the extra data: counter n succeeds with
# probability 1/2 each time, so needing more than 64 retries has odds of 2^-64.
MAX_GRIND_COUNTER = 64


def find_deterministic_nonce(priv: int, z32: bytes, r: int):
    """
    Which deterministic nonce produced r: 0 = plain RFC 6979, n >= 1 = low-R grinding
    with counter n (little-endian in the first 4 bytes of the 32-byte extra data, as in
    Bitcoin Core, libsecp256k1 users, embit and Coldcard). None if no candidate matches.
    """
    for counter in range(0, MAX_GRIND_COUNTER + 1):
        extra = b"" if counter == 0 else counter.to_bytes(4, "little") + b"\x00" * 28
        k = rfc6979_k(priv, z32, extra)
        if point_mul_g(k)[0] % N == r:
            return counter
    return None


# ---------------------------------------------------------------------------------------
# BIP39 seed, BIP32 private derivation
# ---------------------------------------------------------------------------------------
def bip39_seed(mnemonic: str, passphrase: str = "") -> bytes:
    m = unicodedata.normalize("NFKD", " ".join(mnemonic.split()))
    p = unicodedata.normalize("NFKD", "mnemonic" + passphrase)
    return hashlib.pbkdf2_hmac("sha512", m.encode(), p.encode(), 2048)


class HDKey:
    """ BIP32 private node: secret exponent + chain code """
    def __init__(self, priv: int, chain: bytes):
        if not 1 <= priv < N:
            raise ValueError("invalid BIP32 key")
        self.priv = priv
        self.chain = chain
        self._pub = None

    @classmethod
    def from_seed(cls, seed: bytes) -> "HDKey":
        I = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
        return cls(int.from_bytes(I[:32], "big"), I[32:])

    @property
    def pubkey(self) -> bytes:
        if self._pub is None:
            self._pub = privkey_to_pubkey(self.priv)
        return self._pub

    @property
    def fingerprint(self) -> bytes:
        return hash160(self.pubkey)[:4]

    def child(self, index: int) -> "HDKey":
        if index & 0x80000000:
            data = b"\x00" + self.priv.to_bytes(32, "big") + index.to_bytes(4, "big")
        else:
            data = self.pubkey + index.to_bytes(4, "big")
        I = hmac.new(self.chain, data, hashlib.sha512).digest()
        tweak = int.from_bytes(I[:32], "big")
        if tweak >= N:
            raise ValueError("invalid BIP32 child")
        return HDKey((tweak + self.priv) % N, I[32:])

    def derive(self, path) -> "HDKey":
        node = self
        for index in path:
            node = node.child(index)
        return node

    def wipe(self):
        self.priv = 0
        self.chain = b""
        self._pub = None


def format_path(path) -> str:
    return "m/" + "/".join(f"{i & 0x7FFFFFFF}'" if i & 0x80000000 else str(i) for i in path)


# ---------------------------------------------------------------------------------------
# DER signatures
# ---------------------------------------------------------------------------------------
def der_decode(sig: bytes):
    """ (r, s) from a strict DER ECDSA signature (without the sighash byte) """
    if len(sig) < 8 or sig[0] != 0x30 or sig[1] != len(sig) - 2:
        raise ValueError("not a DER signature")
    i = 2
    if sig[i] != 0x02:
        raise ValueError("DER: r missing")
    lr = sig[i + 1]
    r = int.from_bytes(sig[i + 2:i + 2 + lr], "big")
    i += 2 + lr
    if i >= len(sig) or sig[i] != 0x02:
        raise ValueError("DER: s missing")
    ls = sig[i + 1]
    s = int.from_bytes(sig[i + 2:i + 2 + ls], "big")
    if i + 2 + ls != len(sig):
        raise ValueError("DER: trailing bytes")
    return r, s


def is_strict_der_signature(sig: bytes) -> bool:
    """
    Bitcoin Core's IsValidSignatureEncoding (BIP66), on a signature WITH its sighash
    byte. Strict DER is a consensus rule, and every non-canonical form (padding, negative
    integers) would also be a free choice for the signer.
    """
    n = len(sig)
    if n < 9 or n > 73 or sig[0] != 0x30 or sig[1] != n - 3:
        return False
    len_r = sig[3]
    if 5 + len_r >= n:
        return False
    len_s = sig[5 + len_r]
    if len_r + len_s + 7 != n or sig[2] != 0x02 or len_r == 0 or sig[4] & 0x80:
        return False
    if len_r > 1 and sig[4] == 0x00 and not (sig[5] & 0x80):
        return False
    if sig[len_r + 4] != 0x02 or len_s == 0 or sig[len_r + 6] & 0x80:
        return False
    if len_s > 1 and sig[len_r + 6] == 0x00 and not (sig[len_r + 7] & 0x80):
        return False
    return True


def der_length(r: int, s: int) -> int:
    """ Length of the minimal DER encoding of (r, s), without the sighash byte """
    return 6 + (r.bit_length() + 8) // 8 + (s.bit_length() + 8) // 8


def looks_like_signature(item: bytes) -> bool:
    """ A witness item that is a strict DER signature + sighash byte """
    return is_strict_der_signature(item)
