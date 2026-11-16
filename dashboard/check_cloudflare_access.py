"""Offline test of signed Cloudflare Access token checks."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from cloudflare_access import AccessVerifier


class AccessTest(unittest.TestCase):
    def test_signature_audience_and_expiration(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        verifier = AccessVerifier('team-example', 'dashboard-audience')
        base = {'iss': verifier.issuer, 'aud': [verifier.audience], 'iat': now,
                'exp': now + 120, 'email': 'owner@example.com'}
        with patch.object(verifier.keys, 'get_signing_key_from_jwt',
                          return_value=SimpleNamespace(key=key.public_key())):
            for updates, expected in (({}, True), ({'aud': ['wrong']}, False),
                                      ({'exp': now - 1}, False), ({'email': ''}, False),
                                      ({'iss': 'https://wrong.example'}, False)):
                token = jwt.encode({**base, **updates}, key, algorithm='RS256')
                self.assertIs(verifier.accepts(token), expected)
            untrusted_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            self.assertFalse(verifier.accepts(jwt.encode(base, untrusted_key, algorithm='RS256')))
            self.assertFalse(verifier.accepts(None))


if __name__ == '__main__':
    unittest.main()
