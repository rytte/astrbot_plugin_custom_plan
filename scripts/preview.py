"""Generate four example PNGs using the production renderer."""

import argparse
import asyncio
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_custom_plan.appearance import (  # noqa: E402
    DEFAULT_THEME,
    LAYOUTS,
    THEMES,
)
from astrbot_plugin_custom_plan.domain import (  # noqa: E402
    Actor,
    apply_change,
    create_plan,
    today,
)
from astrbot_plugin_custom_plan.renderer import LocalRenderer  # noqa: E402


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", default="")
    parser.add_argument("--font", default="")
    parser.add_argument("--layout", choices=LAYOUTS, default="mobile")
    parser.add_argument("--theme", choices=THEMES, default=DEFAULT_THEME)
    parser.add_argument(
        "--supervised",
        action="store_true",
        help="Show the supervision shield in example images",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "dist" / "previews",
    )
    args = parser.parse_args()
    config = {
        "browser_executable": args.browser,
        "font_path": args.font,
        "render_timeout": 90,
    }
    output = args.output / args.theme / args.layout
    if args.supervised:
        output = output / "supervised"
    renderer = LocalRenderer(output, config)
    await renderer.initialize()
    actor = Actor("preview", "demo")
    metrics = []
    try:
        for kind, preset in (
            ("table", "goal"),
            ("checkin", "checkin"),
            ("todo", "todo"),
            ("calendar", "todo"),
        ):
            plan = create_plan(
                actor,
                {
                    "name": "学习计划 · "
                    + {
                        "table": "通用表格",
                        "checkin": "打卡面板",
                        "todo": "待办列表",
                        "calendar": "日历",
                    }[kind],
                    "preset": preset,
                    "render_layout": args.layout,
                    "render_theme": args.theme,
                    "goal": "每天学习一点，记录自己的进步。",
                },
                "Asia/Shanghai",
            )
            current = today(plan)
            if preset == "todo":
                records = [
                    {
                        "values": {
                            "title": title,
                            "status": "已完成" if index < 2 else "待完成",
                            "due": (current + timedelta(days=index - 1)).isoformat(),
                        }
                    }
                    for index, title in enumerate(
                        [
                            "英语听力 30 分钟",
                            "阅读《深度工作》",
                            "完成 3 道算法练习",
                            "复习课程第 4 章",
                            "整理本周笔记",
                        ]
                    )
                ]
            else:
                records = [
                    {
                        "values": {
                            "date": (current - timedelta(days=i)).isoformat(),
                            "amount": (0 if i % 3 == 2 else 1)
                            if preset == "checkin"
                            else (i % 3 + 1) * 10,
                            "note": ["专注阅读", "完成课程练习", "复盘错题"][i % 3],
                        }
                    }
                    for i in (0, 1, 2, 3, 5, 6, 8, 9, 10)
                ]
            apply_change(plan, "add_records", {"records": records}, actor)
            apply_change(plan, "view", {"type": kind}, actor)
            apply_change(
                plan,
                "block_add",
                {
                    "type": "notes",
                    "title": "本周随笔",
                    "config": {
                        "text": "把学习拆成每天能完成的小任务。\n明天继续巩固今天遇到的难点。"
                    },
                },
                actor,
            )
            started = time.perf_counter()
            plan["supervision"] = {"active": args.supervised}
            image = await renderer.render(plan, {}, actor.user)
            destination = output / (kind + ".png")
            image.replace(destination)
            metrics.append(
                {
                    "view": kind,
                    "seconds": round(time.perf_counter() - started, 3),
                    "bytes": destination.stat().st_size,
                    "path": str(destination),
                }
            )
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    finally:
        await renderer.close()


if __name__ == "__main__":
    asyncio.run(main())
