"""Centralized exception hierarchy.

Every error raised by gateway code derives from :class:`GatewayError` and carries a stable
machine-readable ``code`` used in logs, API responses and audit records.
"""

from __future__ import annotations


class GatewayError(Exception):
    """Base class for all gateway errors."""

    code: str = "GATEWAY_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code

    @property
    def message(self) -> str:
        return str(self)


class ConfigError(GatewayError):
    code = "CONFIG_ERROR"


class TagConfigError(ConfigError):
    code = "TAG_CONFIG_ERROR"


class PlatformNotSupportedError(ConfigError):
    code = "PLATFORM_NOT_SUPPORTED"


class DriverError(GatewayError):
    code = "DRIVER_ERROR"


class DriverConnectionError(DriverError):
    """The connection to the device is lost or cannot be established."""

    code = "CONNECTION_ERROR"


class DriverTimeoutError(DriverConnectionError):
    code = "TIMEOUT"


class UnsupportedOperationError(DriverError):
    code = "UNSUPPORTED_OPERATION"


class AddressError(DriverError):
    code = "ADDRESS_ERROR"


class EncodingError(DriverError):
    code = "ENCODING_ERROR"


class ProtocolError(DriverError):
    """Malformed or unexpected protocol frame."""

    code = "PROTOCOL_ERROR"


class WriteRejectedError(GatewayError):
    """A write request was rejected before reaching the PLC."""

    code = "WRITE_REJECTED"

    def __init__(self, message: str, *, code: str = "WRITE_REJECTED", http_status: int = 403) -> None:
        super().__init__(message, code=code)
        self.http_status = http_status


class DatabaseError(GatewayError):
    code = "DATABASE_ERROR"


class DatabaseUnavailableError(DatabaseError):
    code = "DATABASE_UNAVAILABLE"


class SpoolError(GatewayError):
    code = "SPOOL_ERROR"


class AuthError(GatewayError):
    code = "AUTH_ERROR"
