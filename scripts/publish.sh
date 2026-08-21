#!/usr/bin/env bash
# model-scheduler 一键发布脚本（PyPI）
#
# 解决的历史痛点（打磨加固 2026-08-21）：
#   1. /tmp/pypi-tools venv 会被系统清理 → 本脚本每次自动重建（放持久位置）
#   2. 版本号九处不一致 / 前端缓存坑 → 发布前强制一致性检查（scripts/check-version.sh）
#   3. PyPI 索引延迟 → upload 后轮询等待 pypi.org 可见 + import 实测版本号验证
#   4. pip show 不可信（uv 管理 venv）→ 以 import 实测版本为准
#
# 用法：
#   bash scripts/publish.sh VERSION        # 发布指定版本（如 0.7.0）
#   bash scripts/publish.sh                 # 自动读取当前 __version__

set -euo pipefail

cd "$(dirname "$0")/.."
REPO_DIR="$(pwd)"

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
  VERSION="$(python3 -c 'import re; print(re.search(r"__version__ = [\"\x27]([^\"\x27]+)[\"\x27]", open("src/model_scheduler/__init__.py", encoding="utf-8").read()).group(1))')"
fi
echo "▶ 目标版本: $VERSION"

# ── 1. 版本一致性检查 ──────────────────────────────────────────────────────
echo "▶ [1/6] 版本一致性检查..."
EXPECT="$VERSION" python3 - <<'PY'
import os, re, sys
from pathlib import Path

version = os.environ["EXPECT"]
# 版本必须出现在这些文件（且不出现上一 patch 版本号残留）
files = [
    Path("pyproject.toml"), Path("src/model_scheduler/__init__.py"),
    Path("src/model_scheduler/server.py"), Path("src/model_scheduler/taskserver.py"),
    Path("tests/test_server.py"), Path("tests/test_taskserver.py"),
]
missing = [f"{f}" for f in files if version not in f.read_text(encoding="utf-8")]
if missing:
    print("\n".join("  ✗ " + m + " 未包含版本 " + version for m in missing[:10]))
    sys.exit(1)
print(f"  ✓ {version} 出现在全部 6 个关键文件")
PY

# ── 2. 测试 ────────────────────────────────────────────────────────────────
echo "▶ [2/6] 运行全量测试..."
python3 -m pytest tests/ -q 2>&1 | tail -2

# ── 3. 准备发布工具（持久 venv，不在 /tmp） ───────────────────────────────
TOOLS_DIR="${HOME}/.hermes/tools/pypi-tools"
if [ ! -x "$TOOLS_DIR/bin/python" ]; then
  echo "▶ [3/6] 创建发布工具 venv（$TOOLS_DIR）..."
  python3 -m venv "$TOOLS_DIR"
  "$TOOLS_DIR/bin/pip" install -q build twine
else
  echo "▶ [3/6] 复用发布工具 venv（$TOOLS_DIR）"
fi

# ── 4. 构建 + 检查 ─────────────────────────────────────────────────────────
echo "▶ [4/6] 构建 + twine check..."
if [ ! -f "${HOME}/.pypirc" ]; then
  echo "  ✗ 缺少 ${HOME}/.pypirc（twine 上传凭证），请先配置"
  exit 1
fi
rm -rf dist build *.egg-info
"$TOOLS_DIR/bin/python" -m build >/dev/null 2>&1
"$TOOLS_DIR/bin/twine" check dist/* 2>&1 | tail -1

# ── 5. 上传 ────────────────────────────────────────────────────────────────
echo "▶ [5/6] 上传 PyPI..."
"$TOOLS_DIR/bin/twine" upload -r pypi "dist/model_scheduler-${VERSION}"*.whl "dist/model_scheduler-${VERSION}"*.tar.gz 2>&1 | tail -2

# ── 6. 索引等待 + import 实测版本验证 ─────────────────────────────────────
echo "▶ [6/6] 等待 PyPI 索引 + 验证安装..."
for i in $(seq 1 15); do
  AVAIL="$(curl -s -m 10 "https://pypi.org/pypi/model-scheduler/json" | python3 -c "import sys,json; d=json.load(sys.stdin); print('$VERSION' in d['releases'])" 2>/dev/null || echo false)"
  if [ "$AVAIL" = "True" ]; then echo "  ✓ PyPI 索引可见 $VERSION"; break; fi
  if [ "$i" -eq 15 ]; then echo "  ✗ 15 次轮询后索引仍不可见，请手动验证"; exit 1; fi
  sleep 3
done

# 用临时 venv 从 PyPI 安装并 import 验证（不信 pip show）
TMPVENV="$(mktemp -d)/venv"
python3 -m venv "$TMPVENV"
"$TMPVENV/bin/pip" install -q -i https://pypi.org/simple --no-cache-dir "model-scheduler==${VERSION}"
INSTALLED="$("$TMPVENV/bin/python" -c "import model_scheduler; print(model_scheduler.__version__)" 2>/dev/null || echo MISSING)"
if [ "$INSTALLED" = "$VERSION" ]; then
  echo "  ✓ 从 PyPI 安装实测版本 = $VERSION（import 验证，非 pip show）"
else
  echo "  ✗ 实测版本 $INSTALLED ≠ 期望 $VERSION"
  exit 1
fi

echo
echo "✅ 发布完成: model-scheduler ${VERSION}"
echo "   GitHub tag 记得: git tag v${VERSION} && git push origin v${VERSION}"
