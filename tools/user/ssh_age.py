"""Convert an OpenSSH ed25519 private key to an age X25519 identity.

Same result as ``ssh-to-age -private-key`` (Mic92/ssh-to-age): the age
secret key is the first 32 bytes of SHA-512 over the ed25519 seed, bech32
encoded with the ``AGE-SECRET-KEY-`` prefix. Only unencrypted keys in the
``openssh-key-v1`` format are supported.
"""

import base64
import binascii
import hashlib
import struct
from typing import List, Tuple

_BEGIN = "-----BEGIN OPENSSH PRIVATE KEY-----"
_END = "-----END OPENSSH PRIVATE KEY-----"
_MAGIC = b"openssh-key-v1\x00"
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


class ConvertError(ValueError):
    pass


def _polymod(values: List[int]) -> int:
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            if (top >> i) & 1:
                chk ^= _GENERATOR[i]
    return chk


def _hrp_expand(hrp: str) -> List[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _to_5bit(data: bytes) -> List[int]:
    acc = bits = 0
    out = []
    for byte in data:
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append((acc >> bits) & 31)
    if bits:
        out.append((acc << (5 - bits)) & 31)
    return out


def bech32_encode(hrp: str, data: bytes) -> str:
    """BIP-173 bech32 (not bech32m), without the 90-character limit, as age uses."""
    hrp = hrp.lower()
    values = _to_5bit(data)
    mod = _polymod(_hrp_expand(hrp) + values + [0] * 6) ^ 1
    checksum = [(mod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[v] for v in values + checksum)


def _read_string(buf: bytes, off: int) -> Tuple[bytes, int]:
    if off + 4 > len(buf):
        raise ConvertError("truncated key")
    (n,) = struct.unpack(">I", buf[off : off + 4])
    end = off + 4 + n
    if end > len(buf):
        raise ConvertError("truncated key")
    return buf[off + 4 : end], end


def _ed25519_seed(data: bytes) -> bytes:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise ConvertError("not an OpenSSH private key") from None
    lines = [line.strip() for line in text.strip().splitlines()]
    if len(lines) < 3 or lines[0] != _BEGIN or lines[-1] != _END:
        raise ConvertError("not an OpenSSH private key")
    try:
        blob = base64.b64decode("".join(lines[1:-1]), validate=True)
    except binascii.Error:
        raise ConvertError("invalid base64 in key") from None
    if not blob.startswith(_MAGIC):
        raise ConvertError("not an openssh-key-v1 key")
    off = len(_MAGIC)
    cipher, off = _read_string(blob, off)
    kdf, off = _read_string(blob, off)
    _kdf_options, off = _read_string(blob, off)
    if cipher != b"none" or kdf != b"none":
        raise ConvertError("passphrase-protected keys are not supported")
    if off + 4 > len(blob):
        raise ConvertError("truncated key")
    (count,) = struct.unpack(">I", blob[off : off + 4])
    off += 4
    if count != 1:
        raise ConvertError(f"expected one key, found {count}")
    _public, off = _read_string(blob, off)
    private, _ = _read_string(blob, off)
    if len(private) < 8 or private[:4] != private[4:8]:
        raise ConvertError("corrupt private section")
    key_type, p = _read_string(private, 8)
    if key_type != b"ssh-ed25519":
        shown = key_type[:32].decode("ascii", "replace")
        raise ConvertError(f"only ed25519 keys are supported, got {shown!r}")
    public, p = _read_string(private, p)
    secret, p = _read_string(private, p)
    if len(public) != 32 or len(secret) != 64 or secret[32:] != public:
        raise ConvertError("corrupt ed25519 key")
    return secret[:32]


def private_key_to_age(data: bytes) -> str:
    """``AGE-SECRET-KEY-1...`` for an unencrypted OpenSSH ed25519 private key."""
    scalar = hashlib.sha512(_ed25519_seed(data)).digest()[:32]
    return bech32_encode("AGE-SECRET-KEY-", scalar).upper()
