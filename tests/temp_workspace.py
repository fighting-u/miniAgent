import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4


# Windows 沙箱中继承 tests 目录权限，避免 tempfile 的私有 ACL 导致无法再次访问。
TEST_ROOT = Path(__file__).resolve().parent


@contextmanager
def temporary_workspace(prefix: str) -> Iterator[Path]:
    """管理仅属于本次测试的目录，成功或异常都递归清理。"""
    root = (TEST_ROOT / f"{prefix}{uuid4().hex}").resolve()
    if root.parent != TEST_ROOT.resolve():
        raise ValueError("临时目录必须直接位于 tests 下")
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)
