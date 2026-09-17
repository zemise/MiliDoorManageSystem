"""Minimal argon2 stub so amqtt's anonymous-auth plugin can import pwdlib.

The LicheeRV Nano image has no gcc, so `argon2-cffi` cannot be built.
Only the AnonymousAuthPlugin is used; it imports pwdlib's Argon2Hasher at
module level but never instantiates it. This stub provides just enough for
that import.
"""

DEFAULT_TIME_COST = 3
DEFAULT_MEMORY_COST = 65536
DEFAULT_PARALLELISM = 4
DEFAULT_HASH_LENGTH = 32
DEFAULT_RANDOM_SALT_LENGTH = 16


class Type:
    ID = "id"
    I = "i"
    D = "d"


class PasswordHasher:
    def __init__(self, *a, **k):
        raise NotImplementedError("argon2 stub: only for import compatibility")

    def hash(self, *a, **k):
        raise NotImplementedError

    def verify(self, *a, **k):
        raise NotImplementedError

    def check_needs_rehash(self, *a, **k):
        raise NotImplementedError
