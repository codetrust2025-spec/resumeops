"""Verify Cloudflare Access user tokens for an explicitly configured public origin."""


class AccessVerifier:
    def __init__(self, team, audience):
        import jwt

        self.jwt = jwt
        self.issuer = f'https://{team}.cloudflareaccess.com'
        self.audience = audience
        self.keys = jwt.PyJWKClient(f'{self.issuer}/cdn-cgi/access/certs', timeout=5)

    def accepts(self, token):
        if not token or len(token) > 8192:
            return False
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = self.jwt.decode(
                token, key, algorithms=['RS256'], audience=self.audience, issuer=self.issuer,
                options={'require': ['exp', 'iat', 'iss', 'aud', 'email']},
            )
        except self.jwt.PyJWTError:
            return False
        return bool(claims['email'])
