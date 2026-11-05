"""Strict explicit relayer credentials, bound to the complete public signer."""
import base64
import binascii
import re

from polymarket.auth import BuilderApiKey, RelayerApiKey


def parse_relayer_credentials(payload, expected_signer):
    """Return the pinned SDK key without guessing schemes or exposing input."""
    def refuse():
        raise ValueError('invalid relayer credentials') from None

    if not isinstance(payload, dict):
        refuse()
    builder = set(payload) == {'kind', 'key', 'secret', 'passphrase', 'address'}
    if not builder and set(payload) != {'key', 'address'}:
        refuse()
    if builder and payload['kind'] != 'builder':
        refuse()
    if any(not isinstance(value, str) or not value.strip() or
           any(control in value for control in ('\r', '\n', '\0'))
           for value in payload.values()):
        refuse()
    if (not isinstance(expected_signer, str) or
            not re.fullmatch(r'0x[0-9a-fA-F]{40}', expected_signer) or
            not re.fullmatch(r'0x[0-9a-fA-F]{40}', payload['address']) or
            payload['address'].lower() != expected_signer.lower()):
        refuse()
    if builder:
        secret = payload['secret']
        if not re.fullmatch(r'[A-Za-z0-9+/_-]+={0,2}', secret):
            refuse()
        try:
            material = base64.b64decode(secret + '=' * (-len(secret) % 4), altchars=b'-_', validate=True)
        except (ValueError, binascii.Error):
            refuse()
        if not material:
            refuse()
        return BuilderApiKey(key=payload['key'], secret=secret, passphrase=payload['passphrase'])
    return RelayerApiKey(key=payload['key'], address=payload['address'])
