"""最小可运行示例：难度评估 + 路由决策 + 会话级推荐。"""
from llm_router import assess_difficulty, assess_urgency, recommend_for_session, route_model


def main() -> None:
    text = "帮我写一个 Python 脚本，爬取公开新闻并保存到 CSV"
    difficulty = assess_difficulty(text)
    urgent = assess_urgency(text)
    decision = route_model(difficulty, urgent=urgent)
    print(f"text: {text}")
    print(f"difficulty={difficulty} urgent={urgent}")
    print(f"decision={decision}")

    session = recommend_for_session(text, message_count=3)
    print(f"session_recommendation={session}")


if __name__ == "__main__":
    main()
