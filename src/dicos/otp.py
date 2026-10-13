"""TOTP secret extraction from otpauth:// URIs, Google Authenticator export QRs, and images."""

import base64
import time
import urllib.parse
from dataclasses import dataclass, field

import pyotp


@dataclass
class OtpAccount:
    name: str
    issuer: str
    secret: str = field(repr=False)  # base32


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(buf):
            raise ValueError("Truncated protobuf varint.")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _parse_fields(buf: bytes) -> list[tuple[int, int | bytes]]:
    """Minimal protobuf wire-format parser (varint and length-delimited only)."""
    fields = []
    i = 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _read_varint(buf, i)
        elif wire == 2:
            n, i = _read_varint(buf, i)
            if n > len(buf) - i:
                raise ValueError("Truncated protobuf length-delimited field.")
            val, i = buf[i : i + n], i + n
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        fields.append((num, val))
    return fields


def _parse_migration(uri: str) -> list[OtpAccount]:
    """Decode otpauth-migration://offline?data=... (Google Authenticator export)."""
    data = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)["data"][0].replace(" ", "+")
    payload = base64.b64decode(data + "=" * (-len(data) % 4))
    accounts = []
    for num, val in _parse_fields(payload):
        if num != 1:
            continue
        f = dict(_parse_fields(val))
        # Skip non-TOTP entries. Reject algorithms/digits other than unspecified or SHA1/SIX.
        if f.get(6, 2) != 2:
            continue
        if f.get(4, 0) not in (0, 1) or f.get(5, 0) not in (0, 1):
            raise ValueError("Unsupported TOTP algorithm or digit count.")
        accounts.append(
            OtpAccount(
                name=f.get(2, b"").decode(),
                issuer=f.get(3, b"").decode().strip(),
                secret=base64.b32encode(f[1]).decode().rstrip("="),
            )
        )
    return accounts


def _parse_otpauth(uri: str) -> list[OtpAccount]:
    """Accept otpauth://totp/..., otpauth-migration://..., or a bare base32 secret."""
    uri = uri.strip()
    if uri.startswith("otpauth-migration://"):
        return parse_migration(uri)
    if uri.startswith("otpauth://"):
        p = urllib.parse.urlparse(uri)
        q = urllib.parse.parse_qs(p.query)
        if p.netloc != "totp":
            raise ValueError("Unsupported OTP type.")
        if q.get("algorithm", ["SHA1"])[0].upper() != "SHA1":
            raise ValueError("Unsupported TOTP algorithm.")
        if q.get("digits", ["6"])[0] != "6":
            raise ValueError("Unsupported TOTP digit count.")
        label = urllib.parse.unquote(p.path.lstrip("/"))
        return [OtpAccount(name=label, issuer=q.get("issuer", [""])[0], secret=q["secret"][0])]
    secret = uri.replace(" ", "").upper()
    if not secret:
        raise ValueError("Empty TOTP secret")
    base64.b32decode(secret + "=" * (-len(secret) % 8))  # validate
    return [OtpAccount(name="", issuer="", secret=secret)]


def parse_migration(uri: str) -> list[OtpAccount]:
    try:
        return _parse_migration(uri)
    except (ValueError, KeyError, IndexError, TypeError, UnicodeError):
        raise ValueError("Invalid Google Authenticator export URI.") from None


def parse_otpauth(uri: str) -> list[OtpAccount]:
    try:
        accounts = _parse_otpauth(uri)
        for account in accounts:
            secret = account.secret.replace(" ", "").upper()
            if not secret:
                raise ValueError
            base64.b32decode(secret + "=" * (-len(secret) % 8))
            account.secret = secret
        return accounts
    except (ValueError, KeyError, IndexError, TypeError, UnicodeError):
        raise ValueError("Invalid TOTP secret or URI.") from None


def decode_qr(path: str) -> str:
    import zxingcpp
    from PIL import Image

    results = zxingcpp.read_barcodes(Image.open(path))
    if not results:
        raise ValueError(f"no QR code found in {path}")
    return results[0].text


def current_code(secret: str, min_remaining: int = 3) -> str:
    """Return the current TOTP, waiting for the next window if this one is about to expire."""
    totp = pyotp.TOTP(secret)
    remaining = totp.interval - time.time() % totp.interval
    if remaining < min_remaining:
        time.sleep(remaining + 0.2)
    return totp.now()
