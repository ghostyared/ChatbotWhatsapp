"""
Cifrado simétrico (Fernet / AES-128-CBC + HMAC) para secretos guardados en BD (ej. ApiEndpoint.token).
Usa ENCRYPTION_KEY de .env si existe, o deriva una clave estable de SECRET_KEY.
Compatible hacia atrás: si un token en BD no tiene prefijo 'enc:', se devuelve tal cual.
"""
import base64
import hashlib
import os

from config import Config

PREFIX = "enc:"


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    raw = (os.environ.get("ENCRYPTION_KEY") or Config.SECRET_KEY or "").strip()
    if not raw:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
    return Fernet(key)


def cifrar_token(valor):
    """Cifra un token antes de guardarlo en BD. Devuelve '' si está vacío."""
    val = (valor or "").strip()
    if not val or val.startswith(PREFIX):
        return val
    f = _fernet()
    if not f:
        return val
    return PREFIX + f.encrypt(val.encode("utf-8")).decode("ascii")


def descifrar_token(valor):
    """Descifra un token leído de BD. Si está en texto plano (legado), lo devuelve tal cual."""
    val = (valor or "").strip()
    if not val or not val.startswith(PREFIX):
        return val
    f = _fernet()
    if not f:
        return ""
    try:
        return f.decrypt(val[len(PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        return ""
