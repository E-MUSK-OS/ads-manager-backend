from typing import Optional
from cryptography.fernet import Fernet
from app.config import settings

def _get_fernet(key: Optional[str] = None) -> Fernet:
    effective_key = key or settings.TOKEN_ENCRYPTION_KEY
    if not effective_key:
        raise ValueError("TOKEN_ENCRYPTION_KEY is not configured in settings")
    key_bytes = effective_key.encode("utf-8") if isinstance(effective_key, str) else effective_key
    return Fernet(key_bytes)

def generate_key() -> str:
    """Generate a URL-safe base64-encoded 32-byte Fernet key."""
    return Fernet.generate_key().decode("utf-8")

def encrypt(text: str, key: Optional[str] = None) -> str:
    """Encrypt a plaintext string using AES-CBC-128 / HMAC-SHA256 via Fernet."""
    if not text:
        return ""
    fernet = _get_fernet(key)
    return fernet.encrypt(text.encode("utf-8")).decode("utf-8")

def decrypt(token: str, key: Optional[str] = None) -> str:
    """Decrypt a Fernet cipher string back to plaintext."""
    if not token:
        return ""
    fernet = _get_fernet(key)
    return fernet.decrypt(token.encode("utf-8")).decode("utf-8")

