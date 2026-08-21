"""版本一致性守卫测试。

背景（打磨加固 2026-08-21）：历史上多次出现「改了代码但版本号没 bump」，
导致 PyPI 索引/浏览器缓存加载旧版本，用户看不到新功能。本测试在常规测试
套件内强制执行版本一致性——版本号必须同时出现在 6 个关键文件。

注意：本测试读取当前源码的 __version__，只要求「所有关键文件包含同一版本」，
不要求版本号递增（递增由发布流程 publish.sh 负责）。
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

KEY_FILES = [
    "pyproject.toml",
    "src/model_scheduler/__init__.py",
    "src/model_scheduler/server.py",
    "src/model_scheduler/taskserver.py",
    "tests/test_server.py",
    "tests/test_taskserver.py",
]


def current_version() -> str:
    init = (REPO / "src/model_scheduler/__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', init)
    if not m:
        raise AssertionError("未找到 __version__")
    return m.group(1)


class VersionConsistencyTests(unittest.TestCase):
    def test_version_present_in_all_key_files(self):
        version = current_version()
        missing = [f for f in KEY_FILES if version not in (REPO / f).read_text(encoding="utf-8")]
        self.assertEqual(
            [],
            missing,
            f"版本 {version} 未出现在以下关键文件（改代码必须 bump 版本，否则缓存/索引加载旧版）: {missing}",
        )

    def test_no_stale_previous_patch_in_source(self):
        """源码/测试不得残留上一个 patch 版本（如当前 0.6.2 时不得有 0.6.1 于关键文件）。"""
        version = current_version()
        parts = version.split(".")
        if len(parts) < 3:
            return  # 非语义版本，跳过
        stale = f"{parts[0]}.{parts[1]}.{int(parts[2]) - 1}"
        if int(parts[2]) == 0:
            return  # minor 首版无需查 patch 残留
        for f in KEY_FILES:
            txt = (REPO / f).read_text(encoding="utf-8")
            # 允许 README/docs 提到历史版本；关键文件里不应有 stale patch 版本号
            self.assertNotIn(
                stale,
                txt,
                f"{f} 残留上一版本 {stale}（应为 {version}；版本号必须同步 bump）",
            )


if __name__ == "__main__":
    unittest.main()
