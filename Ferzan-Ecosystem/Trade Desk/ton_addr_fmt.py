"""Show a TON wallet in its non-bounceable (UQ...) form.

EQ... and UQ... are two spellings of the SAME wallet. A transfer to the EQ spelling of a wallet that has never been
used is sent back (bounced); the UQ spelling is kept and activates the wallet. Anything shown to a person so they can
send TON to a wallet must therefore be UQ. Contracts and jettons stay as they are: do not pass those through here.
Pure python, no dependencies.
"""
import base64

_BOUNCEABLE = {0x11: 0x51, 0x91: 0xD1}  # mainnet / testnet tag bytes: bounceable -> non-bounceable


def _crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def wallet_form(address: str) -> str:
    """UQ form of a user-friendly wallet address. Anything that is not a valid, checksummed 48-character address is returned unchanged."""
    s = (address or "").strip()
    if len(s) != 48:
        return address
    try:
        raw = base64.urlsafe_b64decode(s)
    except Exception:  # noqa: BLE001
        return address
    if len(raw) != 36 or _crc16(raw[:34]) != int.from_bytes(raw[34:], "big"):
        return address
    if raw[0] not in _BOUNCEABLE:
        return s  # already non-bounceable, or not a plain address tag: leave it
    head = bytes([_BOUNCEABLE[raw[0]]]) + raw[1:34]
    return base64.urlsafe_b64encode(head + _crc16(head).to_bytes(2, "big")).decode()
