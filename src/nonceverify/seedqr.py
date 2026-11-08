"""
SeedQR (standard: 4 digits per word index) and CompactSeedQR (the raw entropy bytes),
as specified by SeedSigner, decoded to a BIP39 mnemonic and checked.

The English BIP39 wordlist ships next to this file and is checked against the SHA-256
of the official list before use.
"""
import hashlib
import os

WORDLIST_SHA256 = "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"

_WORDLIST = None


class SeedQRError(ValueError):
    pass


def wordlist() -> list:
    global _WORDLIST
    if _WORDLIST is None:
        path = os.path.join(os.path.dirname(__file__), "bip39_english.txt")
        with open(path, "rb") as f:
            raw = f.read()
        if hashlib.sha256(raw).hexdigest() != WORDLIST_SHA256:
            raise SeedQRError("BIP39 wordlist altered")
        words = raw.decode().split("\n")[:2048]
        if len(words) != 2048:
            raise SeedQRError("BIP39 wordlist incomplete")
        _WORDLIST = words
    return _WORDLIST


def entropy_to_mnemonic(entropy: bytes) -> str:
    if len(entropy) not in (16, 20, 24, 28, 32):
        raise SeedQRError("invalid entropy length")
    bits = len(entropy) * 8
    cs = bits // 32
    n = (int.from_bytes(entropy, "big") << cs) | (hashlib.sha256(entropy).digest()[0] >> (8 - cs))
    words = wordlist()
    count = (bits + cs) // 11
    return " ".join(words[(n >> (11 * (count - 1 - i))) & 0x7FF] for i in range(count))


def mnemonic_is_valid(mnemonic: str) -> bool:
    words = mnemonic.split()
    if len(words) not in (12, 15, 18, 21, 24):
        return False
    wl = wordlist()
    index = {w: i for i, w in enumerate(wl)}
    try:
        n = 0
        for w in words:
            n = (n << 11) | index[w]
    except KeyError:
        return False
    total = len(words) * 11
    cs = total // 33
    entropy = (n >> cs).to_bytes((total - cs) // 8, "big")
    return (n & ((1 << cs) - 1)) == (hashlib.sha256(entropy).digest()[0] >> (8 - cs))


def decode(data) -> str:
    """
    Mnemonic from the payload of a scanned QR: digits (standard SeedQR) or 16/32 raw
    bytes (CompactSeedQR). Raises SeedQRError if it is neither or the checksum fails.
    """
    text = None
    if isinstance(data, str):
        text = data
    else:
        try:
            text = data.decode("ascii")
        except UnicodeDecodeError:
            text = None

    if text is not None and text.strip().isdigit() and len(text.strip()) in (48, 96):
        digits = text.strip()
        wl = wordlist()
        words = [wl[int(digits[i:i + 4])] for i in range(0, len(digits), 4)
                 if int(digits[i:i + 4]) < 2048]
        if len(words) * 4 != len(digits):
            raise SeedQRError("invalid SeedQR")
        mnemonic = " ".join(words)
    elif isinstance(data, (bytes, bytearray)) and len(data) in (16, 32):
        mnemonic = entropy_to_mnemonic(bytes(data))
    else:
        raise SeedQRError("this QR is not a SeedQR")

    if not mnemonic_is_valid(mnemonic):
        raise SeedQRError("seed checksum wrong")
    return mnemonic
