import base64
import urllib.parse

import pyotp
import pytest

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


def _export_uri(entry: bytes) -> str:
    data = base64.b64encode(_field(1, entry)).decode()
    return "otpauth-migration://offline?data=" + urllib.parse.quote(data)


@pytest.mark.parametrize("field,value", [(4, 2), (4, 3), (4, 4), (5, 2), (5, 3)])
def test_migration_rejects_unsupported_parameters(field, value):
    from dicos.otp import parse_migration
    secret = b"synthetic-test"
    entry = _field(1, secret) + _field(6, 2) + _field(field, value)
    for parse in (parse_migration, parse_otpauth):
        with pytest.raises(ValueError) as error:
            parse(_export_uri(entry))
        assert secret.decode() not in str(error.value)
        assert base64.b32encode(secret).decode().rstrip("=") not in str(error.value)


@pytest.mark.parametrize("value", [None, 0, 1])
def test_migration_accepts_supported_parameters(value):
    entry = _field(1, b"synthetic-test") + _field(6, 2)
    if value is not None:
        entry += _field(4, value) + _field(5, value)
    assert len(parse_otpauth(_export_uri(entry))) == 1


def test_migration_rejects_only_non_totp():
    entry = _field(1, b"synthetic-test") + _field(6, 1)
    with pytest.raises(ValueError):
        parse_otpauth(_export_uri(entry))


@pytest.mark.parametrize("bad", [_field(6, 1), _field(6, 2) + _field(4, 2), _field(6, 2) + _field(5, 2)])
def test_migration_skips_unsupported_entries(bad):
    good = _field(1, b"synthetic-good") + _field(3, b"DiCOS") + _field(6, 2)
    payload = _field(1, _field(1, b"synthetic-bad") + bad) + _field(1, good)
    uri = "otpauth-migration://offline?data=" + urllib.parse.quote(base64.b64encode(payload).decode())
    [account] = parse_otpauth(uri)
    assert account.issuer == "DiCOS"
    assert account.secret == base64.b32encode(b"synthetic-good").decode().rstrip("=")


@pytest.mark.parametrize("kind,query", [
    ("hotp", ""), ("unknown", ""), ("totp", "&algorithm=SHA256"),
    ("totp", "&algorithm=MD5"), ("totp", "&digits=8"), ("totp", "&digits=5"),
])
def test_otpauth_rejects_unsupported_parameters(kind, query):
    secret = "JBSWY3DPEHPK3PXP"
    with pytest.raises(ValueError) as error:
        parse_otpauth(f"otpauth://{kind}/test?secret={secret}{query}")
    assert secret not in str(error.value)


@pytest.mark.parametrize("query", ["", "&algorithm=SHA1&digits=6", "&algorithm=sha1", "&algorithm=ShA1"])
def test_otpauth_accepts_supported_parameters(query):
    assert len(parse_otpauth("otpauth://totp/test?secret=JBSWY3DPEHPK3PXP" + query)) == 1


@pytest.mark.parametrize("payload", [b"\x80", b"\x08", b"\x08\x80", b"\x0a", b"\x0a\x80", b"\x0a\x02x"])
def test_protobuf_rejects_truncated_fields(payload):
    from dicos.otp import _parse_fields
    with pytest.raises(ValueError, match="Truncated protobuf"):
        _parse_fields(payload)
    uri = "otpauth-migration://offline?data=" + urllib.parse.quote(base64.b64encode(payload).decode())
    with pytest.raises(ValueError):
        parse_otpauth(uri)


def test_protobuf_varint_bounds():
    from dicos.otp import _read_varint
    with pytest.raises(ValueError, match="Truncated protobuf varint"):
        _read_varint(b"", 0)
    assert _read_varint(b"\xac\x02", 0) == (300, 2)


def test_migration_raw_plus():
    entry = _field(1, b"\xfe" * 9) + _field(6, 2)
    uri = _export_uri(entry)
    raw_uri = urllib.parse.unquote(uri)
    assert "+" in raw_uri
    assert parse_otpauth(raw_uri) == parse_otpauth(uri)
