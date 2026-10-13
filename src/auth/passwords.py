"""Password hashing for the `users` table (C21a, deep dive §C21).

ARBITRATED decision (brief C21a, not reopened here): `hashlib.pbkdf2_hmac`
from the **stdlib** (sha256, >=600,000 iterations -- OWASP 2023+
recommendation for PBKDF2-HMAC-SHA256 --, 16-byte salt from `secrets`). The
original deep dive asked for argon2/bcrypt, but neither is installed in
this repo: they are COMPILED dependencies (C/Rust bindings), and the
app is meant to run on small low-memory ARM64 hosts that may not have a
reliable build toolchain for them -- project principle 4 ("stdlib-first, no new dependencies
without a real need") outweighs following the deep dive to the letter.

Stored format: `pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>`.
The algorithm prefix is deliberate and is the actual output of this
decision: if argon2/bcrypt become viable later on (typical Docker x86_64,
or a cross-compilation toolchain for ARM64 hosts), `verify_password` can
learn to recognize a second prefix (e.g. `argon2id$...`) and migrate the
hashes row by row on each user's next successful login, without touching
or invalidating data already saved in the current format.

Never compare passwords/hashes with `==` -- a byte-comparison shortcut that
leaks a timing oracle over the stored hash. `hmac.compare_digest` is used
instead (same approach as `Database.verify_api_key`, see
src/database/models.py)."""
import base64
import hashlib
import hmac
import secrets

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 600_000
_SALT_BYTES = 16
_HASH_NAME = "sha256"

# Password policy (SECURITY.md, item 10): length is what matters (NIST SP
# 800-63B) -- at least 12 characters, a passphrase of a few words is ideal;
# no composition rules. A short list of the most common choices and the
# username itself are refused. The upper bound keeps one login from
# costing an unbounded PBKDF2 run.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
_COMMON = frozenset({
    "password1234", "123456789012", "qwertyuiopas", "passwordpassword",
    "administrator", "letmeinletmein", "changemechangeme", "serpmonitor123",
    "111111111111", "000000000000", "abcdefghijkl", "iloveyou1234",
})


def password_problem(password: str, username: str = "") -> str | None:
    """None when `password` is acceptable, else a plain reason (never
    echoes the password)."""
    pw = password or ""
    if len(pw) < MIN_PASSWORD_LENGTH:
        return (f"Use at least {MIN_PASSWORD_LENGTH} characters "
                "(a passphrase of a few words works well).")
    if len(pw) > MAX_PASSWORD_LENGTH:
        return f"Use at most {MAX_PASSWORD_LENGTH} characters."
    if len(set(pw)) < 4:
        return "That password is too repetitive."
    low = pw.lower()
    if low in _COMMON or (username and low == (username or "").strip().lower()):
        return "That password is too easy to guess."
    return None


def needs_rehash(stored: str | None) -> bool:
    """True when `stored` was made with weaker parameters than today's (fewer
    iterations, or another algorithm): login then re-hashes the password it
    just verified (Database.authenticate_user), so raising _ITERATIONS
    upgrades every account on its next sign-in."""
    parts = (stored or "").split("$")
    if len(parts) != 4 or parts[0] != _ALGORITHM:
        return True
    try:
        return int(parts[1]) < _ITERATIONS
    except ValueError:
        return True


def hash_password(password: str) -> str:
    """Generates a new hash with a random salt -- two calls with the SAME
    password produce DIFFERENT strings (by design: the salt prevents two
    users with the same password from having the same password_hash in
    the DB, which would reveal the match and ease precomputed dictionary
    attacks)."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(_HASH_NAME, password.encode("utf-8"), salt, _ITERATIONS)
    return (
        f"{_ALGORITHM}${_ITERATIONS}$"
        f"{base64.b64encode(salt).decode('ascii')}${base64.b64encode(digest).decode('ascii')}"
    )


def verify_password(password: str, stored: str | None) -> bool:
    """Verifies `password` against `stored` in the format produced by
    hash_password(). Any malformed, empty, or unknown-algorithm value
    returns False without raising -- the caller (login) must not
    distinguish "corrupt hash" from "wrong password" in its response."""
    if not stored or len(password or "") > MAX_PASSWORD_LENGTH:
        return False
    parts = stored.split("$")
    if len(parts) != 4:
        return False
    algo, iterations_s, salt_b64, digest_b64 = parts
    if algo != _ALGORITHM:
        return False
    try:
        iterations = int(iterations_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    if not 1 <= iterations <= 10_000_000:   # a corrupt row must not hang the login
        return False
    computed = hashlib.pbkdf2_hmac(_HASH_NAME, password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(computed, expected)
