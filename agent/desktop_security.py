# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Owner-local desktop control storage, using SID checks rather than localized ACL text."""

import ctypes
import os
from pathlib import Path
import sys


class WindowsSecurity:
    def __init__(self):
        from ctypes import wintypes

        pointer = ctypes.c_void_p
        self.api = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.LocalFree.argtypes = [pointer]
        self.kernel.LocalFree.restype = pointer
        self.api.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        self.api.OpenProcessToken.restype = wintypes.BOOL
        self.api.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, pointer, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.api.GetTokenInformation.restype = wintypes.BOOL
        self.api.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(pointer)]
        self.api.ConvertSidToStringSidW.restype = wintypes.BOOL
        self.api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(pointer), ctypes.POINTER(wintypes.DWORD)]
        self.api.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        self.api.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                                                  ctypes.POINTER(pointer), pointer, ctypes.POINTER(pointer), pointer, ctypes.POINTER(pointer)]
        self.api.GetNamedSecurityInfoW.restype = wintypes.DWORD
        self.api.GetAce.argtypes = [pointer, wintypes.DWORD, ctypes.POINTER(pointer)]
        self.api.GetAce.restype = wintypes.BOOL
        self.api.GetSecurityDescriptorDacl.argtypes = [pointer, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(pointer), ctypes.POINTER(wintypes.BOOL)]
        self.api.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        self.api.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, pointer, pointer, pointer, pointer]
        self.api.SetNamedSecurityInfoW.restype = wintypes.DWORD

        class Attributes(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("descriptor", pointer), ("inherit", wintypes.BOOL)]

        self.Attributes = Attributes
        self.kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(Attributes)]
        self.kernel.CreateDirectoryW.restype = wintypes.BOOL
        self.user_sid = self.current_user_sid()
        self.trusted = {self.user_sid, "S-1-5-18", "S-1-5-32-544"}

    def sid_string(self, sid):
        if not sid:
            raise PermissionError("The desktop control ACL has no trusted owner.")
        value = ctypes.c_void_p()
        if not self.api.ConvertSidToStringSidW(sid, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(value)
        finally:
            self.kernel.LocalFree(value)

    def current_user_sid(self):
        from ctypes import wintypes
        token, length = wintypes.HANDLE(), wintypes.DWORD()
        if not self.api.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.api.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))  # TokenUser
            if not length.value:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(length.value)
            if not self.api.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
                raise ctypes.WinError(ctypes.get_last_error())
            return self.sid_string(ctypes.c_void_p.from_buffer(buffer).value)
        finally:
            self.kernel.CloseHandle(token)

    def descriptor(self):
        result = ctypes.c_void_p()
        # Protected DACL; all inheritable rights belong to these three SID classes.
        sddl = (f"O:{self.user_sid}D:P(A;OICI;FA;;;{self.user_sid})"
                "(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)")
        if not self.api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(result), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def create_directory(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = self.descriptor()
        try:
            attributes = self.Attributes(ctypes.sizeof(self.Attributes), descriptor, False)
            if not self.kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                code = ctypes.get_last_error()
                if code != 183:  # A concurrent creator still has to pass verify().
                    raise ctypes.WinError(code)
        finally:
            self.kernel.LocalFree(descriptor)

    def protect_new_file(self, path):
        """Only called for a newly created, still-empty staging file; never repair existing data."""
        from ctypes import wintypes
        descriptor, dacl = self.descriptor(), ctypes.c_void_p()
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        try:
            if not self.api.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
                raise ctypes.WinError(ctypes.get_last_error())
            code = self.api.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None)
            if code:
                raise ctypes.WinError(code)
        finally:
            self.kernel.LocalFree(descriptor)

    def verify(self, path):
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        code = self.api.GetNamedSecurityInfoW(str(path), 1, 0x00000005, ctypes.byref(owner), None,
                                              ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if code:
            raise ctypes.WinError(code)
        try:
            if self.sid_string(owner) not in self.trusted or not dacl:
                raise PermissionError("Unsafe desktop control owner or unrestricted Windows DACL.")
            # ACL header: revision, padding, size, ACE count, padding (8 bytes).
            count = ctypes.c_ushort.from_address(dacl.value + 4).value
            for index in range(count):
                ace = ctypes.c_void_p()
                if not self.api.GetAce(dacl, index, ctypes.byref(ace)):
                    raise ctypes.WinError(ctypes.get_last_error())
                kind = ctypes.c_ubyte.from_address(ace.value).value
                if kind == 1:  # ACCESS_DENIED_ACE cannot grant access.
                    continue
                if kind != 0:
                    raise PermissionError("Unsupported Windows DACL entry; use a private desktop control directory.")
                mask = ctypes.c_uint32.from_address(ace.value + 4).value
                if mask and self.sid_string(ace.value + 8) not in self.trusted:
                    raise PermissionError("Unsafe Windows ACL: desktop control permits a SID beyond the current user, SYSTEM and Administrators. Existing ACLs were not changed.")
        finally:
            self.kernel.LocalFree(descriptor)


def private_directory(path):
    path = Path(path).resolve()
    if sys.platform == "win32":
        security = WindowsSecurity()
        if not path.exists():
            security.create_directory(path)
        security.verify(path)
    else:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("Desktop control needs an owner-only directory (mode 0700).")
    return path


def private_file(path):
    path = Path(path)
    if sys.platform == "win32":
        WindowsSecurity().verify(path)
    else:
        info = path.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("Desktop control files must be private to their owner.")


def protect_new_file(path):
    if sys.platform == "win32":
        WindowsSecurity().protect_new_file(Path(path))
