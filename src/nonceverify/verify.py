"""
The verification itself: for every signature that the broadcast transaction will carry
and that this seed made, recompute the nonce the signer was obliged to use and compare
it with the one it did use.

A signer that follows RFC 6979 has no freedom at all: the nonce is a function of the
private key and of the transaction. If the r of the signature is exactly the one this
independent code recomputes, the signer did not pick the nonce, so it cannot have hidden
anything in it (the "Dark Skippy" seed exfiltration and every other nonce covert channel).

Fail closed. The PSBT comes from the very device under suspicion, so nothing it declares
is trusted:
- which keys are this seed's is decided by deriving them from the seed (at every path the
  PSBT mentions, under any fingerprint, and if needed by scanning the account branches),
  never by the fingerprint labels;
- the signatures checked are the ones the transaction will contain: the final witness if
  the input is finalized (any partial signature must then be identical), else every
  partial signature; each must belong to a key of the input's own script;
- anything that can't be attributed or checked turns the verdict yellow, never green.

Freedoms that remain even with deterministic nonces, and are checked as well:
- low-R grinding: the rule (libsecp256k1: r < 2^255; embit: DER <= 70 bytes; none) is
  set by the user for the signer being checked, and only the FIRST retry that rule
  accepts passes: neither "grind or not" nor "which retry" is left to the signer;
- strict DER (BIP66): padding or negative integers would be free bits too;
- transaction fields (version, nSequence, locktime): only the values wallets use;
- s or N - s: only low s is standard, so a high s fails;
- the same r twice: reveals a private key outright.
"""
from dataclasses import dataclass, field

from nonceverify import crypto
from nonceverify.psbt import (PSBT, PSBTError, SCRIPT__P2SH, SCRIPT__P2TR, SCRIPT__P2WPKH,
    SCRIPT__P2WSH, address, bip143_sighash, network_of, p2pkh_script, p2wpkh_script,
    parse_multisig, push, script_type, spent_output)

STATUS__OK = "ok"                     # every signature of this seed on the input verified
STATUS__FAIL = "fail"                 # do not broadcast
STATUS__UNVERIFIABLE = "unverifiable" # can't be checked (Taproot, unknown key, ...)
STATUS__UNSIGNED = "unsigned"         # this seed's key is there, its signature is not
STATUS__FOREIGN = "foreign"           # not spent by this seed, no signature at stake

VERDICT__OK = "ok"
VERDICT__FAIL = "fail"
VERDICT__INCOMPLETE = "incomplete"

HALF_N = crypto.N // 2
LOW_R_LIMIT = 1 << 255                # r below this fits 32 bytes in DER (low R)
SCAN_GAP = 40                         # addresses tried per branch when looking for a key
DEEP_SCAN_GAP = 120                   # multisig: addresses tried on the wallet's own branches
MAX_KNOWN_PATHS = 300                 # PSBT paths derived at most (a PSBT can list thousands)
MAX_SCAN_PREFIXES = 8                 # account branches scanned at most
H = 0x80000000


@dataclass
class InputResult:
    index: int
    amount: int = None
    address: str = ""
    path: str = ""
    status: str = STATUS__FOREIGN
    message: str = ""
    counter: int = None               # RFC 6979 retry counter (0 = no grinding)
    r: int = None
    cosigners: int = 0                # multisig: signatures of other keys (not checked here)
    unsupported: bool = False         # a script type this method can't check (Taproot, legacy)


@dataclass
class OutputInfo:
    index: int
    amount: int
    address: str
    is_change: bool


@dataclass
class Report:
    fingerprint: str
    txid: str = ""
    inputs: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    fee: int = None
    verdict: str = VERDICT__INCOMPLETE
    summary: str = ""
    network: str = "main"
    seed_matches: bool = False
    grinding: str = "libsecp"
    locktime: int = 0
    version: int = 2
    tx_problem: str = ""


class InputFail(Exception):
    pass


def _parse_xpub_path(value: bytes):
    """ Path of a PSBT_GLOBAL_XPUB value (fingerprint + path), or None """
    if len(value) < 4 or (len(value) - 4) % 4 or (len(value) - 4) // 4 > 10:
        return None
    return [int.from_bytes(value[i:i + 4], "little") for i in range(4, len(value), 4)]


# ---------------------------------------------------------------------------------------
# Which keys are this seed's
# ---------------------------------------------------------------------------------------
class KeyFinder:
    """
    Keys of this seed, found by derivation only. First at every BIP32 path mentioned in
    the PSBT (inputs, outputs; the fingerprint next to it is ignored), then, for a key
    still unknown, by scanning receive and change addresses 0..SCAN_GAP-1 of the
    account branches seen in the PSBT, and of the standard accounts 0-1.
    """
    def __init__(self, master: crypto.HDKey, psbt: PSBT, network: str):
        self.master = master
        self.network = network
        self.by_pubkey = {}           # compressed pubkey -> path
        self.by_hash = {}             # hash160(pubkey) -> (pubkey, path)
        self.nodes = {}               # path tuple -> HDKey (cache of intermediate nodes)
        self.scanned = False
        coin = 1 if network == "test" else 0
        paths = set()
        for inp in psbt.inputs:
            paths.update(tuple(p) for _f, p in inp.bip32.values())
        for out in psbt.outputs:
            paths.update(tuple(p) for _f, p in out.bip32.values())
        self.known_paths = sorted(paths)[:MAX_KNOWN_PATHS]
        seen_prefixes = sorted({p[:-2] for p in self.known_paths if len(p) >= 3})
        standard = []
        for account in (0, 1):
            for purpose in ((84 | H,), (48 | H, 2 | H), (49 | H,), (48 | H, 1 | H), (44 | H,)):
                standard.append((purpose[0], coin | H, account | H) + purpose[1:])
        prefixes = []
        for prefix in seen_prefixes + standard:
            if prefix not in prefixes:
                prefixes.append(prefix)
        self.scan_prefixes = prefixes[:MAX_SCAN_PREFIXES]
        # Multisig: every (chain, index) seen in the PSBT, under the global xpubs' paths,
        # the seen account branches and multisig accounts 0..9 of both BIP48 script types
        self.suffixes = sorted({p[-2:] for p in self.known_paths if len(p) >= 2})[:16]
        ms = [tuple(_parse_xpub_path(v)) for v in psbt.xpubs.values() if _parse_xpub_path(v) is not None]
        ms += seen_prefixes
        for account in range(10):
            for script_type_index in (2, 1):
                ms.append((48 | H, coin | H, account | H, script_type_index | H))
        self.multisig_prefixes = []
        for prefix in ms:
            if prefix not in self.multisig_prefixes and len(prefix) <= 10:
                self.multisig_prefixes.append(prefix)
        self.multisig_prefixes = self.multisig_prefixes[:64]
        self.deep_prefixes = seen_prefixes[:4]       # the wallet's own branches: a longer scan
        self.multisig_scanned = False
        for path in self.known_paths:
            self._add(path)

    def _node(self, path: tuple) -> crypto.HDKey:
        # iterative, reusing the deepest cached ancestor
        depth = len(path)
        while depth > 0 and path[:depth] not in self.nodes:
            depth -= 1
        node = self.nodes[path[:depth]] if depth else self.master
        for i in range(depth, len(path)):
            node = node.child(path[i])
            self.nodes[path[:i + 1]] = node
        return node

    def _add(self, path: tuple):
        try:
            pub = self._node(path).pubkey
        except ValueError:
            return
        self.by_pubkey.setdefault(pub, path)
        self.by_hash.setdefault(crypto.hash160(pub), (pub, path))

    def _scan(self, progress=None):
        if self.scanned:
            return
        self.scanned = True
        for n, prefix in enumerate(self.scan_prefixes):
            if progress:
                progress(n, len(self.scan_prefixes))
            for chain in (0, 1):
                for index in range(SCAN_GAP):
                    self._add(prefix + (chain, index))

    def scan_multisig(self, progress=None):
        """ Once per PSBT: look for further keys of this seed among multisig branches """
        if self.multisig_scanned:
            return
        self.multisig_scanned = True
        total = len(self.multisig_prefixes) + len(self.deep_prefixes)
        for n, prefix in enumerate(self.multisig_prefixes):
            if progress and n % 8 == 0:
                progress(n, total)
            for suffix in self.suffixes:
                self._add(prefix + suffix)
        for n, prefix in enumerate(self.deep_prefixes):
            if progress:
                progress(len(self.multisig_prefixes) + n, total)
            for chain in (0, 1):
                for index in range(DEEP_SCAN_GAP):
                    self._add(prefix + (chain, index))

    def path_of(self, pubkey: bytes, progress=None):
        if pubkey not in self.by_pubkey and not self.scanned:
            self._scan(progress)
        return self.by_pubkey.get(pubkey)

    def owner_of_hash(self, h: bytes, progress=None):
        """ (pubkey, path) of this seed's key whose hash160 is h, or None """
        if h not in self.by_hash and not self.scanned:
            self._scan(progress)
        return self.by_hash.get(h)

    def private_key(self, path: tuple) -> int:
        return self._node(path).priv

    def wipe(self):
        for node in self.nodes.values():
            if node is not self.master:
                node.wipe()
        self.nodes = {}


# ---------------------------------------------------------------------------------------
# One input
# ---------------------------------------------------------------------------------------
def _spend_script(utxo, inp):
    """
    (kind, script_code_or_None, program_keys) for the coin spent:
      kind "wpkh": program = the 20-byte key hash, script code built per key;
      kind "wsh": script_code = witness script, program_keys = (m, [keys]).
    Raises InputFail for inconsistencies, returns kind None if not verifiable.
    """
    kind = script_type(utxo.script_pubkey)
    spk = utxo.script_pubkey
    if kind == SCRIPT__P2TR or inp.taproot:
        return None, "Taproot: the BIP340 nonce contains random data, it can't be recomputed", None
    if kind == SCRIPT__P2SH:
        redeem = inp.redeem_script
        if inp.final_script_sig:
            pushed = _single_push(inp.final_script_sig)
            if pushed is None or (redeem is not None and pushed != redeem):
                raise InputFail("invalid final scriptSig")
            redeem = pushed
        if redeem is None or crypto.hash160(redeem) != spk[2:22]:
            raise InputFail("redeem script missing or not matching the address")
        spk = redeem
        kind = script_type(redeem)
        if kind not in (SCRIPT__P2WPKH, SCRIPT__P2WSH):
            return None, "legacy P2SH input: not supported", None
    elif inp.final_script_sig:
        raise InputFail("non-empty scriptSig on a native SegWit input")

    if kind == SCRIPT__P2WPKH:
        return "wpkh", None, spk[2:]
    if kind == SCRIPT__P2WSH:
        ws = inp.witness_script
        if inp.final_witness:
            if ws is not None and inp.final_witness[-1] != ws:
                raise InputFail("witness script differs between the PSBT and the final witness")
            ws = inp.final_witness[-1]
        if ws is None or crypto.sha256(ws) != spk[2:]:
            raise InputFail("witness script missing or not matching the address")
        multisig = parse_multisig(ws)
        if multisig is None:
            return None, "non-standard multisig script: not supported", None
        return "wsh", ws, multisig
    return None, "legacy input: not supported", None


def _single_push(script_sig: bytes):
    """ The data of a scriptSig made of exactly one push, else None """
    if not script_sig:
        return None
    n = script_sig[0]
    if 1 <= n < 0x4C:
        data, rest = script_sig[1:1 + n], script_sig[1 + n:]
    elif n == 0x4C and len(script_sig) >= 2:
        data, rest = script_sig[2:2 + script_sig[1]], script_sig[2 + script_sig[1]:]
    elif n == 0x4D and len(script_sig) >= 3:
        size = int.from_bytes(script_sig[1:3], "little")
        data, rest = script_sig[3:3 + size], script_sig[3 + size:]
    else:
        return None
    if rest or push(data) != script_sig:
        return None
    return data


SIGHASH_ALL = 0x01


def sighash_name(hashtype: int) -> str:
    base = {1: "ALL", 2: "NONE", 3: "SINGLE"}.get(hashtype & 0x1F, f"0x{hashtype & 0x1F:02x}")
    return base + ("|ANYONECANPAY" if hashtype & 0x80 else "") + f" (0x{hashtype:02x})"


def sighash_problem(hashtype: int) -> str:
    """ Why a signature that isn't SIGHASH_ALL must not be broadcast """
    base, anyone = hashtype & 0x1F, hashtype & 0x80
    if base == 2:
        why = "does not commit to the outputs: anyone in the mempool can redirect the funds"
    elif base == 3:
        why = "commits to one output only: the others can be changed in the mempool"
    elif anyone:
        why = "does not commit to the other inputs: the transaction can be modified and combined"
    else:
        why = "non-standard type"
    return f"sighash {sighash_name(hashtype)} instead of ALL: {why}"


def _taproot_sighash_problem(inp, utxo) -> str:
    """
    Taproot inputs can't have their nonce checked (yellow), but their signatures' sighash
    can: BIP340 signatures are 64 bytes (SIGHASH_DEFAULT, i.e. ALL) or 65 with the
    hashtype last. Anything but ALL is red, as for ECDSA. Checked in the BIP371 fields
    and, when the coin is known to be P2TR, in the final witness.
    """
    types = list(inp.tap_sighashes)
    w = inp.final_witness
    if w and utxo is not None and script_type(utxo.script_pubkey) == SCRIPT__P2TR:
        items = list(w)
        if len(items) >= 2 and items[-1][:1] == b"\x50":
            items = items[:-1]                       # annex
        candidates = items if len(items) == 1 else items[:-2]   # key path, or script path stack
        for item in candidates:
            if len(item) == 64:
                types.append(0)
            elif len(item) == 65:
                # On a script path a 65-byte data push would read as a signature: a false
                # red at worst, never a false green (Taproot is never green anyway)
                types.append(item[64] if item[64] else -1)
    for t in types:
        if t == -1:
            return "Taproot signature of invalid length"
        if t not in (0, SIGHASH_ALL):
            return sighash_problem(t)
    return None


def _signatures(kind, inp, multisig, tx, index, amount):
    """
    [(pubkey, sig_with_hashtype)] that the broadcast transaction will carry for this
    input. Every signature must belong to a key of the input's own script, and every one,
    cosigners' and unattributable ones included, must be SIGHASH_ALL: no wallet signs
    anything else, and NONE / SINGLE / ANYONECANPAY leave parts of the transaction open
    to change after signing.
    """
    def valid_for(pubkey, sig):
        if not crypto.is_strict_der_signature(sig):
            raise InputFail("signature not in strict DER (BIP66)")
        if sig[-1] != SIGHASH_ALL:
            raise InputFail(sighash_problem(sig[-1]))
        code = p2pkh_script(pubkey) if kind == "wpkh" else multisig_script
        r, s = crypto.der_decode(sig[:-1])
        z = int.from_bytes(bip143_sighash(tx, index, code, amount, sig[-1]), "big")
        return crypto.ecdsa_verify(crypto.pubkey_decode(pubkey), z, r, s)

    multisig_script = inp.final_witness[-1] if (kind == "wsh" and inp.final_witness) else inp.witness_script

    if kind == "wpkh":
        program = multisig        # the 20-byte hash, passed through the same slot
        for pk in inp.partial_sigs:
            if crypto.hash160(pk) != program:
                raise InputFail("partial signature by a key foreign to the input")
        if inp.final_witness is not None:
            w = inp.final_witness
            if len(w) != 2 or crypto.hash160(w[1]) != program:
                raise InputFail("invalid final witness")
            for pk, sig in inp.partial_sigs.items():
                if pk != w[1] or sig != w[0]:
                    raise InputFail("partial signature differs from the final witness")
            sigs = [(w[1], w[0])]
        else:
            sigs = list(inp.partial_sigs.items())
        for pk, sig in sigs:
            if not valid_for(pk, sig):
                raise InputFail("signature not valid for this transaction")
        return sigs

    m, keys = multisig
    for pk in inp.partial_sigs:
        if pk not in keys:
            raise InputFail("partial signature by a key foreign to the script")
    if inp.final_witness is not None:
        w = inp.final_witness
        if len(w) < 2 or w[0] != b"" or len(w) - 2 != m:
            raise InputFail("invalid final multisig witness")
        sigs, k = [], 0
        for sig in w[1:-1]:
            # CHECKMULTISIG: signatures in the order of the keys
            while k < len(keys) and not valid_for(keys[k], sig):
                k += 1
            if k == len(keys):
                raise InputFail("invalid signature in the final witness")
            sigs.append((keys[k], sig))
            k += 1
        for pk, sig in inp.partial_sigs.items():
            if (pk, sig) not in sigs and any(p == pk for p, _ in sigs):
                raise InputFail("partial signature differs from the final witness")
            if (pk, sig) not in sigs and not valid_for(pk, sig):
                # Not in the final witness, but a coordinator could re-finalize with it:
                # every signature in the PSBT is held to the same rules (valid, ALL)
                raise InputFail("partial signature not valid for this transaction")
        return sigs
    sigs = list(inp.partial_sigs.items())
    for pk, sig in sigs:
        if not valid_for(pk, sig):
            raise InputFail("signature not valid for this transaction")
    return sigs


GRIND__LIBSECP = "libsecp"            # retry until r < 2^255: libsecp256k1, Bitcoin Core, Coldcard, Sparrow
GRIND__EMBIT = "embit"                # retry until the DER signature is <= 70 bytes: embit (SeedSigner)
GRIND__NONE = "none"                  # never retry
GRINDING_POLICIES = (GRIND__LIBSECP, GRIND__EMBIT, GRIND__NONE)


def _extra(counter: int) -> bytes:
    return b"" if counter == 0 else counter.to_bytes(4, "little") + b"\x00" * 28


def expected_nonce(priv: int, z32: bytes, policy: str):
    """
    (counter, r) of the one signature an honest signer with this grinding policy
    produces: the first RFC 6979 retry its own rule accepts. No choice is left.
    """
    z = int.from_bytes(z32, "big")
    for counter in range(0, crypto.MAX_GRIND_COUNTER + 1):
        k = crypto.rfc6979_k(priv, z32, _extra(counter))
        r = crypto.point_mul_g(k)[0] % crypto.N
        if policy == GRIND__NONE:
            return counter, r
        if policy == GRIND__LIBSECP and r < LOW_R_LIMIT:
            return counter, r
        if policy == GRIND__EMBIT:
            s = pow(k, -1, crypto.N) * (z + r * priv) % crypto.N
            if crypto.der_length(r, min(s, crypto.N - s)) <= 70:
                return counter, r
    return None, None


def _check_nonce(finder, path, z32, r, s, hashtype, policy: str):
    """
    The RFC 6979 retry counter if the signature used the one nonce the signer was
    obliged to use, else InputFail. The grinding rule depends on the device and is set
    by the user: if the signer could pick it, "grind or not" (or "stop at which retry")
    would be free bits.
    """
    if s > HALF_N:
        raise InputFail("high s: non-standard, and a free bit for a covert channel")
    if hashtype != SIGHASH_ALL:
        raise InputFail(sighash_problem(hashtype))
    priv = finder.private_key(path)
    counter, expected_r = expected_nonce(priv, z32, policy)
    if r == expected_r:
        return counter
    other = crypto.find_deterministic_nonce(priv, z32, r)
    if other is None:
        raise InputFail("the nonce is NOT the RFC 6979 one: the signer chose it")
    raise InputFail(f"RFC 6979 nonce at retry {other}, but the chosen signer's grinding rule requires retry "
                    f"{counter}: the signer did not follow its own rule. A signature that follows "
                    "another device's rule is exactly the trace of this attack")


def _check_input(psbt, index, finder, seen_r, policy, scan_progress):
    inp = psbt.inputs[index]
    result = InputResult(index=index)
    net = finder.network
    # PSBT_IN_SIGHASH_TYPE: the coordinator asking the signer for a sighash. Sparrow and
    # every wallet ask for ALL (or nothing); anything else is a request to sign a
    # transaction that can be changed afterwards, whoever signs it. Taproot's DEFAULT (0)
    # is ALL too.
    requested = inp.sighash_type
    if requested == 0 and not inp.taproot:
        # DEFAULT is Taproot's ALL: fine on a P2TR coin even without BIP371 fields
        try:
            if script_type(spent_output(psbt, index).script_pubkey) == SCRIPT__P2TR:
                requested = SIGHASH_ALL
        except PSBTError:
            pass
    if requested is not None and requested != SIGHASH_ALL and not (requested == 0 and inp.taproot):
        result.status = STATUS__FAIL
        result.message = "the PSBT requests " + sighash_problem(requested)
        return result, False
    problem = _taproot_sighash_problem(inp, None)
    if problem:
        result.status = STATUS__FAIL
        result.message = problem
        return result, False
    try:
        utxo = spent_output(psbt, index)
    except PSBTError as e:
        # Without the coin being spent nothing can be checked, and a signature for it
        # (Taproot ones included) could still be added by the coordinator: never green.
        result.status = STATUS__UNVERIFIABLE
        result.message = str(e)
        return result, False
    result.amount = utxo.value
    result.address = address(utxo.script_pubkey, net)
    problem = _taproot_sighash_problem(inp, utxo)
    if problem:
        result.status = STATUS__FAIL
        result.message = problem
        return result, True
    has_sigs = bool(inp.partial_sigs or inp.final_witness or inp.final_script_sig)

    try:
        kind, script_code, extra = _spend_script(utxo, inp)
        if kind is None:
            result.status = STATUS__UNVERIFIABLE if (has_sigs or _maybe_ours(finder, inp, utxo)) else STATUS__FOREIGN
            result.message = script_code
            result.unsupported = result.status == STATUS__UNVERIFIABLE
            return result, True

        # This seed's keys in the script, by derivation
        if kind == "wpkh":
            owner = finder.owner_of_hash(extra, scan_progress)
            ours = {owner[0]: owner[1]} if owner else {}
        else:
            # every key of the script, cosigners included: one of them could be a second
            # key of this seed that the PSBT doesn't declare (the scan runs once per PSBT)
            ours = {}
            for pk in extra[1]:
                path = finder.path_of(pk, scan_progress)
                if path is not None:
                    ours[pk] = path
            if len(ours) < len(extra[1]):
                finder.scan_multisig(scan_progress)
                ours = {pk: finder.by_pubkey[pk] for pk in extra[1] if pk in finder.by_pubkey}

        sigs = _signatures(kind, inp, extra, psbt.tx, index, utxo.value)

        if not ours:
            if sigs:
                result.status = STATUS__UNVERIFIABLE
                result.message = "signed by keys that are not this seed's"
            else:
                result.status = STATUS__FOREIGN
                result.message = "not spent by this seed"
            return result, True

        result.path = ", ".join(crypto.format_path(p) for p in ours.values())
        signed = {pk for pk, _ in sigs}
        for pk, sig in sigs:
            r, s = crypto.der_decode(sig[:-1])
            if r in seen_r:
                raise InputFail(f"same r as input #{seen_r[r]}: the private key can be computed")
            seen_r[r] = index
            if pk not in ours:
                result.cosigners += 1
                continue
            code = p2pkh_script(pk) if kind == "wpkh" else script_code
            z32 = bip143_sighash(psbt.tx, index, code, utxo.value, sig[-1])
            counter = _check_nonce(finder, ours[pk], z32, r, s, sig[-1], policy)
            result.counter = counter if result.counter is None else max(result.counter, counter)
            result.r = r

        if any(pk not in signed for pk in ours):
            result.status = STATUS__UNSIGNED
            result.message = "not signed by this seed"
            return result, True
        result.status = STATUS__OK
        result.message = "RFC 6979" if not result.counter else f"RFC 6979, low-R retry {result.counter}"
        if result.cosigners:
            result.message += f" · {result.cosigners} cosigner signature(s)"
    except InputFail as e:
        result.status = STATUS__FAIL
        result.message = str(e)
    except (ValueError, PSBTError) as e:
        result.status = STATUS__FAIL
        result.message = f"invalid input: {e}"
    return result, True


def _maybe_ours(finder, inp, utxo) -> bool:
    """ An input we can't verify still matters if this seed could be behind it """
    if inp.bip32 or inp.taproot:
        return True
    return script_type(utxo.script_pubkey) == SCRIPT__P2TR


# ---------------------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------------------
def _is_change(finder, out, meta) -> bool:
    """ The output script really pays a key of this seed (derived, not declared) """
    if meta is None:
        return False
    spk = out.script_pubkey
    kind = script_type(spk)
    for pk in meta.bip32:
        path = finder.by_pubkey.get(pk)
        if path is None:
            continue
        if kind == SCRIPT__P2WPKH and spk[2:] == crypto.hash160(pk):
            return True
        if kind == SCRIPT__P2SH and spk[2:22] == crypto.hash160(p2wpkh_script(pk)):
            return True
        ws = meta.witness_script
        if ws is not None and pk in (parse_multisig(ws) or (0, []))[1]:
            if kind == SCRIPT__P2WSH and spk[2:] == crypto.sha256(ws):
                return True
            if kind == SCRIPT__P2SH and spk[2:22] == crypto.hash160(b"\x00\x20" + crypto.sha256(ws)):
                return True
    return False


# ---------------------------------------------------------------------------------------
# Whole PSBT
# ---------------------------------------------------------------------------------------
def txid(psbt: PSBT) -> str:
    from nonceverify.psbt import _serialize_no_witness
    return crypto.dsha256(_serialize_no_witness(psbt.tx))[::-1].hex()


def verify_psbt(psbt: PSBT, master: crypto.HDKey, progress=None, scan_progress=None,
                grinding: str = GRIND__LIBSECP) -> Report:
    """
    Check every input of `psbt`. `progress(i, n)` is called before each input and
    `scan_progress(i, n)` while looking for a key not found at the PSBT's paths.
    `grinding`: the signer's low-R rule, GRIND__LIBSECP (libsecp256k1 wallets, Coldcard,
    Bitcoin Core, Sparrow), GRIND__EMBIT (embit: SeedSigner) or GRIND__NONE.
    """
    net = network_of(psbt)
    if grinding not in GRINDING_POLICIES:
        raise ValueError("unknown grinding policy")
    report = Report(fingerprint=master.fingerprint.hex(), network=net, txid=txid(psbt),
                    grinding=grinding, locktime=int.from_bytes(psbt.tx.locktime, "little"),
                    version=int.from_bytes(psbt.tx.version, "little"))
    finder = KeyFinder(master, psbt, net)
    seen_r = {}
    total_in = 0
    known_amounts = True
    try:
        for index in range(len(psbt.inputs)):
            if progress:
                progress(index, len(psbt.inputs))
            result, known = _check_input(psbt, index, finder, seen_r, grinding, scan_progress)
            report.inputs.append(result)
            if result.amount is None:
                known_amounts = False
            else:
                total_in += result.amount

        spent = 0
        for index, out in enumerate(psbt.tx.outputs):
            meta = psbt.outputs[index] if index < len(psbt.outputs) else None
            report.outputs.append(OutputInfo(index, out.value, address(out.script_pubkey, net),
                                             _is_change(finder, out, meta)))
            spent += out.value
        if known_amounts:
            report.fee = total_in - spent
    finally:
        finder.wipe()

    # Fields of the transaction itself are a channel too if the signer could pick them:
    # only the values wallets use (Sparrow: version 2, nSequence fffffffd/e/f, locktime =
    # a block height or 0). The locktime is shown, to compare with Sparrow's.
    tx = psbt.tx
    sequences = {i.sequence for i in tx.inputs}
    if report.version not in (1, 2):
        report.tx_problem = f"unusual transaction version ({report.version})"
    elif len(sequences) != 1 or int.from_bytes(next(iter(sequences)), "little") not in (0xFFFFFFFD, 0xFFFFFFFE, 0xFFFFFFFF):
        report.tx_problem = "non-standard nSequence, or different between inputs: possible covert channel"
    elif report.locktime >= 500_000_000:
        report.tx_problem = "locktime given as a date: unusual, possible covert channel"

    statuses = [r.status for r in report.inputs]
    report.seed_matches = any(r.path for r in report.inputs)
    if STATUS__FAIL in statuses or report.tx_problem:
        report.verdict = VERDICT__FAIL
        report.summary = "DO NOT BROADCAST"
    elif not report.seed_matches and not any(r.unsupported for r in report.inputs):
        report.verdict = VERDICT__FAIL
        report.summary = "this seed spends nothing here"
    elif STATUS__UNSIGNED in statuses or STATUS__UNVERIFIABLE in statuses:
        report.verdict = VERDICT__INCOMPLETE
        report.summary = "verification incomplete"
    else:
        report.verdict = VERDICT__OK
        report.summary = "nonces verified"
    return report
