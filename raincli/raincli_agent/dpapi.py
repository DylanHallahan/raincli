"""Windows DPAPI for the stored credential (protocol 15.3). Standard library only.

The token is protected with ``CryptProtectData`` in the CurrentUser scope with
the fixed entropy below, so only the same Windows user on the same computer can
unprotect it. Off Windows there is no backend: ``protect``/``unprotect`` raise,
and tests install a fake through ``set_backend``.
"""
import base64
import binascii
import os

from .errors import ConfigError

ENTROPY = b"raincli-agent-v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x1

FOREIGN = ("the stored credential was protected for another Windows user or computer "
           "and cannot be read here; sign in again with: raincli login --force")


class DpapiError(ConfigError):
    pass


class _Windows:
    """``CryptProtectData``/``CryptUnprotectData`` through ctypes."""

    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w

        class Blob(c.Structure):
            _fields_ = [("size", w.DWORD), ("data", c.POINTER(c.c_char))]

        crypt = c.WinDLL("crypt32", use_last_error=True)
        kernel = c.WinDLL("kernel32", use_last_error=True)
        self.c, self.Blob = c, Blob
        self._protect = crypt.CryptProtectData
        self._unprotect = crypt.CryptUnprotectData
        for f in (self._protect, self._unprotect):
            f.restype = w.BOOL
        self._protect.argtypes = [c.POINTER(Blob), w.LPCWSTR, c.POINTER(Blob), c.c_void_p, c.c_void_p,
                                  w.DWORD, c.POINTER(Blob)]
        self._unprotect.argtypes = [c.POINTER(Blob), c.c_void_p, c.POINTER(Blob), c.c_void_p, c.c_void_p,
                                    w.DWORD, c.POINTER(Blob)]
        self._free = kernel.LocalFree
        self._free.argtypes, self._free.restype = [c.c_void_p], c.c_void_p

    def _blob(self, data):
        buf = self.c.create_string_buffer(data, len(data))
        return self.Blob(len(data), self.c.cast(buf, self.c.POINTER(self.c.c_char))), buf

    def _call(self, func, data, *middle):
        source, keep1 = self._blob(data)
        entropy, keep2 = self._blob(ENTROPY)
        out = self.Blob()
        if not func(self.c.byref(source), *middle, self.c.byref(entropy), None, None,
                    CRYPTPROTECT_UI_FORBIDDEN, self.c.byref(out)):
            return None
        try:
            return self.c.string_at(out.data, out.size)
        finally:
            self._free(self.c.cast(out.data, self.c.c_void_p))

    def protect(self, data):
        result = self._call(self._protect, data, "raincli")
        if result is None:
            raise DpapiError("Windows could not protect the credential (CryptProtectData failed)")
        return result

    def unprotect(self, data):
        result = self._call(self._unprotect, data, None)
        if result is None:
            raise DpapiError(FOREIGN)
        return result


_backend = None


def set_backend(backend):
    """Install a backend with ``protect``/``unprotect`` (tests); None restores the default."""
    global _backend
    _backend = backend


def backend():
    global _backend
    if _backend is None:
        if os.name != "nt":
            raise DpapiError("DPAPI-protected credentials can only be read on Windows")
        _backend = _Windows()
    return _backend


def protect_token(token):
    """``token`` (str) -> base64 text of the DPAPI blob."""
    return base64.b64encode(backend().protect(token.encode("ascii"))).decode("ascii")


def unprotect_token(text):
    """base64 text of a DPAPI blob -> token (str). A blob from another user or
    computer, or a damaged one, fails with the "sign in again" error."""
    if not isinstance(text, str) or not text:
        raise DpapiError("token_dpapi must be a non-empty base64 string")
    try:
        blob = base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise DpapiError("token_dpapi is not valid base64; " + FOREIGN.split("; ")[1]) from None
    data = backend().unprotect(blob)
    try:
        return data.decode("ascii")
    except UnicodeDecodeError:
        raise DpapiError(FOREIGN) from None
