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
from nonceverify.address import ckd_pub
from nonceverify.psbt import (PSBT, PSBTError, SCRIPT__P2PKH, SCRIPT__P2SH, SCRIPT__P2TR, SCRIPT__P2WPKH,
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
VERDICT__PARTIAL = "partial"          # v7.5: this seed's signatures clean, cosigners' not checked yet

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
    cosigners: int = 0                # multisig: signatures of other keys (their nonce not checked here)
    checked_sigs: list = field(default_factory=list)    # v7.5: (pubkey, sig) of this seed, nonce verified
    cosigner_sigs: list = field(default_factory=list)   # v7.5: (pubkey, sig) of other keys: valid, ALL, low s
    unsupported: bool = False         # a script type this method can't check (Taproot, legacy)
    stray_bip32: int = 0              # v7.5: BIP32 entries for keys that aren't in the input's script
    our_paths: list = field(default_factory=list)   # v7.6: paths of this seed's keys in the script (derived)
    quorum: tuple = None              # v7.6: multisig input: (wrapper type, m, keys in script order, declared bip32)


OWNER__EXTERNAL = "external"          # pays someone else (or nothing claims it's ours)
OWNER__OURS = "ours"                  # v7.6: proven this wallet's: derived from the seed (+ descriptor)
OWNER__UNVERIFIED = "unverified"      # multisig, our key in it, but no confirmed descriptor to check
OWNER__FAKE = "fake"                  # declared as this wallet's, but it isn't


@dataclass
class OutputInfo:
    index: int
    amount: int
    address: str
    is_change: bool
    owner: str = OWNER__EXTERNAL      # v7.6
    label: str = ""                   # "change #12", "receive #3"
    path: str = ""
    note: str = ""                    # why it's unusual, or why it's refused


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
    unchecked_cosigners: int = 0      # v7.5: cosigner signatures whose nonce no seed has checked yet
    extra_fields: int = 0             # v7.5: unknown / proprietary PSBT fields (not on chain)
    broadcast_tx: bytes = None        # v7.5: the transaction verified here, ready to broadcast
    change_unverified: bool = False   # v7.6: a multisig change that couldn't be proven: no Tx QR
    change_warning: bool = False      # v7.6: a change that is yours, somewhere a wallet may not look


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
    elif kind not in (SCRIPT__P2WPKH, SCRIPT__P2WSH):
        # v7.5: a finalized legacy input carries its signature in the scriptSig: not the
        # "native SegWit" error below, the same "not supported" as before finalizing
        return None, "legacy input: not supported", None
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
        if s > HALF_N:
            # v7.5: cosigners' signatures too. Flipping s -> N - s on a signature already
            # in the PSBT would be a free bit for the last signer
            raise InputFail("high s: non-standard, and a free bit for a covert channel")
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

        result.our_paths = [tuple(p) for p in ours.values()]
        if kind == "wsh":
            result.quorum = (script_type(utxo.script_pubkey), extra[0], list(extra[1]),
                             {pk: inp.bip32.get(pk) for pk in extra[1]})
        sigs = _signatures(kind, inp, extra, psbt.tx, index, utxo.value)
        # v7.5 review: BIP32 entries for keys that the script doesn't contain are free bytes
        # (fake pubkeys, long paths): counted with the unknown fields
        script_keys = set(extra[1]) if kind == "wsh" else {pk for pk in inp.bip32 if crypto.hash160(pk) == extra}
        result.stray_bip32 = sum(1 for pk in inp.bip32 if pk not in script_keys)

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
                result.cosigner_sigs.append((pk, sig))
                continue
            code = p2pkh_script(pk) if kind == "wpkh" else script_code
            z32 = bip143_sighash(psbt.tx, index, code, utxo.value, sig[-1])
            counter = _check_nonce(finder, ours[pk], z32, r, s, sig[-1], policy)
            result.checked_sigs.append((pk, sig))
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
FAR_INDEX = 1000          # v7.6: beyond this, a wallet restored from the seed won't look
MAX_ACCOUNT = 99


class WalletContext:
    """
    v7.6: what the inputs prove about the wallet, to judge the change against.
    - single_prefixes: account paths (m/purpose'/coin'/account') of this seed's keys that
      signed single-sig inputs, from the keys actually verified (not the PSBT's claims).
    - quorum: for a multisig input of this seed, its script's m, the type and every
      cosigner xpub. The input's witness script is what our signature commits to
      (BIP143 scriptCode): its keys are the wallet the coins are in. The PSBT's global
      xpubs are accepted only if they derive exactly those keys.
    - descriptor: the one confirmed against the paper in this session, if any.
    """
    def __init__(self, psbt, report, descriptor, net):
        self.net = net
        self.descriptor = descriptor
        self.single_prefixes, self.multisig = set(), False
        self.quorum = None
        for r in report.inputs:
            if r.status != STATUS__OK:
                continue
            if r.quorum is None:
                self.single_prefixes.update(p[:-2] for p in r.our_paths if len(p) >= 2)
            else:
                self.multisig = True
                if self.quorum is None:
                    self.quorum = _resolve_quorum(psbt, r)
        self._desc_nodes = None

    def descriptor_spk(self, our_key_path, our_fp):
        """ The confirmed descriptor's script at our key's branch and index, or None """
        desc = self.descriptor
        if desc is None:
            return None
        for key in desc.keys:
            origin = list(key.origin_path or [])
            if key.origin_fingerprint != our_fp or list(our_key_path[:len(origin)]) != origin:
                continue
            suffix = list(our_key_path[len(origin):])
            if not suffix or any(step & H for step in suffix):
                continue
            for b in range(desc.branch_count()):
                if list(key.branches[b]) + ([suffix[-1]] if key.wildcard else []) != suffix:
                    continue
                pubs = []
                for k in desc.keys:
                    point, chain = k.point, k.chain
                    for step in list(k.branches[b]) + ([suffix[-1]] if k.wildcard else []):
                        point, chain = ckd_pub(point, chain, step)
                    pubs.append(crypto.pubkey_compressed(point))
                return desc.script_pubkey(b, pubs)
        return None


def _resolve_quorum(psbt, result):
    """ (wrapper, m, [(xpub point, chain, xpub path)] in script order, sorted?, suffix length) or None """
    wrapper, m, keys, declared = result.quorum
    xpubs = []
    for raw, value in psbt.xpubs.items():
        path = _parse_xpub_path(value)
        if len(raw) == 78 and path is not None:
            try:
                xpubs.append((value[:4], path, crypto.pubkey_decode(raw[45:]), raw[13:45]))
            except ValueError:
                continue
    resolved, suffix_len = [], None
    for pk in keys:
        claim = declared.get(pk)
        if claim is None:
            return None
        fp, path = claim
        found = None
        for xfp, xpath, point, chain in xpubs:
            if xfp != fp or list(path[:len(xpath)]) != list(xpath):
                continue
            suffix = list(path[len(xpath):])
            if not suffix or any(step & H for step in suffix):
                continue
            p, c = point, chain
            for step in suffix:
                p, c = ckd_pub(p, c, step)
            if crypto.pubkey_compressed(p) == pk:
                found = (point, chain, tuple(xpath), len(suffix))
                break
        if found is None:
            return None
        if suffix_len not in (None, found[3]):
            return None
        suffix_len = found[3]
        resolved.append(found[:3])
    return wrapper, m, resolved, keys == sorted(keys), suffix_len


def _quorum_spk(quorum, suffix):
    wrapper, m, resolved, is_sorted, suffix_len = quorum
    if len(suffix) != suffix_len or any(step & H for step in suffix):
        return None
    pubs = []
    for point, chain, _xpath in resolved:
        p, c = point, chain
        for step in suffix:
            p, c = ckd_pub(p, c, step)
        pubs.append(crypto.pubkey_compressed(p))
    if is_sorted:
        pubs.sort()
    ws = bytes([0x50 + m]) + b"".join(bytes([33]) + pk for pk in pubs) + bytes([0x50 + len(pubs), 0xAE])
    wsh = b"\x00\x20" + crypto.sha256(ws)
    if wrapper == SCRIPT__P2SH:
        return b"\xa9\x14" + crypto.hash160(wsh) + b"\x87"
    return wsh


def _path_notes(path, kind, ctx: WalletContext, single: bool) -> list:
    """ v7.6 review: where a wallet restored from the seed (or Sparrow) wouldn't look """
    notes = []
    coin = 1 if ctx.net == "test" else 0
    if single:
        if len(path) != 5:
            notes.append(f"path depth {len(path)}: not a standard wallet path")
        else:
            purpose, coin_type, account, chain, number = path
            expected = {84 | H: SCRIPT__P2WPKH, 49 | H: SCRIPT__P2SH, 44 | H: SCRIPT__P2PKH}.get(purpose)
            if expected is None:
                notes.append(f"purpose {purpose & 0x7FFFFFFF}: not a single-sig wallet path")
            elif expected != kind:
                notes.append("script type doesn't match the path: Sparrow won't see these funds")
            if coin_type != (coin | H):
                notes.append("coin type of another network")
            if not account & H or (account & 0x7FFFFFFF) > MAX_ACCOUNT:
                notes.append(f"account {account & 0x7FFFFFFF}{'' if account & H else ' (not hardened)'}: unusual")
            if ctx.single_prefixes and tuple(path[:3]) not in ctx.single_prefixes:
                notes.append("another account or type than the coins spent: check Sparrow sees it")
    chain, number = path[-2], path[-1]
    if chain & H or (chain & 0x7FFFFFFF) not in (0, 1):
        notes.append(f"branch {chain & 0x7FFFFFFF}: not receive or change")
    if number & H or number > FAR_INDEX:
        notes.append(f"index {number & 0x7FFFFFFF}: a wallet restored from the seed may not find it")
    return notes


def _classify_output(finder, out, meta, ctx: WalletContext, index: int, net: str) -> OutputInfo:
    """
    v7.6 (Ollen, 10 Nov 2026): is the change really this wallet's?

    Single-sig: the output pays a key DERIVED here from the seed at the path the PSBT
    declares (the key is recomputed, never trusted): the seed can spend it. Notes when
    it's somewhere a wallet wouldn't look (_path_notes).
    Multisig: our key in the witness script is not enough (the other keys could be the
    attacker's, threshold 1). The script must be the wallet's: the descriptor confirmed
    against the paper, or the same quorum as the multisig coins being spent (their
    script is signed by our key). A multisig change for a wallet that spends no
    multisig coin is refused.
    Declared as ours (our fingerprint, or a key of this seed) but not paying us: FAKE.
    """
    info = OutputInfo(index, out.value, address(out.script_pubkey, net), False)
    spk = out.script_pubkey
    kind = script_type(spk)
    claims = meta.bip32 if meta is not None else {}
    our_fp = finder.master.fingerprint
    ws = meta.witness_script if meta is not None else None
    for pk, (_fp, _declared) in claims.items():
        path = finder.by_pubkey.get(pk)
        if path is None:
            continue
        single = ((kind == SCRIPT__P2WPKH and spk[2:] == crypto.hash160(pk)) or
                  (kind == SCRIPT__P2SH and spk[2:22] == crypto.hash160(p2wpkh_script(pk))) or
                  (kind == SCRIPT__P2PKH and spk[3:23] == crypto.hash160(pk)))
        multi = ws is not None and pk in (parse_multisig(ws) or (0, []))[1] and (
            (kind == SCRIPT__P2WSH and spk[2:] == crypto.sha256(ws)) or
            (kind == SCRIPT__P2SH and spk[2:22] == crypto.hash160(b"\x00\x20" + crypto.sha256(ws))))
        if not (single or multi):
            continue
        number = path[-1] if path else None
        chain = path[-2] if len(path) >= 2 else None
        info.path = crypto.format_path(path)
        info.label = {0: f"receive #{number}", 1: f"change #{number}"}.get(chain, f"branch {chain} #{number}")
        if multi:
            if not ctx.multisig:
                info.owner = OWNER__FAKE
                info.note = "a multisig with your key, but the coins spent are single-sig: not your wallet"
                return info
            by_desc = ctx.descriptor_spk(path, our_fp)
            by_quorum = None
            if ctx.quorum is not None:
                our = [x for x in ctx.quorum[2] if list(path[:len(x[2])]) == list(x[2])]
                if our:
                    by_quorum = _quorum_spk(ctx.quorum, list(path[len(our[0][2]):]))
            if spk in (by_desc, by_quorum):
                info.owner = OWNER__OURS
            elif by_quorum is not None:
                # the quorum of the coins being spent is known and this isn't it
                info.owner = OWNER__FAKE
                info.note = "pays a multisig with your key, but not your wallet's (other keys or threshold)"
                return info
            else:
                info.owner = OWNER__UNVERIFIED
                info.note = ("multisig: can't be proven yours. Load the descriptor from your backup "
                             "(Address > Multisig), then scan the PSBT again")
                return info
        else:
            info.owner = OWNER__OURS
        info.note = "; ".join(_path_notes(path, kind, ctx, single))
        info.is_change = True
        return info
    if any(fp == our_fp for fp, _p in claims.values()) or any(pk in finder.by_pubkey for pk in claims):
        # v7.6 review: our fingerprint, or a key of this seed, on an output that doesn't pay us
        info.owner = OWNER__FAKE
        info.note = "declared as this wallet's, but no key of this seed pays it"
    return info


# ---------------------------------------------------------------------------------------
# Whole PSBT
# ---------------------------------------------------------------------------------------
def txid(psbt: PSBT) -> str:
    """
    The txid of the transaction as it will be broadcast: with the final scriptSigs of
    nested segwit inputs (P2SH-P2WPKH / P2SH-P2WSH), which are part of the txid.
    """
    from dataclasses import replace
    from nonceverify.psbt import _serialize_no_witness
    tx = replace(psbt.tx, inputs=[replace(txin, script_sig=_script_sig_for_txid(psbt, i))
                                  for i, txin in enumerate(psbt.tx.inputs)])
    return crypto.dsha256(_serialize_no_witness(tx))[::-1].hex()


def _script_sig_for_txid(psbt: PSBT, index: int) -> bytes:
    """
    The scriptSig the input will have once finalized. v7.5: an unfinalized nested segwit
    input (P2SH-P2WPKH / P2SH-P2WSH) will carry push(redeem script), which is part of
    the txid; before, the txid shown left it out and differed from Sparrow's.
    """
    inp, txin = psbt.inputs[index], psbt.tx.inputs[index]
    if inp.final_script_sig:
        return inp.final_script_sig
    if inp.redeem_script and script_type(inp.redeem_script) in (SCRIPT__P2WPKH, SCRIPT__P2WSH):
        try:
            spk = spent_output(psbt, index).script_pubkey
        except PSBTError:
            return txin.script_sig
        if script_type(spk) == SCRIPT__P2SH and spk[2:22] == crypto.hash160(inp.redeem_script):
            return push(inp.redeem_script)
    return txin.script_sig


# ---------------------------------------------------------------------------------------
# v7.5: the transaction as it will be broadcast, made here from the verified signatures
# ---------------------------------------------------------------------------------------
def build_broadcast_tx(psbt: PSBT, report: Report):
    """
    The final transaction, serialized with its witnesses, built ONLY from signatures this
    run has checked: this seed's (nonce verified) and the cosigners' (valid, ALL, low s).
    None if any input can't be completed (unsigned, foreign, legacy, Taproot).

    Why (independent review, 10 Nov 2026): a segwit txid does not commit to the
    witnesses. A green on the QR scanned proves nothing about a DIFFERENT set of
    witnesses for the same txid (a signer that shows a clean QR and writes leaking
    signatures to its file, or a compromised coordinator). Broadcasting exactly these
    bytes closes that gap.
    """
    from nonceverify.psbt import serialize_tx
    script_sigs, witnesses = [], []
    for index, (inp, result) in enumerate(zip(psbt.inputs, report.inputs)):
        if result.status != STATUS__OK:
            return None
        try:
            utxo = spent_output(psbt, index)
            kind, script_code, extra = _spend_script(utxo, inp)
        except (PSBTError, InputFail, ValueError):
            return None
        if kind is None:
            return None
        allowed = {pk: sig for pk, sig in result.checked_sigs + result.cosigner_sigs}
        redeem = None
        if script_type(utxo.script_pubkey) == SCRIPT__P2SH:
            redeem = _single_push(inp.final_script_sig) if inp.final_script_sig else inp.redeem_script
        if kind == "wpkh":
            if len(allowed) != 1:
                return None
            (pk, sig), = allowed.items()
            witness = [sig, pk]
        else:
            m, keys = extra
            chosen = [(pk, allowed[pk]) for pk in keys if pk in allowed][:m]
            if len(chosen) < m:
                return None
            witness = [b""] + [sig for _pk, sig in chosen] + [script_code]
        if inp.final_witness is not None:
            # finalized: the signatures verified are exactly the final witness's
            # (_signatures reads them from there), so it goes out as it is
            witness = list(inp.final_witness)
        script_sigs.append(push(redeem) if redeem else b"")
        witnesses.append(witness)
    raw = serialize_tx(psbt.tx, script_sigs, witnesses)
    from nonceverify.psbt import _serialize_no_witness, parse_tx
    if crypto.dsha256(_serialize_no_witness(parse_tx(raw)))[::-1].hex() != report.txid:
        return None                       # never export something whose txid isn't the one shown
    return raw


def verify_psbt(psbt: PSBT, master: crypto.HDKey, progress=None, scan_progress=None,
                grinding: str = GRIND__LIBSECP, clean_elsewhere=frozenset(), descriptor=None) -> Report:
    """
    Check every input of `psbt`. `progress(i, n)` is called before each input and
    `scan_progress(i, n)` while looking for a key not found at the PSBT's paths.
    `grinding`: the signer's low-R rule, GRIND__LIBSECP (libsecp256k1 wallets, Coldcard,
    Bitcoin Core, Sparrow), GRIND__EMBIT (embit: SeedSigner) or GRIND__NONE.
    `clean_elsewhere`: (pubkey, signature) pairs whose nonce another seed of this
    session already verified (v7.5): a cosigner signature among them counts as checked.
    """
    net = network_of(psbt)
    if grinding not in GRINDING_POLICIES:
        raise ValueError("unknown grinding policy")
    report = Report(fingerprint=master.fingerprint.hex(), network=net, txid=txid(psbt),
                    grinding=grinding, locktime=int.from_bytes(psbt.tx.locktime, "little"),
                    version=int.from_bytes(psbt.tx.version, "little"),
                    extra_fields=getattr(psbt, "extra_fields", 0))
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
        ctx = WalletContext(psbt, report, descriptor, net)
        for index, out in enumerate(psbt.tx.outputs):
            meta = psbt.outputs[index] if index < len(psbt.outputs) else None
            report.outputs.append(_classify_output(finder, out, meta, ctx, index, net))
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
        # v7.5 (independent review): a cosigner's signature is checked for validity, ALL
        # and low s, but its nonce can only be recomputed with ITS seed. One SeedSigner
        # signing for two seeds of the same multisig could leak the other seed through
        # that signature: no green until another seed of this session has verified it.
        report.unchecked_cosigners = sum(1 for r in report.inputs for c in r.cosigner_sigs
                                         if c not in clean_elsewhere)
        report.verdict = VERDICT__PARTIAL if report.unchecked_cosigners else VERDICT__OK
        report.summary = "nonces verified"
    fake = [o for o in report.outputs if o.owner == OWNER__FAKE]
    if fake:
        # v7.6: an output presented as change that isn't this wallet's moves funds away
        report.verdict = VERDICT__FAIL
        problem = "; ".join(f"output #{o.index} ({o.note})" for o in fake)
        report.tx_problem = ((report.tx_problem + " ") if report.tx_problem else "") + \
            f"NOT YOUR CHANGE: {problem}. Do not broadcast."
    report.change_unverified = any(o.owner == OWNER__UNVERIFIED for o in report.outputs)
    report.change_warning = any(o.owner == OWNER__OURS and o.note for o in report.outputs)
    report.extra_fields += sum(r.stray_bip32 for r in report.inputs)
    if report.verdict in (VERDICT__OK, VERDICT__PARTIAL):
        report.broadcast_tx = build_broadcast_tx(psbt, report)
    if report.verdict == VERDICT__OK and report.broadcast_tx is None:
        # v7.5 review: this seed's signatures are clean but the transaction can't be
        # completed here (an input not signed yet, a foreign one): never "broadcast"
        report.verdict = VERDICT__PARTIAL
    return report
