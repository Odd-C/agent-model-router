"""真实外部依赖冒烟测试（可选，无 key 时自动 skip）。

背景：v2.1 接入层曾出现 mock 测试全过但真实 API 调用失败的案例
（GLM URL 构造 / 模型 @provider 后缀 / JSON 围栏三个 bug 全是真调才暴露）。
本文件提供最小真实调用冒烟，防止「OpenAI 兼容端点 + 严格 JSON 输出」类
集成回归。

运行方式：
  python -m pytest tests/test_live_smoke.py -v

需要环境变量（任一存在即跑真实调用；否则全部 skip）：
  MODEL_SCHEDULER_SMOKE_BASE_URL   OpenAI 兼容 base_url（如 https://open.bigmodel.cn/api/paas/v4）
  MODEL_SCHEDULER_SMOKE_API_KEY    API key
  MODEL_SCHEDULER_SMOKE_MODEL      模型 id（如 glm-4-flash-250414）
"""

import json
import os
import sys
import unittest
import urllib.request

BASE_URL = os.environ.get("MODEL_SCHEDULER_SMOKE_BASE_URL", "").strip()
API_KEY = os.environ.get("MODEL_SCHEDULER_SMOKE_API_KEY", "").strip()
MODEL = os.environ.get("MODEL_SCHEDULER_SMOKE_MODEL", "").strip()


def _available():
    return bool(BASE_URL and API_KEY and MODEL)


@unittest.skipUnless(_available(), "未配置 MODEL_SCHEDULER_SMOKE_* 环境变量（真实调用冒烟可选）")
class LiveSmokeTests(unittest.TestCase):
    def test_chat_completion_strict_json(self):
        """真实调用 chat completions，要求返回可解析的严格 JSON（剥围栏后）。"""
        url = f"{BASE_URL.rstrip('/')}/chat/completions"
        body = {
            "model": MODEL,
            "temperature": 0,
            "max_tokens": 80,
            "stream": False,
            "messages": [
                {"role": "system", "content": "你是任务分类器，只输出严格 JSON。"},
                {"role": "user", "content": '{"task_type":"coding","confidence":0.9,"reason":"test"}'},
            ],
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {API_KEY}",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310（环境变量受控）
            raw = resp.read().decode()

        parsed = json.loads(raw)
        content = parsed["choices"][0]["message"]["content"]

        # 剥 ```json / ``` 围栏后解析
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = __import__("re").sub(r"^```[a-zA-Z]*\s*", "", cleaned)
            cleaned = __import__("re").sub(r"\s*```$", "", cleaned)
        data = json.loads(cleaned)

        self.assertIn("task_type", data)
        self.assertIn(data["task_type"], {"coding", "image", "text", "batch", "maintenance"})
        self.assertIn("confidence", data)


if __name__ == "__main__":
    unittest.main()
