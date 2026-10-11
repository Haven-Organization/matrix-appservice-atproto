"""Signing keys and ``did:key`` encoding.

ATProto signs with ECDSA over SHA-256 on one of two curves: secp256k1
("k256", what this bridge generates, same as Bluesky's own PDS) or P-256
(accepted when verifying others). Two protocol-specific rules on top of
plain ECDSA:

- Signatures are the raw 64-byte ``r || s``, not DER.
- Only "low-S" signatures are valid. For every signature ``(r, s)`` the
  pair ``(r, n - s)`` verifies too; requiring the smaller ``s`` makes a
  signature's bytes unique, so nobody can alter a signed block's hash
  without the key. Signing normalizes to it, verifying rejects the other.

Public keys travel as ``did:key:z...``: base58btc over a curve prefix plus
the 33-byte compressed point.
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


@dataclass(frozen=True)
class _Curve:
    curve: ec.EllipticCurve
    multicodec: bytes
    order: int


_K256 = _Curve(ec.SECP256K1(), b"\xe7\x01", 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141)
_P256 = _Curve(ec.SECP256R1(), b"\x80\x24", 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551)

DID_KEY_PREFIX = "did:key:"


def b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    digits = ""
    while number:
        number, remainder = divmod(number, 58)
        digits = _B58_ALPHABET[remainder] + digits
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + digits


def b58decode(text: str) -> bytes:
    number = 0
    for char in text:
        number = number * 58 + _B58_ALPHABET.index(char)
    zeros = len(text) - len(text.lstrip("1"))
    return b"\0" * zeros + number.to_bytes((number.bit_length() + 7) // 8, "big")


def _parse_multikey(multibase: str) -> tuple[_Curve, ec.EllipticCurvePublicKey]:
    """``z...`` multibase -> curve and public key."""
    if not multibase.startswith("z"):
        raise ValueError("Expected a base58btc ('z...') multibase key")
    raw = b58decode(multibase[1:])
    for curve in (_K256, _P256):
        if raw.startswith(curve.multicodec):
            point = raw[len(curve.multicodec) :]
            return curve, ec.EllipticCurvePublicKey.from_encoded_point(curve.curve, point)
    raise ValueError("Unsupported key type (only secp256k1 and P-256)")


def verify_signature(did_key: str, data: bytes, signature: bytes) -> bool:
    """Whether ``signature`` is a valid low-S signature of ``data`` by
    ``did_key`` (a ``did:key:z...``, or the bare ``z...`` multibase form a
    DID document's ``publicKeyMultibase`` carries)."""
    try:
        curve, public_key = _parse_multikey(did_key.removeprefix(DID_KEY_PREFIX))
    except ValueError:
        return False
    if len(signature) != 64:
        return False
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if s > curve.order // 2:
        return False
    try:
        public_key.verify(encode_dss_signature(r, s), data, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


class SigningKey:
    """A secp256k1 private key held by the bridge on a local user's behalf."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey) -> None:
        self._private_key = private_key

    @classmethod
    def generate(cls) -> SigningKey:
        return cls(ec.generate_private_key(_K256.curve))

    @classmethod
    def from_hex(cls, private_hex: str) -> SigningKey:
        return cls(ec.derive_private_key(int(private_hex, 16), _K256.curve))

    def to_hex(self) -> str:
        return format(self._private_key.private_numbers().private_value, "064x")

    def did_key(self) -> str:
        point = self._private_key.public_key().public_bytes(Encoding.X962, PublicFormat.CompressedPoint)
        return DID_KEY_PREFIX + "z" + b58encode(_K256.multicodec + point)

    def multibase(self) -> str:
        """The ``publicKeyMultibase`` form: the did:key without its prefix."""
        return self.did_key().removeprefix(DID_KEY_PREFIX)

    def sign(self, data: bytes) -> bytes:
        r, s = decode_dss_signature(self._private_key.sign(data, ec.ECDSA(hashes.SHA256())))
        if s > _K256.order // 2:
            s = _K256.order - s
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")
