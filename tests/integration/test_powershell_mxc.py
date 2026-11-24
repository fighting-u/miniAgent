import json
import time
import unittest
from functools import partial

from src.tools.local_tools import Plan, run_powershell
from src.tools.tool import ToolContext
from tests.temp_workspace import temporary_workspace


class PowerShellMxcTest(unittest.TestCase):
    """直接调用本机 MXC 工具，测试目录由宿主统一清理。"""

    def test_directory_permissions(self):
        """三个授权来源均可读写，未授权的兄弟目录不能读写。"""
        with temporary_workspace("mxc-") as root:
            configured, turn, work, outside = (root / name for name in ("配置 目录", "turn", "work", "outside"))
            for directory in (configured, turn, work, outside):
                directory.mkdir()
            run = partial(run_powershell, context=ToolContext(Plan(), turn), timeout=30,
                          allowed_dirs=(configured, turn), workdir=str(work))
            for directory in (configured, turn, work):
                target = directory / "probe.txt"
                path = str(target).replace("'", "''")
                result = run(f"Set-Content -LiteralPath '{path}' -Value '通过' -Encoding UTF8; "
                             f"Get-Content -LiteralPath '{path}'")
                self.assertFalse(result.is_error, result.content)
                self.assertEqual(json.loads(result.content[0].text)["stdout"].strip(), "通过")
                self.assertEqual(target.read_text(encoding="utf-8-sig").strip(), "通过")

            secret = outside / "private.txt"
            secret.write_text("private-value", encoding="utf-8")
            blocked = outside / "blocked.txt"
            for cmd, target in (("Get-Content", secret), ("Set-Content", blocked)):
                path = str(target).replace("'", "''")
                command = f"{cmd} -LiteralPath '{path}'" + (" -Value 'x'" if cmd == "Set-Content" else "")
                result = run(command + "; Write-Output '继续'")
                output = json.loads(result.content[0].text)
                self.assertTrue(result.is_error, output)
                self.assertIn("权限不足", output["error"])
                self.assertEqual(output["stdout"].strip(), "继续")
                self.assertFalse(blocked.exists())

    def test_timeout(self):
        """调用方超时退出后，内层 PowerShell 不应继续写文件。"""
        with temporary_workspace("mxc-timeout-") as root:
            result = run_powershell("Set-Content started 1; Start-Sleep 5; Set-Content late 1",
                                    timeout=3, allowed_dirs=(root,), context=ToolContext(Plan(), root))
            self.assertTrue(result.is_error, result.content)
            self.assertIn("超过", json.loads(result.content[0].text)["error"])
            self.assertTrue((root / "started").exists())
            time.sleep(5)
            self.assertFalse((root / "late").exists())
