import json
import sqlite3
from copy import deepcopy

import pytest
from astrbot_plugin_custom_plan.domain import (
    Actor,
    PlanError,
    apply_change,
    create_plan,
)
from astrbot_plugin_custom_plan.presentation import build_view
from astrbot_plugin_custom_plan.renderer import LocalRenderer

OWNER = Actor("qq-1", "owner")


def legacy_todo(document):
    document = deepcopy(document)
    amount = document["rules"]["amount_field"]
    for definition in document["fields"]:
        if definition["field_id"] == amount:
            definition.update(
                field_id="status",
                name="状态",
                type="status",
                options=["待完成", "已完成"],
            )
    document["rules"].pop("amount_field")
    document["rules"].update(
        status_field="status", done_value="已完成", pending_value="待完成"
    )
    if "view" in document:
        document["view"].pop("amount_field")
        document["view"].update(status_field="status", done_value="已完成")
    for record in document.get("records", []):
        quantity = record["values"].pop(amount)
        record["values"]["status"] = "已完成" if quantity > 0 else "待完成"
    if "protected_fields" in document:
        document["protected_fields"] = [
            "status" if identifier == amount else identifier
            for identifier in document["protected_fields"]
        ]
    return document


def downgrade_todos(connection, version=4):
    for table, column in (("plans", "document"), ("changes", "previous")):
        rows = connection.execute(
            f"SELECT rowid, {column} FROM {table} WHERE {column} IS NOT NULL"
        ).fetchall()
        for rowid, raw in rows:
            document = legacy_todo(json.loads(raw))
            if version == 1:
                document.pop("render_layout")
            if version in (1, 2):
                document.pop("render_theme")
            connection.execute(
                f"UPDATE {table} SET {column}=? WHERE rowid=?",
                (json.dumps(document), rowid),
            )
    connection.execute(f"PRAGMA user_version={version}")


def test_todo_preset_has_no_status_field_or_binding():
    plan = create_plan(OWNER, {"name": "任务", "preset": "todo"}, "Asia/Shanghai")
    assert {definition["field_id"] for definition in plan["fields"]} == {
        "title",
        "amount",
        "due",
        "note",
    }
    assert plan["rules"] == {
        "kind": "todo",
        "title_field": "title",
        "amount_field": "amount",
    }
    for params in (
        {"status_field": "amount"},
        {"done_value": 1},
    ):
        with pytest.raises(PlanError, match="不支持"):
            apply_change(deepcopy(plan), "view", params, OWNER)
    with pytest.raises(PlanError, match="字段类型"):
        apply_change(
            deepcopy(plan),
            "fields",
            {"fields": [{"field_id": "s", "name": "状态", "type": "status"}]},
            OWNER,
        )


@pytest.mark.parametrize(
    "amount", [-1, None, True, "1", [], float("nan"), float("inf")]
)
def test_todo_amount_must_be_a_nonnegative_number(amount):
    plan = create_plan(OWNER, {"name": "任务", "preset": "todo"}, "Asia/Shanghai")
    with pytest.raises(PlanError):
        apply_change(
            plan,
            "add_records",
            {"records": [{"values": {"title": "阅读", "amount": amount}}]},
            OWNER,
        )


@pytest.mark.parametrize("layout", ["mobile", "desktop"])
def test_todo_view_uses_positive_amount_and_pending_first(tmp_path, layout):
    plan = create_plan(
        OWNER,
        {"name": "任务", "preset": "todo", "render_layout": layout},
        "Asia/Shanghai",
    )
    apply_change(
        plan,
        "add_records",
        {
            "records": [
                {"values": {"title": "完成甲", "amount": 0.25}},
                {"values": {"title": "待办"}},
                {"values": {"title": "完成乙", "amount": 3}},
            ]
        },
        OWNER,
    )
    records = deepcopy(plan["records"])
    assert records[1]["values"]["amount"] == 0
    apply_change(plan, "view", {"type": "todo"}, OWNER)
    view = build_view(plan, {}, OWNER.user)["view"]
    assert view["completed"] == 2
    assert [row["done"] for row in view["rows"]] == [False, True, True]
    assert view["rows"][0]["title"] == "待办"
    html = LocalRenderer(tmp_path, {}).html(plan, {}, OWNER.user)
    assert "1 项未完成 · 2 项已完成" in html
    assert "待完成" in html and "完成于" in html
    assert plan["records"] == records


def test_checkin_todo_still_requires_the_daily_threshold():
    plan = create_plan(OWNER, {"name": "打卡", "preset": "checkin"}, "Asia/Shanghai")
    plan["rules"]["thresholds"][0]["value"] = 2
    apply_change(plan, "add_records", {"records": [{"values": {"amount": 1}}]}, OWNER)
    apply_change(plan, "view", {"type": "todo"}, OWNER)
    row = build_view(plan, {}, OWNER.user)["view"]["rows"][0]
    assert row["amount"] == 1 and not row["done"]


async def test_positive_amount_updates_preserve_completion_time_and_undo(
    storage, monkeypatch
):
    clock = {"now": "2026-09-30T00:00:00+00:00"}
    monkeypatch.setattr(
        "astrbot_plugin_custom_plan.domain.now_iso", lambda: clock["now"]
    )
    created = await storage.mutate(
        OWNER, "create", {"name": "任务", "preset": "todo"}, message_key="create"
    )
    plan_id = created["plan_id"]
    added = await storage.mutate(
        OWNER,
        "add_records",
        {"records": [{"values": {"title": "阅读"}}]},
        plan_id,
        1,
        "add",
    )
    record_id = added["changed_ids"][0]
    for revision, amount in ((2, 0.5), (3, 2), (4, 0)):
        clock["now"] = f"2026-09-30T00:0{revision}:00+00:00"
        await storage.mutate(
            OWNER,
            "update_records",
            {"records": [{"record_id": record_id, "values": {"amount": amount}}]},
            plan_id,
            revision,
            f"amount-{revision}",
        )
        record = (await storage.snapshot(plan_id, OWNER))["records"][0]
        expected = "2026-09-30T00:02:00+00:00" if amount > 0 else None
        assert record["completed_at"] == expected
    await storage.mutate(OWNER, "undo", {}, plan_id, 5, "undo")
    restored = (await storage.snapshot(plan_id, OWNER))["records"][0]
    assert restored["values"]["amount"] == 2
    assert restored["completed_at"] == "2026-09-30T00:02:00+00:00"


@pytest.mark.parametrize("version", [1, 2, 3, 4])
async def test_legacy_todos_migrate_with_deleted_plans_and_undo(storage, version):
    created = await storage.mutate(
        OWNER,
        "create",
        {"name": "旧待办", "preset": "todo", "render_theme": "forest"},
        message_key="create",
    )
    plan_id = created["plan_id"]
    added = await storage.mutate(
        OWNER,
        "add_records",
        {
            "records": [
                {"values": {"title": "未完成"}},
                {"values": {"title": "已完成", "amount": 1}},
            ]
        },
        plan_id,
        1,
        "add",
    )
    await storage.mutate(
        OWNER,
        "update_records",
        {"records": [{"record_id": added["changed_ids"][0], "values": {"amount": 1}}]},
        plan_id,
        2,
        "done",
    )
    before = await storage.snapshot(plan_id, OWNER)
    deleted = await storage.mutate(
        OWNER,
        "create",
        {"name": "已删除", "preset": "todo"},
        message_key="deleted-create",
    )
    await storage.mutate(OWNER, "delete", {}, deleted["plan_id"], 1, "delete")
    with sqlite3.connect(storage.path) as connection:
        downgrade_todos(connection, version)
    await storage.initialize()
    migrated = await storage.snapshot(plan_id, OWNER)
    assert migrated["revision"] == before["revision"]
    assert migrated["updated_at"] == before["updated_at"]
    assert migrated["rules"] == {
        "kind": "todo",
        "title_field": "title",
        "amount_field": "status",
    }
    assert all(definition["type"] != "status" for definition in migrated["fields"])
    for record, old in zip(migrated["records"], before["records"], strict=True):
        assert record["record_id"] == old["record_id"]
        assert record["completed_at"] == old["completed_at"]
        assert record["values"]["status"] == 1
    assert (await storage.snapshot(deleted["plan_id"], OWNER, include_deleted=True))[
        "deleted"
    ]
    await storage.initialize()
    assert await storage.snapshot(plan_id, OWNER) == migrated
    await storage.mutate(OWNER, "undo", {}, plan_id, 3, "undo")
    restored = await storage.snapshot(plan_id, OWNER)
    assert restored["records"][0]["values"]["status"] == 0
    assert restored["records"][0]["completed_at"] is None
    with sqlite3.connect(storage.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5


async def test_status_migration_rolls_back_on_invalid_legacy_values(storage):
    for name in ("first", "second"):
        created = await storage.mutate(
            OWNER, "create", {"name": name, "preset": "todo"}, message_key=name
        )
        await storage.mutate(
            OWNER,
            "add_records",
            {"records": [{"values": {"title": name}}]},
            created["plan_id"],
            1,
            name + "-record",
        )
    with sqlite3.connect(storage.path) as connection:
        downgrade_todos(connection)
        rowid, raw = connection.execute(
            "SELECT rowid, document FROM plans ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        broken = json.loads(raw)
        broken["records"][0]["values"]["status"] = "未知值"
        connection.execute(
            "UPDATE plans SET document=? WHERE rowid=?", (json.dumps(broken), rowid)
        )
        before = connection.execute("SELECT * FROM plans").fetchall()
        changes = connection.execute("SELECT * FROM changes").fetchall()
    with pytest.raises(PlanError, match="无法安全迁移"):
        await storage.initialize()
    with sqlite3.connect(storage.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT * FROM plans").fetchall() == before
        assert connection.execute("SELECT * FROM changes").fetchall() == changes


async def test_unbound_legacy_status_preserves_text(storage):
    created = await storage.mutate(
        OWNER, "create", {"name": "普通表格"}, message_key="create"
    )
    document = await storage.snapshot(created["plan_id"], OWNER)
    document["fields"] = [
        {
            "field_id": "stage",
            "name": "阶段",
            "type": "status",
            "options": ["准备", "进行中"],
        }
    ]
    with sqlite3.connect(storage.path) as connection:
        connection.execute("UPDATE plans SET document=?", (json.dumps(document),))
        connection.execute("PRAGMA user_version=4")
    await storage.initialize()
    migrated = await storage.snapshot(created["plan_id"], OWNER)
    assert migrated["fields"] == [{"field_id": "stage", "name": "阶段", "type": "text"}]
    await storage.mutate(
        OWNER,
        "add_records",
        {"records": [{"values": {"stage": "进行中"}}]},
        created["plan_id"],
        1,
        "text",
    )


async def test_generic_todo_migration_updates_filters_and_supervision_fields(storage):
    created = await storage.mutate(
        OWNER, "create", {"name": "自定义待办"}, message_key="create"
    )
    plan_id = created["plan_id"]
    await storage.mutate(
        OWNER,
        "fields",
        {
            "fields": [
                {"field_id": "name", "name": "标题", "type": "text"},
                {"field_id": "progress", "name": "完成量", "type": "number"},
            ]
        },
        plan_id,
        1,
        "fields",
    )
    await storage.mutate(
        OWNER,
        "add_records",
        {"records": [{"values": {"name": "阅读", "progress": 1}}]},
        plan_id,
        2,
        "add",
    )
    await storage.mutate(
        OWNER,
        "view",
        {
            "type": "todo",
            "title_field": "name",
            "amount_field": "progress",
            "filters": {"equals": {"progress": 1}},
        },
        plan_id,
        3,
        "view",
    )
    preview = await storage.supervision_preview(
        OWNER,
        plan_id,
        4,
        {
            "end_date": None,
            "execution_standard": "完成阅读任务。",
            "protected_fields": ["name", "progress"],
        },
        "preview",
    )
    await storage.mutate(
        OWNER,
        "supervision_enable",
        {"confirmation_token": preview["confirmation_token"]},
        plan_id,
        4,
        "enable",
    )
    with sqlite3.connect(storage.path) as connection:
        document = json.loads(
            connection.execute("SELECT document FROM plans").fetchone()[0]
        )
        state = json.loads(
            connection.execute("SELECT document FROM supervision").fetchone()[0]
        )
        for definitions in (document["fields"], state["constraints"]["fields"]):
            definitions[1].update(type="status", options=["否", "是"])
        document["records"][0]["values"]["progress"] = "是"
        document["view"].pop("amount_field")
        document["view"].update(status_field="progress", done_value="是")
        document["view"]["filters"]["equals"]["progress"] = "是"
        connection.execute("UPDATE plans SET document=?", (json.dumps(document),))
        connection.execute("UPDATE supervision SET document=?", (json.dumps(state),))
        connection.execute("PRAGMA user_version=4")
    await storage.initialize()
    migrated = await storage.snapshot(plan_id, OWNER)
    assert migrated["view"]["amount_field"] == "progress"
    assert migrated["view"]["filters"]["equals"]["progress"] == 1
    assert build_view(migrated, {}, OWNER.user)["view"]["completed"] == 1
    agreement = await storage.supervision_rules(OWNER, plan_id)
    assert agreement["constraints"]["fields"][1]["type"] == "number"
    assert agreement["constraints"]["protected_fields"] == ["name", "progress"]


async def test_supervised_legacy_todo_preserves_anchors_and_correction_rules(storage):
    created = await storage.mutate(
        OWNER, "create", {"name": "受监督待办", "preset": "todo"}, message_key="create"
    )
    plan_id = created["plan_id"]
    added = await storage.mutate(
        OWNER,
        "add_records",
        {"records": [{"values": {"title": "阅读", "amount": 1}}]},
        plan_id,
        1,
        "add",
    )
    record_id = added["changed_ids"][0]
    preview = await storage.supervision_preview(
        OWNER,
        plan_id,
        2,
        {"end_date": None, "execution_standard": "完成阅读任务并如实记录。"},
        "preview",
    )
    await storage.mutate(
        OWNER,
        "supervision_enable",
        {"confirmation_token": preview["confirmation_token"]},
        plan_id,
        2,
        "enable",
    )
    before = await storage.snapshot(plan_id, OWNER)
    with sqlite3.connect(storage.path) as connection:
        downgrade_todos(connection)
        state = json.loads(
            connection.execute(
                "SELECT document FROM supervision WHERE plan_id=?", (plan_id,)
            ).fetchone()[0]
        )
        state["constraints"] = legacy_todo(state["constraints"])
        state["rules_text"] = "原协议正文：" + json.dumps(
            state["constraints"], ensure_ascii=False
        )
        original_text = state["rules_text"]
        anchors = deepcopy(state["completion_anchors"])
        connection.execute(
            "UPDATE supervision SET document=? WHERE plan_id=?",
            (json.dumps(state), plan_id),
        )
        connection.execute(
            "INSERT OR REPLACE INTO supervision_previews VALUES(?, ?)",
            (plan_id, json.dumps({"token": "old-preview"})),
        )
        logs = connection.execute("SELECT * FROM supervision_log").fetchall()
    await storage.initialize()
    agreement = await storage.supervision_rules(OWNER, plan_id)
    assert agreement["supervision"]["active"]
    assert agreement["rules_text"].startswith(original_text)
    assert "字段格式升级说明" in agreement["rules_text"]
    assert "status" in agreement["constraints"]["protected_fields"]
    assert agreement["constraints"]["rules"]["amount_field"] == "status"
    with sqlite3.connect(storage.path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM supervision_previews").fetchone()[
                0
            ]
            == 0
        )
        assert connection.execute("SELECT * FROM supervision_log").fetchall() == logs
        state = json.loads(
            connection.execute("SELECT document FROM supervision").fetchone()[0]
        )
        assert state["completion_anchors"] == anchors
    await storage.mutate(
        OWNER,
        "update_records",
        {"records": [{"record_id": record_id, "values": {"status": 2}}]},
        plan_id,
        3,
        "progress",
    )
    assert (await storage.snapshot(plan_id, OWNER))["records"][0][
        "completed_at"
    ] == before["records"][0]["completed_at"]
    with pytest.raises(PlanError, match="留痕纠错"):
        await storage.mutate(
            OWNER,
            "update_records",
            {"records": [{"record_id": record_id, "values": {"status": 0}}]},
            plan_id,
            4,
            "reopen",
        )
    await storage.mutate(
        OWNER,
        "correct_records",
        {
            "records": [{"record_id": record_id, "values": {"status": 0}}],
            "reason": "误标完成",
        },
        plan_id,
        4,
        "correct",
    )
    assert (await storage.snapshot(plan_id, OWNER))["records"][0][
        "completed_at"
    ] is None
