"""Tests for item C21a (deep dive §C21): password hashing for the
`users` table.

Arbitrated decision from the brief (not discussed here, only verified):
stdlib `hashlib.pbkdf2_hmac` (sha256, >=600,000 iterations, 16-byte salt
from `secrets`), format `pbkdf2_sha256$<iters>$<salt_b64>$<hash_b64>`,
verification with `hmac.compare_digest` -- argon2/bcrypt would require a
compiled dependency, impractical on low-memory ARM64 hosts (principle 4:
stdlib-first). The algorithm prefix in the stored format is deliberate:
it allows migrating to argon2 in the future without touching already
saved data (a verify_password that recognizes several prefixes, without
rewriting existing rows)."""
from auth.passwords import hash_password, verify_password


def test_hash_password_returns_pbkdf2_sha256_format_with_at_least_600k_iterations():
    stored = hash_password("correct horse battery staple")
    parts = stored.split("$")
    assert len(parts) == 4
    algo, iters, salt_b64, hash_b64 = parts
    assert algo == "pbkdf2_sha256"
    assert int(iters) >= 600_000
    assert salt_b64 and hash_b64


def test_verify_password_accepts_the_correct_password():
    stored = hash_password("s3cr3t!")
    assert verify_password("s3cr3t!", stored) is True


def test_verify_password_rejects_the_wrong_password():
    stored = hash_password("s3cr3t!")
    assert verify_password("wrong", stored) is False


def test_verify_password_rejects_garbage_or_malformed_stored_values():
    assert verify_password("anything", "") is False
    assert verify_password("anything", "not-a-hash") is False
    assert verify_password("anything", "pbkdf2_sha256$bad$salt") is False
    assert verify_password("anything", None) is False


def test_stored_hash_never_contains_the_plaintext_password():
    password = "MySuperSecretPassword123"
    stored = hash_password(password)
    assert password not in stored


def test_two_hashes_of_the_same_password_are_different_salt_makes_them_unique():
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b
    # but both verify correctly against the same plaintext password
    assert verify_password("same-password", a) is True
    assert verify_password("same-password", b) is True
