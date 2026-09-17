class Argon2Error(Exception):
    pass


class VerificationError(Argon2Error):
    pass


class InvalidHashError(Argon2Error):
    pass
