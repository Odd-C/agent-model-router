"""API contract 文档与 taskserver 路由一致性检查。"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
API_DOC = REPO_ROOT / "docs" / "API.md"
TASKSERVER_SRC = REPO_ROOT / "src" / "agent_model_router" / "taskserver.py"


def test_api_doc_declared_paths_exist_in_taskserver():
    """docs/API.md 中声明的每个 /api 路径，其静态段都应出现在 taskserver.py 中。"""
    assert API_DOC.exists(), "docs/API.md should exist"
    doc = API_DOC.read_text(encoding="utf-8")
    server_src = TASKSERVER_SRC.read_text(encoding="utf-8")

    # 提取形如 `/api/tasks/{task_id}` 的路径（忽略行内代码中普通文本）。
    paths = set(re.findall(r"`(/api/[^`\s]+)`", doc))
    assert paths, "docs/API.md should declare at least one /api path"

    for path in sorted(paths):
        # 跳过 {placeholder} 段；静态段（tasks/tick/stats/...）必须出现在实现源码里。
        static_segments = [seg for seg in path.split("/") if seg and not seg.startswith("{")]
        # 第一个段固定为 api；后续段需要能在 taskserver.py 路由判断中找到。
        for seg in static_segments[1:]:
            assert f'"{seg}"' in server_src or f"'{seg}'" in server_src, (
                f"path segment {seg!r} from {path!r} not found in taskserver.py"
            )


def test_api_doc_contains_error_codes():
    doc = API_DOC.read_text(encoding="utf-8")
    for code in ("400", "404", "405", "500"):
        assert code in doc
