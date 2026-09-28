"""Windows file primitives. Imported only on Windows; no third-party modules.

Private objects use a protected DACL at creation (current user, SYSTEM and
Administrators only). Credential reads check the owner and DACL on the open
handle. Reparse points are never accepted as source files.
"""
import ctypes as c
from ctypes import wintypes as w
import errno
import msvcrt
import os
from contextlib import contextmanager
from functools import lru_cache

k = c.WinDLL("kernel32", use_last_error=True)
a = c.WinDLL("advapi32", use_last_error=True)
P = c.c_void_p


def _api(dll, name, args, result=w.BOOL):
    f = getattr(dll, name)
    f.argtypes, f.restype = args, result
    return f


_process = _api(k, "GetCurrentProcess", [], w.HANDLE)
_close = _api(k, "CloseHandle", [w.HANDLE])
_free = _api(k, "LocalFree", [P], P)
_token = _api(a, "OpenProcessToken", [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)])
_token_info = _api(a, "GetTokenInformation", [w.HANDLE, c.c_int, P, w.DWORD, c.POINTER(w.DWORD)])
_sid_string = _api(a, "ConvertSidToStringSidW", [P, c.POINTER(P)])
_sd_string = _api(a, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                  [w.LPCWSTR, w.DWORD, c.POINTER(P), c.POINTER(w.DWORD)])
_security = _api(a, "GetSecurityInfo", [w.HANDLE, c.c_int, w.DWORD,
                 c.POINTER(P), P, c.POINTER(P), P, c.POINTER(P)], w.DWORD)
_get_ace = _api(a, "GetAce", [P, w.DWORD, c.POINTER(P)])
_attributes = _api(k, "GetFileInformationByHandleEx", [w.HANDLE, c.c_int, P, w.DWORD])
_move = _api(k, "MoveFileExW", [w.LPCWSTR, w.LPCWSTR, w.DWORD])


class SecurityAttributes(c.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", P), ("inherit", w.BOOL)]


_create = _api(k, "CreateFileW", [w.LPCWSTR, w.DWORD, w.DWORD,
               c.POINTER(SecurityAttributes), w.DWORD, w.DWORD, w.HANDLE], w.HANDLE)
_mkdir = _api(k, "CreateDirectoryW", [w.LPCWSTR, c.POINTER(SecurityAttributes)])


def _check(ok):
    if not ok:
        raise c.WinError(c.get_last_error())
    return ok


def _sid_text(sid):
    out = P()
    _check(_sid_string(sid, c.byref(out)))
    try:
        return c.wstring_at(out)
    finally:
        _free(out)


@lru_cache(maxsize=1)
def current_sid():
    handle = w.HANDLE()
    _check(_token(_process(), 8, c.byref(handle)))  # TOKEN_QUERY
    try:
        size = w.DWORD()
        _token_info(handle, 1, None, 0, c.byref(size))  # TokenUser
        buf = c.create_string_buffer(size.value)
        _check(_token_info(handle, 1, buf, size, c.byref(size)))
        return _sid_text(c.cast(buf, c.POINTER(P))[0])
    finally:
        _close(handle)


@contextmanager
def private_security(directory=False):
    sid = current_sid()
    inherit = "OICI" if directory else ""
    sddl = f"O:{sid}D:P" + "".join(f"(A;{inherit};FA;;;{s})" for s in (sid, "SY", "BA"))
    sd = P()
    _check(_sd_string(sddl, 1, c.byref(sd), None))
    try:
        yield SecurityAttributes(c.sizeof(SecurityAttributes), sd, False)
    finally:
        _free(sd)


def mkdir_private(path, mode=0o700):
    with private_security(directory=True) as sa:
        _check(_mkdir(os.path.abspath(path), c.byref(sa)))


def _fd(path, access, disposition, sa=None):
    handle = _create(os.path.abspath(path), access, 7, c.byref(sa) if sa else None,
                     disposition, 0x00200000, None)  # OPEN_REPARSE_POINT
    if handle == w.HANDLE(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        info = (w.DWORD * 2)()  # FILE_ATTRIBUTE_TAG_INFO
        _check(_attributes(handle, 9, info, c.sizeof(info)))
        if info[0] & 0x400:
            raise OSError(errno.ELOOP, "reparse point refused", path)
        flags = os.O_BINARY | (os.O_WRONLY if access & 0x40000000 else os.O_RDONLY)
        fd = msvcrt.open_osfhandle(handle, flags)
    except BaseException:
        _close(handle)
        raise
    return fd  # descriptor now owns the handle


def create_private(path, mode=0o600):
    with private_security() as sa:
        return _fd(path, 0x40000000 | 0x20000, 1, sa)  # GENERIC_WRITE|READ_CONTROL, CREATE_NEW


def open_read(path):
    return _fd(path, 0x80000000 | 0x20000, 3)  # GENERIC_READ|READ_CONTROL, OPEN_EXISTING


def check_private(fd):
    owner, dacl, sd = P(), P(), P()
    code = _security(msvcrt.get_osfhandle(fd), 1, 5, c.byref(owner), None,
                     c.byref(dacl), None, c.byref(sd))  # SE_FILE_OBJECT, OWNER|DACL
    if code:
        raise c.WinError(code)
    try:
        if not owner or _sid_text(owner) != current_sid():
            raise PermissionError("credential is not owned by the current Windows user")
        if not dacl:
            raise PermissionError("credential has an unrestricted Windows ACL")
        # ACL header: revision, reserved, size, ACE count, reserved.
        count = c.c_ushort.from_address(dacl.value + 4).value
        trusted = {current_sid(), "S-1-5-18", "S-1-5-32-544"}
        for index in range(count):
            ace = P()
            _check(_get_ace(dacl, index, c.byref(ace)))
            kind, flags = (c.c_ubyte * 2).from_address(ace.value)
            if flags & 8:  # INHERIT_ONLY does not grant access to this file
                continue
            if kind == 1:  # ACCESS_DENIED_ACE only restricts access
                continue
            if kind != 0 or _sid_text(ace.value + 8) not in trusted:
                raise PermissionError("credential ACL grants access outside this user, SYSTEM and Administrators; re-import with config init")
    finally:
        _free(sd)


def replace(source, target):
    _check(_move(os.path.abspath(source), os.path.abspath(target), 1 | 8))
