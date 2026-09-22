import json
import os
import re
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
from astrbot_plugin_custom_plan.appearance import LAYOUTS, THEMES, Theme
from astrbot_plugin_custom_plan.domain import (
    Actor,
    PlanError,
    apply_change,
    create_plan,
)
from astrbot_plugin_custom_plan.renderer import LocalRenderer
from astrbot_plugin_custom_plan.storage import Storage

OWNER = Actor("qq-1", "owner")
ASSETS = Path(__file__).parents[1] / "assets"


@pytest.fixture
def extra_theme(tmp_path, monkeypatch):
    """Exercise extension with a real CSS file without shipping another design."""
    path = tmp_path / "test-ink.css"
    source = (ASSETS / THEMES["forest"].stylesheet).read_text(encoding="utf-8")
    path.write_text(
        source.replace("--canvas: #edf3ef", "--canvas: #101828"), encoding="utf-8"
    )
    monkeypatch.setitem(THEMES, "test_ink", Theme("测试墨色", str(path)))
    return "test_ink"


def test_registered_styles_have_complete_variables_and_no_embedded_palette():
    base = (ASSETS / "base.css").read_text(encoding="utf-8")
    for layout in LAYOUTS.values():
        layout_css = (ASSETS / layout.stylesheet).read_text(encoding="utf-8")
        components = base + layout_css
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgb|hsl)a?\(", components)
        for theme in THEMES.values():
            css = components + (ASSETS / theme.stylesheet).read_text(encoding="utf-8")
            references = set(re.findall(r"var\((--[\w-]+)\)", css))
            definitions = set(re.findall(r"(--[\w-]+)\s*:", css)) | {"--board-width"}
            assert references <= definitions, references - definitions


async def test_theme_is_persisted_and_independent_of_layout_and_global_default(
    storage, extra_theme
):
    created = await storage.mutate(
        OWNER, "create", {"name": "现有计划", "preset": "todo"}, message_key="c"
    )
    pid = created["plan_id"]
    initial = await storage.snapshot(pid, OWNER)
    reopened = Storage(
        storage.path, default_render_layout="desktop", default_render_theme=extra_theme
    )
    await reopened.initialize()
    assert await reopened.snapshot(pid, OWNER) == initial
    new = await reopened.mutate(OWNER, "create", {"name": "新默认"}, message_key="n")
    assert new["render_theme"] == extra_theme and new["render_layout"] == "desktop"
    explicit = await reopened.mutate(
        OWNER, "create", {"name": "指定主题", "render_theme": "forest"}, message_key="e"
    )
    assert explicit["render_theme"] == "forest"
    await reopened.mutate(
        OWNER, "update", {"render_theme": extra_theme}, pid, 1, "theme"
    )
    updated = await reopened.snapshot(pid, OWNER)
    assert (
        updated["render_theme"] == extra_theme and updated["render_layout"] == "mobile"
    )
    for key in ("records", "fields", "rules", "view", "blocks"):
        assert updated[key] == initial[key]
    await reopened.mutate(OWNER, "undo", {}, pid, 2, "undo")
    assert (await reopened.snapshot(pid, OWNER))["render_theme"] == "forest"
    await reopened.mutate(
        OWNER, "update", {"render_theme": extra_theme}, pid, 3, "theme-again"
    )
    await reopened.mutate(
        OWNER, "update", {"render_layout": "desktop"}, pid, 4, "layout"
    )
    assert (await reopened.snapshot(pid, OWNER))["render_theme"] == extra_theme
    with pytest.raises(PlanError, match="版本冲突"):
        await reopened.mutate(
            OWNER, "update", {"render_theme": "forest"}, pid, 4, "stale"
        )
    with pytest.raises(PlanError, match="权限"):
        await reopened.mutate(
            Actor("qq-1", "other"),
            "update",
            {"render_theme": "forest"},
            pid,
            5,
            "forbidden",
        )
    await storage.initialize()
    assert (await storage.query(OWNER, {"plan_id": pid}))["render_theme"] == extra_theme
    listing = await reopened.query(OWNER, {})
    assert listing["default_render_theme"] == extra_theme
    assert {"id": extra_theme, "name": "测试墨色"} in listing["themes"]
    assert (
        next(p for p in listing["plans"] if p["plan_id"] == pid)["render_theme"]
        == extra_theme
    )


@pytest.mark.parametrize(
    "value", [None, "", "FOREST", "missing", "../../secrets", 1, [], {}]
)
async def test_unknown_themes_fail_without_fallback_or_writes(storage, tmp_path, value):
    with pytest.raises(PlanError, match="default_render_theme"):
        Storage(storage.path, default_render_theme=value)
    with pytest.raises(PlanError, match="render_theme"):
        await storage.mutate(
            OWNER, "create", {"name": "无效", "render_theme": value}, message_key="bad"
        )
    assert (await storage.query(OWNER, {}))["total"] == 0
    created = await storage.mutate(OWNER, "create", {"name": "正常"}, message_key="c")
    pid = created["plan_id"]
    with pytest.raises(PlanError, match="render_theme"):
        await storage.mutate(OWNER, "update", {"render_theme": value}, pid, 1, "bad-u")
    snapshot = await storage.snapshot(pid, OWNER)
    assert snapshot["revision"] == 1 and snapshot["render_theme"] == "forest"
    snapshot["render_theme"] = value
    with pytest.raises(PlanError, match="render_theme"):
        LocalRenderer(tmp_path, {}).html(snapshot, {}, OWNER.user)
    snapshot.pop("render_theme")
    with pytest.raises(PlanError, match="render_theme"):
        LocalRenderer(tmp_path, {}).html(snapshot, {}, OWNER.user)


async def test_v2_theme_migration_preserves_desktop_deleted_plans_and_undo(
    storage, extra_theme
):
    created = await storage.mutate(
        OWNER,
        "create",
        {"name": "历史桌面计划", "render_layout": "desktop"},
        message_key="c",
    )
    pid = created["plan_id"]
    await storage.mutate(OWNER, "delete", {}, pid, 1, "delete")
    before = await storage.snapshot(pid, OWNER, include_deleted=True)
    with sqlite3.connect(storage.path) as connection:
        for table, column in (("plans", "document"), ("changes", "previous")):
            for rowid, raw in connection.execute(
                f"SELECT rowid, {column} FROM {table} WHERE {column} IS NOT NULL"
            ).fetchall():
                document = json.loads(raw)
                document.pop("render_theme")
                connection.execute(
                    f"UPDATE {table} SET {column}=? WHERE rowid=?",
                    (json.dumps(document), rowid),
                )
        connection.execute("PRAGMA user_version=2")
    upgraded = Storage(storage.path, default_render_theme=extra_theme)
    await upgraded.initialize()
    assert await upgraded.snapshot(pid, OWNER, include_deleted=True) == before
    with sqlite3.connect(storage.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert all(
            json.loads(row[0])["render_theme"] == "forest"
            for row in connection.execute(
                "SELECT previous FROM changes WHERE previous IS NOT NULL"
            )
        )
    await upgraded.mutate(OWNER, "undo", {}, pid, 2, "restore")
    restored = await upgraded.snapshot(pid, OWNER)
    assert (
        restored["render_theme"] == "forest" and restored["render_layout"] == "desktop"
    )
    await upgraded.mutate(
        OWNER, "update", {"render_theme": extra_theme}, pid, 3, "theme"
    )
    await upgraded.initialize()
    assert (await upgraded.snapshot(pid, OWNER))["render_theme"] == extra_theme


async def test_theme_migration_rolls_back_and_preserves_existing_values(
    storage, extra_theme
):
    first = await storage.mutate(OWNER, "create", {"name": "缺失"}, message_key="1")
    second = await storage.mutate(
        OWNER,
        "create",
        {"name": "已设置", "render_theme": extra_theme},
        message_key="2",
    )
    first_doc = await storage.snapshot(first["plan_id"], OWNER)
    first_doc.pop("render_theme")
    second_doc = await storage.snapshot(second["plan_id"], OWNER)
    second_doc["render_theme"] = None
    with sqlite3.connect(storage.path) as connection:
        connection.executemany(
            "UPDATE plans SET document=? WHERE plan_id=?",
            [
                (json.dumps(first_doc), first["plan_id"]),
                (json.dumps(second_doc), second["plan_id"]),
            ],
        )
        connection.execute("PRAGMA user_version=2")
    with pytest.raises(PlanError, match="render_theme"):
        await storage.initialize()
    assert "render_theme" not in await storage.snapshot(first["plan_id"], OWNER)
    with sqlite3.connect(storage.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        second_doc["render_theme"] = extra_theme
        connection.execute(
            "UPDATE plans SET document=? WHERE plan_id=?",
            (json.dumps(second_doc), second["plan_id"]),
        )
    await storage.initialize()
    assert (await storage.snapshot(first["plan_id"], OWNER))["render_theme"] == "forest"
    assert (await storage.snapshot(second["plan_id"], OWNER))[
        "render_theme"
    ] == extra_theme


@pytest.mark.skipif(
    not os.environ.get("CUSTOM_PLAN_BROWSER"), reason="Requires local Chromium"
)
async def test_registered_theme_renders_all_views_and_layouts_without_leaking(
    tmp_path, extra_theme
):
    from PIL import Image

    renderer = LocalRenderer(
        tmp_path,
        {"browser_executable": os.environ["CUSTOM_PLAN_BROWSER"], "render_timeout": 90},
    )
    await renderer.initialize()
    try:
        for kind, preset in (
            ("table", "goal"),
            ("checkin", "checkin"),
            ("todo", "todo"),
            ("calendar", "todo"),
        ):
            plan = create_plan(
                OWNER, {"name": "主题扩展验证", "preset": preset}, "Asia/Shanghai"
            )
            apply_change(plan, "view", {"type": kind}, OWNER)
            apply_change(
                plan,
                "block_add",
                {"type": "notes", "config": {"text": "主题不改变数据与布局"}},
                OWNER,
            )
            for layout, definition in LAYOUTS.items():
                plan["render_layout"] = layout
                original = deepcopy(plan)
                sizes = []
                for theme, color in (
                    ("forest", (237, 243, 239)),
                    (extra_theme, (16, 24, 40)),
                    ("forest", (237, 243, 239)),
                ):
                    plan["render_theme"] = theme
                    path = await renderer.render(plan, {}, OWNER.user)
                    with Image.open(path) as image:
                        assert image.width == definition.width
                        assert image.convert("RGB").getpixel((0, 0)) == color
                        sizes.append(image.size)
                    path.unlink()
                    assert renderer.browser.contexts == []
                assert len(set(sizes)) == 1
                assert plan == original
    finally:
        await renderer.close()
