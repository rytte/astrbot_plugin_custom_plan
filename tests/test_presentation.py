import os
from copy import deepcopy
from pathlib import Path

import pytest
from astrbot_plugin_custom_plan.domain import (
    Actor,
    PlanError,
    apply_change,
    create_plan,
)
from astrbot_plugin_custom_plan.presentation import build_view
from astrbot_plugin_custom_plan.renderer import LocalRenderer

OWNER = Actor("qq", "owner")


@pytest.mark.parametrize(
    "kind,preset",
    [
        ("table", "generic"),
        ("table", "goal"),
        ("checkin", "checkin"),
        ("todo", "todo"),
        ("calendar", "todo"),
    ],
)
def test_all_templates_empty_and_populated(tmp_path, kind, preset):
    plan = create_plan(
        OWNER, {"name": "九月学习计划", "preset": preset}, "Asia/Shanghai"
    )
    apply_change(plan, "view", {"type": kind}, OWNER)
    renderer = LocalRenderer(tmp_path, {})
    empty = renderer.html(plan, {}, OWNER.user)
    assert "九月学习计划" in empty
    if preset == "generic":
        apply_change(
            plan,
            "fields",
            {"fields": [{"field_id": "name", "name": "内容", "type": "text"}]},
            OWNER,
        )
        values = {"name": "示例"}
    elif preset == "todo":
        values = {"title": "复习", "due": "2026-09-21"}
    else:
        values = {"amount": 3}
    apply_change(plan, "add_records", {"records": [{"values": values}]}, OWNER)
    populated = renderer.html(plan, {"month": "2026-09"}, OWNER.user)
    assert '<main class="board">' in populated


def test_html_escaping_notes_and_unknown_template_rejection(tmp_path):
    attack = '<img src="https://example.invalid/leak"><script>alert(1)</script>{{7*7}}'
    plan = create_plan(
        OWNER, {"name": "<script>x</script>", "goal": attack}, "Asia/Shanghai"
    )
    apply_change(
        plan, "block_add", {"type": "notes", "config": {"text": attack}}, OWNER
    )
    renderer = LocalRenderer(tmp_path, {})
    html = renderer.html(plan, {}, OWNER.user)
    assert "&lt;script&gt;" in html and '<img src="https' not in html
    assert "{{7*7}}" in html
    with pytest.raises(PlanError):
        apply_change(
            deepcopy(plan), "block_add", {"type": "../../secrets", "config": {}}, OWNER
        )
    with pytest.raises(PlanError):
        apply_change(deepcopy(plan), "view", {"type": "../../secrets"}, OWNER)


def test_calendar_month_alignment_undated_and_record_overflow():
    plan = create_plan(OWNER, {"name": "任务", "preset": "todo"}, "Asia/Shanghai")
    apply_change(plan, "view", {"type": "calendar"}, OWNER)
    apply_change(
        plan,
        "add_records",
        {
            "records": [
                {"values": {"title": "a", "due": "2026-09-01"}},
                {"values": {"title": "b", "due": "2026-09-01"}},
                {"values": {"title": "c", "due": "2026-09-01"}},
                {"values": {"title": "无日期"}},
            ]
        },
        OWNER,
    )
    result = build_view(plan, {"month": "2026-09"}, OWNER.user)["view"]
    assert result["cells"][0]["day"] == 0
    assert result["cells"][1]["day"] == 1
    assert result["cells"][1]["count"] == 3
    assert len(result["cells"][1]["labels"]) == 2
    assert result["undated"] == 1 and result["monthly_count"] == 3
    with pytest.raises(PlanError):
        build_view(plan, {"month": "2026-13"}, OWNER.user)


def test_stats_nulls_scope_and_hidden_main_view(tmp_path):
    plan = create_plan(OWNER, {"name": "记录"}, "Asia/Shanghai")
    apply_change(
        plan,
        "fields",
        {
            "fields": [
                {"field_id": "n", "name": "数值", "type": "number"},
                {"field_id": "s", "name": "分类", "type": "text"},
            ]
        },
        OWNER,
    )
    apply_change(
        plan,
        "add_records",
        {
            "records": [
                {"values": {"n": 0, "s": "a"}},
                {"values": {"n": 6, "s": "a"}},
                {"values": {"n": None, "s": "b"}},
            ]
        },
        OWNER,
    )
    apply_change(
        plan,
        "block_add",
        {"type": "statistics", "config": {"operation": "average", "field_id": "n"}},
        OWNER,
    )
    apply_change(
        plan,
        "block_add",
        {
            "type": "statistics",
            "config": {"operation": "count", "filters": {"equals": {"s": "b"}}},
        },
        OWNER,
    )
    apply_change(
        plan, "view", {"visible": False, "filters": {"equals": {"s": "b"}}}, OWNER
    )
    model = build_view(plan, {}, OWNER.user)
    assert model["blocks"][0]["value"] == 3
    assert model["blocks"][1]["value"] == 1
    assert "平均" in LocalRenderer(tmp_path, {}).html(plan, {}, OWNER.user)


def test_wide_table_pages_and_invalid_page():
    plan = create_plan(OWNER, {"name": "宽表"}, "Asia/Shanghai")
    apply_change(
        plan,
        "fields",
        {
            "fields": [
                {"field_id": "f" + str(i), "name": "字段" + str(i), "type": "text"}
                for i in range(8)
            ]
        },
        OWNER,
    )
    data = build_view(plan, {"column_page": 2}, OWNER.user)
    assert len(data["view"]["fields"]) == 2
    with pytest.raises(PlanError, match="字段只有"):
        build_view(plan, {"column_page": 3}, OWNER.user)


async def test_renderer_disabled_and_queue_limits(tmp_path):
    plan = create_plan(OWNER, {"name": "计划"}, "Asia/Shanghai")
    renderer = LocalRenderer(tmp_path, {"render_enabled": False})
    with pytest.raises(PlanError, match="未启用"):
        await renderer.render(plan, {}, OWNER.user)
    renderer = LocalRenderer(tmp_path, {"render_queue_size": 0})
    renderer.pending = 1
    with pytest.raises(PlanError, match="已满"):
        await renderer.render(plan, {}, OWNER.user)


@pytest.mark.skipif(
    not os.environ.get("CUSTOM_PLAN_BROWSER"),
    reason="Set CUSTOM_PLAN_BROWSER to run real local Chromium screenshots",
)
async def test_real_screenshots_offline_cleanup_and_browser_reuse(tmp_path):
    pytest.importorskip("playwright")
    renderer = LocalRenderer(
        tmp_path,
        {"browser_executable": os.environ["CUSTOM_PLAN_BROWSER"], "render_timeout": 90},
    )
    await renderer.initialize()
    try:
        browser = None
        for kind, preset in (
            ("table", "goal"),
            ("checkin", "checkin"),
            ("todo", "todo"),
            ("calendar", "todo"),
        ):
            plan = create_plan(
                OWNER, {"name": "本地渲染验证", "preset": preset}, "Asia/Shanghai"
            )
            apply_change(plan, "view", {"type": kind}, OWNER)
            values = (
                {"title": "学习中文字体", "due": "2026-09-21"}
                if preset == "todo"
                else {"amount": 2}
            )
            apply_change(plan, "add_records", {"records": [{"values": values}]}, OWNER)
            path = await renderer.render(plan, {"month": "2026-09"}, OWNER.user)
            assert path.read_bytes().startswith(b"\x89PNG")
            assert renderer.browser.contexts == []
            if browser is not None:
                assert renderer.browser is browser
            browser = renderer.browser
            path.unlink()
        assert renderer.pending == 0
        assert not list(Path(tmp_path).glob("*.png"))
    finally:
        await renderer.close()
