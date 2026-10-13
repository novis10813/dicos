import base64
import urllib.parse

import pyotp

from dicos.otp import parse_otpauth


def _field(num: int, val: bytes | int) -> bytes:
    if isinstance(val, int):
        return bytes([num << 3 | 0, val])
    return bytes([num << 3 | 2, len(val)]) + val


def _migration_uri(entries: list[tuple[bytes, str, str]]) -> str:
    payload = b""
    for secret, name, issuer in entries:
        otp = _field(1, secret) + _field(2, name.encode()) + _field(3, issuer.encode())
        otp += _field(4, 1) + _field(5, 1) + _field(6, 2)
        payload += _field(1, otp)
    payload += _field(2, 1)
    data = urllib.parse.quote(base64.b64encode(payload).decode())
    return f"otpauth-migration://offline?data={data}"


def test_migration_single_account():
    secret = bytes(range(10))
    [acc] = parse_otpauth(_migration_uri([(secret, "jack@example.org", "DiCOS 2FA APP ")]))
    assert acc.name == "jack@example.org"
    assert acc.issuer == "DiCOS 2FA APP"
    assert acc.secret == base64.b32encode(secret).decode().rstrip("=")
    pyotp.TOTP(acc.secret).now()  # secret is usable


def test_migration_multiple_accounts():
    accs = parse_otpauth(_migration_uri([(b"a" * 10, "x", "GitHub"), (b"b" * 10, "y", "DiCOS")]))
    assert [a.issuer for a in accs] == ["GitHub", "DiCOS"]


def test_otpauth_uri():
    [acc] = parse_otpauth("otpauth://totp/DiCOS:jack?secret=JBSWY3DPEHPK3PXP&issuer=DiCOS")
    assert acc.secret == "JBSWY3DPEHPK3PXP"
    assert acc.issuer == "DiCOS"


def test_bare_secret():
    [acc] = parse_otpauth("jbsw y3dp ehpk 3pxp")
    assert acc.secret == "JBSWY3DPEHPK3PXP"


def test_current_code_wait(monkeypatch):
    from dicos import otp
    slept = []
    monkeypatch.setattr(otp.time, "time", lambda: 29)
    monkeypatch.setattr(otp.time, "sleep", slept.append)
    assert len(otp.current_code("JBSWY3DPEHPK3PXP")) == 6
    assert slept == [1.2]
    slept.clear()
    monkeypatch.setattr(otp.time, "time", lambda: 27)
    otp.current_code("JBSWY3DPEHPK3PXP")
    assert slept == []


def test_bad_inputs_are_sanitized():
    import pytest
    for value in ("", "invalid-sensitive-input", "otpauth://totp/user", "otpauth-migration://offline?data=broken"):
        with pytest.raises(ValueError) as error:
            parse_otpauth(value)
        assert not value or value not in str(error.value)


def test_qr_decode(tmp_path):
    import zxingcpp
    from PIL import Image
    from dicos.otp import decode_qr
    text = "otpauth://totp/test?secret=JBSWY3DPEHPK3PXP"
    barcode = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode)
    path = tmp_path / "qr.png"
    Image.fromarray(zxingcpp.write_barcode_to_image(barcode, scale=4)).save(path)
    assert decode_qr(str(path)) == text
