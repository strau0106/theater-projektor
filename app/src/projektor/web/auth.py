"""Password hashing + signed session cookies for the web UI.

Hash format (stdlib only, no extra deps):

    pbkdf2_sha256$<iterations>$<salt b64>$<hash b64>
    scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>

CLI (used by the image build):

    python -m projektor.web.auth hash            # prompts (or reads stdin if not a tty)
    python -m projektor.web.auth hash PASSWORD
    python -m projektor.web.auth verify HASH     # reads password, exit 0 if ok
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import os
import secrets
import sys
from pathlib import Path

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

PBKDF2_ITERATIONS = 200_000
COOKIE_NAME = "projektor_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str, *, method: str = "pbkdf2_sha256", iterations: int = PBKDF2_ITERATIONS) -> str:
    """Return a self-describing hash string for ``password``."""
    salt = secrets.token_bytes(16)
    pw = password.encode("utf-8")
    if method == "pbkdf2_sha256":
        dk = hashlib.pbkdf2_hmac("sha256", pw, salt, iterations)
        return f"pbkdf2_sha256${iterations}${_b64e(salt)}${_b64e(dk)}"
    if method == "scrypt":
        n, r, p = 2**14, 8, 1
        dk = hashlib.scrypt(pw, salt=salt, n=n, r=r, p=p, dklen=32)
        return f"scrypt${n}${r}${p}${_b64e(salt)}${_b64e(dk)}"
    raise ValueError(f"unknown hash method: {method}")


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of ``password`` against a hash from :func:`hash_password`."""
    if not encoded:
        return False
    pw = password.encode("utf-8")
    try:
        parts = encoded.split("$")
        if parts[0] == "pbkdf2_sha256" and len(parts) == 4:
            iterations = int(parts[1])
            salt, expected = _b64d(parts[2]), _b64d(parts[3])
            dk = hashlib.pbkdf2_hmac("sha256", pw, salt, iterations, dklen=len(expected))
        elif parts[0] == "scrypt" and len(parts) == 6:
            n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
            salt, expected = _b64d(parts[4]), _b64d(parts[5])
            dk = hashlib.scrypt(pw, salt=salt, n=n, r=r, p=p, dklen=len(expected), maxmem=128 * 1024 * 1024)
        else:
            return False
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


def load_or_create_secret(path: Path) -> bytes:
    """Read the cookie-signing secret, creating it (mode 0600) on first use."""
    path = Path(path)
    try:
        data = path.read_bytes().strip()
        if len(data) >= 32:
            return data
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32).encode("ascii")
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, secret)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return secret


class SessionManager:
    """Signed, timestamped session tokens.

    The token embeds a fingerprint of the current password hash so that changing
    the password invalidates all existing sessions.
    """

    def __init__(self, secret: bytes, password_hash: str, max_age: int = SESSION_MAX_AGE):
        self.password_hash = password_hash or ""
        self.max_age = max_age
        self._ser = URLSafeTimedSerializer(secret, salt="projektor-web-session")

    @property
    def auth_required(self) -> bool:
        return bool(self.password_hash)

    def _fingerprint(self) -> str:
        return hashlib.sha256(self.password_hash.encode()).hexdigest()[:16]

    def check_password(self, password: str) -> bool:
        return verify_password(password, self.password_hash)

    def issue(self) -> str:
        return self._ser.dumps({"v": 1, "fp": self._fingerprint()})

    def valid(self, token: str | None) -> bool:
        if not self.auth_required:
            return True
        if not token:
            return False
        try:
            data = self._ser.loads(token, max_age=self.max_age)
        except (BadSignature, SignatureExpired):
            return False
        return isinstance(data, dict) and data.get("fp") == self._fingerprint()


def _read_password(prompt: str) -> str:
    if sys.stdin.isatty():
        pw = getpass.getpass(prompt)
        if prompt.startswith("Password") and getpass.getpass("Repeat: ") != pw:
            print("passwords do not match", file=sys.stderr)
            sys.exit(1)
        return pw
    return sys.stdin.readline().rstrip("\r\n")


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m projektor.web.auth", description="Web UI password hashes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hash", help="print a hash for settings [web] password_hash")
    h.add_argument("password", nargs="?", help="password (default: prompt / first stdin line)")
    h.add_argument("--method", choices=["pbkdf2_sha256", "scrypt"], default="pbkdf2_sha256")
    v = sub.add_parser("verify", help="check a password against a hash (exit 0 = match)")
    v.add_argument("hash")
    v.add_argument("password", nargs="?")
    args = ap.parse_args(argv)

    if args.cmd == "hash":
        pw = args.password if args.password is not None else _read_password("Password: ")
        if not pw:
            print("empty password", file=sys.stderr)
            return 1
        print(hash_password(pw, method=args.method))
        return 0
    pw = args.password if args.password is not None else _read_password("Check password: ")
    ok = verify_password(pw, args.hash)
    print("ok" if ok else "mismatch")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
