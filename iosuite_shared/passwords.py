"""Password hashing: scrypt, with the parameters stored in the hash so they can be raised later.

Formats accepted: scrypt2$n$r$p$salt$hash (what is made now), and scrypt$salt$hash (the first iopix / ioContacts
format, n=2^14, r=8, p=1), which is replaced with the current one the next time the password is seen.
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
    """Whether pw matches the stored hash (either format); False, never an exception, for a malformed hash."""
    try:
        parts = stored.split("$")
        if parts[0] == "scrypt2":
            n, r, p, s, h = int(parts[1]), int(parts[2]), int(parts[3]), parts[4], parts[5]
        else:
            _, s, h = parts
            n, r, p = 2 ** 14, 8, 1
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
