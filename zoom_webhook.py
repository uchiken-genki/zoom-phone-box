"""Zoom Webhook の URL 検証と署名確認。"""

import hashlib
import hmac
import time


def validation_response(plain_token: str, secret: str) -> dict:
    encrypted = hmac.new(secret.encode("utf-8"), plain_token.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"plainToken": plain_token, "encryptedToken": encrypted}


def verify_signature(body: bytes, timestamp: str, signature: str, secret: str, now: float | None = None) -> bool:
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > 300:
        return False
    message = f"v0:{timestamp}:".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)
