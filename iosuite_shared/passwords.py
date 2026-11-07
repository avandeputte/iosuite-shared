"""Password hashing: scrypt, with the parameters stored in the hash (scrypt2$n$r$p$salt$hash) so they can be raised
later: a hash made with other parameters is replaced the next time the password is seen (needs_rehash).
"""
import base64
import hashlib
import hmac
import secrets

SCRYPT = (2 ** 16, 8, 1)  # n, r, p: 64 MiB per hash (OWASP's floor is 2^17 x 8 x 1; 2^16 keeps a small container responsive)


def hash_password(pw: str) -> str:
    n, r, p = SCRYPT
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=256 * 1024 * 1024)
    return f"scrypt2${n}${r}${p}$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def check_password(pw: str, stored: str) -> bool:
    """Whether pw matches the stored hash; False, never an exception, for a malformed hash."""
    try:
        kind, n, r, p, s, h = stored.split("$")
        if kind != "scrypt2":
            return False
        n, r, p = int(n), int(r), int(p)
        got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(s), n=n, r=r, p=p, dklen=32, maxmem=256 * 1024 * 1024)
        return hmac.compare_digest(got, base64.b64decode(h))
    except Exception:
        return False


def needs_rehash(stored: str) -> bool:
    parts = (stored or "").split("$")
    try:
        return bool(stored) and (parts[0] != "scrypt2" or tuple(int(x) for x in parts[1:4]) != SCRYPT)
    except ValueError:
        return True


_DUMMY = None


def burn(pw: str):
    """The work of a real check, for an unknown name: a miss costs the same as a wrong password."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("not a real password")
    check_password(pw, _DUMMY)
