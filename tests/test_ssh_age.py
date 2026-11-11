import base64
import os
import shutil
import subprocess
import tempfile
import unittest

import ssh_keys

from tools.user import ssh_age


def _truncated(pem: bytes, size: int) -> bytes:
    lines = pem.decode().strip().splitlines()
    blob = base64.b64decode("".join(lines[1:-1]))[:size]
    return "\n".join([lines[0], base64.b64encode(blob).decode(), lines[-1]]).encode()


class TestSshAge(unittest.TestCase):
    def test_matches_ssh_to_age(self):
        self.assertEqual(ssh_age.private_key_to_age(ssh_keys.openssh_key()), ssh_keys.AGE_KEY)

    def test_crlf_and_surrounding_whitespace(self):
        key = b"\n" + ssh_keys.openssh_key().replace(b"\n", b"\r\n") + b"\n\n"
        self.assertEqual(ssh_age.private_key_to_age(key), ssh_keys.AGE_KEY)

    def test_bech32_reference_vector(self):
        # BIP-173 valid test string "a12uel5l": hrp "a", empty data.
        self.assertEqual(ssh_age.bech32_encode("A", b""), "a12uel5l")

    def test_rejected_keys(self):
        cases = {
            "passphrase": ssh_keys.openssh_key(cipher=b"aes256-ctr", kdf=b"bcrypt"),
            "only ed25519": ssh_keys.openssh_key(key_type=b"ssh-rsa"),
            "corrupt private section": ssh_keys.openssh_key(check2=b"\x00\x00\x00\x00"),
            "corrupt ed25519": ssh_keys.openssh_key(secret=ssh_keys.SEED + bytes(32)),
            "not an OpenSSH": (
                b"-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----\n"
            ),
            "invalid base64": (
                b"-----BEGIN OPENSSH PRIVATE KEY-----\n!!\n-----END OPENSSH PRIVATE KEY-----\n"
            ),
            "truncated": _truncated(ssh_keys.openssh_key(), 150),
        }
        for why, data in cases.items():
            with self.assertRaises(ssh_age.ConvertError, msg=why) as ctx:
                ssh_age.private_key_to_age(data)
            self.assertIn(why.split()[0], str(ctx.exception).replace("-", " "), why)
        with self.assertRaisesRegex(ssh_age.ConvertError, "not an OpenSSH"):
            ssh_age.private_key_to_age(b"\xff\xfe")

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen not available")
    def test_ssh_keygen_output_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "id")
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", path],
                check=True,
                stdin=subprocess.DEVNULL,
            )
            with open(path, "rb") as f:
                key = ssh_age.private_key_to_age(f.read())
        self.assertRegex(key, r"^AGE-SECRET-KEY-1[QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L]{58}$")


if __name__ == "__main__":
    unittest.main()
