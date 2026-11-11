"""Throwaway ed25519 test key, assembled at runtime in openssh-key-v1 form.

The expected age identity was produced by ``ssh-to-age -private-key`` from
the same key.
"""

import base64
import struct

SEED = bytes.fromhex("4cd3d7b5b4c05569c7852daf4570ed338aad08e777f2c7272ec1514e75b02609")
PUBLIC = bytes.fromhex("caf5c36c3bd4622e973538bb1a723efba4962a056b11673b7dfa04313284cf59")
AGE_KEY = "AGE-SECRET-KEY-10SQL6LLG3ZV0QNNP7V6USD92EZ8X6L589U0HCKLCUWSHT66YENDQET7R96"


def _s(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def openssh_key(
    seed=SEED,
    public=PUBLIC,
    key_type=b"ssh-ed25519",
    cipher=b"none",
    kdf=b"none",
    check2=None,
    secret=None,
) -> bytes:
    pub_blob = _s(key_type) + _s(public)
    check = b"\x01\x02\x03\x04"
    private = check + (check if check2 is None else check2)
    private += (
        _s(key_type)
        + _s(public)
        + _s(seed + public if secret is None else secret)
        + _s(b"throwaway")
    )
    private += bytes(range(1, 1 + (-len(private) % 8)))
    blob = b"openssh-key-v1\x00" + _s(cipher) + _s(kdf) + _s(b"")
    blob += struct.pack(">I", 1) + _s(pub_blob) + _s(private)
    b64 = base64.b64encode(blob).decode()
    body = "\n".join(b64[i : i + 70] for i in range(0, len(b64), 70))
    return (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n" + body + "\n-----END OPENSSH PRIVATE KEY-----\n"
    ).encode()
