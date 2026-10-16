import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rush_phone.private_files import PrivateFileError, protect_private_path


class PrivateFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "private.json"
        self.path.touch()

    @unittest.skipIf(os.name == "nt", "POSIX 模式位不等同于 Windows ACL")
    def test_posix_file_and_directory_permissions(self):
        protect_private_path(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        protect_private_path(Path(self.directory.name), directory=True)
        self.assertEqual(Path(self.directory.name).stat().st_mode & 0o777, 0o700)

    def test_windows_uses_current_sid_protected_dacl_and_directory_inheritance(self):
        security = Mock(ACL_REVISION=2, TokenUser=1, SE_FILE_OBJECT=1,
                        DACL_SECURITY_INFORMATION=4, PROTECTED_DACL_SECURITY_INFORMATION=0x80000000)
        token = security.OpenProcessToken.return_value
        security.GetTokenInformation.return_value = ("current-user-sid", 0)
        modules = {"win32security": security, "win32api": Mock(),
                   "win32con": SimpleNamespace(TOKEN_QUERY=8, OBJECT_INHERIT_ACE=1, CONTAINER_INHERIT_ACE=2),
                   "ntsecuritycon": SimpleNamespace(FILE_ALL_ACCESS=0x1F01FF)}
        with patch("rush_phone.private_files.sys.platform", "win32"), patch.dict("sys.modules", modules):
            protect_private_path(self.path)
            security.ACL.return_value.AddAccessAllowedAceEx.assert_called_with(2, 0, 0x1F01FF, "current-user-sid")
            security.SetNamedSecurityInfo.assert_called_with(str(self.path), 1, 0x80000004, None, None, security.ACL.return_value, None)
            protect_private_path(Path(self.directory.name), directory=True)
            security.ACL.return_value.AddAccessAllowedAceEx.assert_called_with(2, 3, 0x1F01FF, "current-user-sid")
        self.assertEqual(token.Close.call_count, 2)

    def test_windows_missing_dependency_stops(self):
        with patch("rush_phone.private_files.sys.platform", "win32"), patch.dict("sys.modules", {"win32security": None}):
            with self.assertRaisesRegex(PrivateFileError, "pip install"):
                protect_private_path(self.path)

    def test_permission_failure_does_not_echo_raw_system_error(self):
        with patch("rush_phone.private_files.sys.platform", "win32"), \
             patch("rush_phone.private_files._protect_windows_path", side_effect=OSError("private-file-data")):
            with self.assertRaises(PrivateFileError) as captured:
                protect_private_path(self.path)
        self.assertNotIn("private-file-data", str(captured.exception))

    def test_wrong_object_type_is_not_modified(self):
        for path, directory in ((self.path, True), (Path(self.directory.name), False), (self.path.with_name("missing"), False)):
            with self.assertRaises(PrivateFileError):
                protect_private_path(path, directory=directory)

    @unittest.skipUnless(sys.platform == "win32", "需在 Windows 上验收真实 NTFS ACL")
    def test_native_windows_acl_has_only_current_user_allow_entry(self):
        import win32api
        import win32con
        import win32security

        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        protect_private_path(self.path)
        sd = win32security.GetNamedSecurityInfo(str(self.path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
        acl = sd.GetSecurityDescriptorDacl()
        self.assertEqual(acl.GetAceCount(), 1)
        self.assertEqual(str(acl.GetAce(0)[2]), str(sid))
        self.assertEqual(acl.GetAce(0)[0][0], win32security.ACCESS_ALLOWED_ACE_TYPE)


if __name__ == "__main__":
    unittest.main()
