import os
import json
from irys_sdk import Builder


def _get_client():
    raw = os.environ.get("IRYS_UPLOADER_KEY", "").strip()
    if not raw:
        raise RuntimeError("IRYS_UPLOADER_KEY not set in .env")
    return Builder("solana").wallet(raw).build()


def upload_bytes(data: bytes, tags=None) -> str:
    client = _get_client()
    res = client.upload(data, tags=tags or [])
    return f"https://gateway.irys.xyz/{res['id']}"


def upload_token_metadata(name: str, symbol: str, image_url: str, description: str = "") -> str:
    metadata = {
        "name": name,
        "symbol": symbol,
        "description": description,
        "image": image_url,
    }
    payload = json.dumps(metadata).encode("utf-8")
    return upload_bytes(payload, tags=[("Content-Type", "application/json")])
