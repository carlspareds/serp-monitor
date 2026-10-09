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
    if not stored:
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
    computed = hashlib.pbkdf2_hmac(_HASH_NAME, password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(computed, expected)
