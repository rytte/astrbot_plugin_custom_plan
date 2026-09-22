import asyncio
import json
import sqlite3
from copy import deepcopy
from datetime import date, timedelta

import pytest
from astrbot_plugin_custom_plan.domain import Actor, PlanError, allowed, create_plan
from astrbot_plugin_custom_plan.presentation import build_view

OWNER = Actor("qq-1", "owner")


async def test_plan_layout_is_persisted_independent_of_creation_default(storage):
    from astrbot_plugin_custom_plan.storage import Storage

    created = await storage.mutate(OWNER, "create", {"name": "手机"}, message_key="c")
    pid = created["plan_id"]
    reopened = Storage(storage.path, default_render_layout="desktop")
    await reopened.initialize()
    assert (await reopened.snapshot(pid, OWNER))["render_layout"] == "mobile"
    desktop = await reopened.mutate(OWNER, "create", {"name": "桌面"}, message_key="d")
    assert (await reopened.snapshot(desktop["plan_id"], OWNER))[
        "render_layout"
    ] == "desktop"
    explicit = await reopened.mutate(
        OWNER, "create", {"name": "指定", "render_layout": "mobile"}, message_key="e"
    )
    assert (await reopened.snapshot(explicit["plan_id"], OWNER))[
        "render_layout"
    ] == "mobile"
    await reopened.mutate(OWNER, "update", {"render_layout": "desktop"}, pid, 1, "u")
    assert (await storage.query(OWNER, {"plan_id": pid}))["render_layout"] == "desktop"
    assert (await reopened.query(OWNER, {}))["default_render_layout"] == "desktop"
    with pytest.raises(PlanError, match="版本冲突"):
        await reopened.mutate(
            OWNER, "update", {"render_layout": "mobile"}, pid, 1, "stale"
        )
    await reopened.mutate(OWNER, "undo", {}, pid, 2, "undo")
    assert (await storage.snapshot(pid, OWNER))["render_layout"] == "mobile"


@pytest.mark.parametrize("value", [None, "", "auto", "MOBILE", 1, [], {}])
async def test_invalid_layout_rejected_without_writes(storage, value):
    from astrbot_plugin_custom_plan.storage import Storage

    with pytest.raises(PlanError, match="default_render_layout"):
        Storage(storage.path, default_render_layout=value)
    with pytest.raises(PlanError, match="render_layout"):
        await storage.mutate(
            OWNER, "create", {"name": "无效", "render_layout": value}, message_key="bad"
        )
    assert (await storage.query(OWNER, {}))["total"] == 0
    created = await storage.mutate(OWNER, "create", {"name": "有效"}, message_key="ok")
    with pytest.raises(PlanError, match="render_layout"):
        await storage.mutate(
            OWNER, "update", {"render_layout": value}, created["plan_id"], 1, "bad-u"
        )
    snapshot = await storage.snapshot(created["plan_id"], OWNER)
    assert snapshot["revision"] == 1 and snapshot["render_layout"] == "mobile"


async def test_layout_migration_covers_deleted_plans_and_undo_and_runs_once(storage):
    from astrbot_plugin_custom_plan.storage import Storage

    created = await storage.mutate(
        OWNER, "create", {"name": "历史计划", "preset": "todo"}, message_key="c"
    )
    pid = created["plan_id"]
    await storage.mutate(
        OWNER,
        "add_records",
        {"records": [{"values": {"title": "保留记录"}}]},
        pid,
        1,
        "r",
    )
    await storage.mutate(OWNER, "delete", {}, pid, 2, "delete")
    before = await storage.snapshot(pid, OWNER, include_deleted=True)
    # Reproduce v1 documents using the unchanged v1 SQL table schema.
    with sqlite3.connect(storage.path) as connection:
        for table, column in (("plans", "document"), ("changes", "previous")):
            for rowid, raw in connection.execute(
                f"SELECT rowid, {column} FROM {table} WHERE {column} IS NOT NULL"
            ).fetchall():
                document = json.loads(raw)
                document.pop("render_layout")
                document.pop("render_theme")
                connection.execute(
                    f"UPDATE {table} SET {column}=? WHERE rowid=?",
                    (json.dumps(document), rowid),
                )
        connection.execute("PRAGMA user_version=1")
    upgraded = Storage(storage.path, default_render_layout="desktop")
    await upgraded.initialize()
    assert await upgraded.snapshot(pid, OWNER, include_deleted=True) == before
    with sqlite3.connect(storage.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert all(
            json.loads(row[0])["render_layout"] == "mobile"
            for row in connection.execute(
                "SELECT previous FROM changes WHERE previous IS NOT NULL"
            )
        )
    await upgraded.mutate(OWNER, "undo", {}, pid, 3, "restore")
    restored = await upgraded.snapshot(pid, OWNER)
    assert (
        restored["records"] == before["records"]
        and restored["render_layout"] == "mobile"
    )
    await upgraded.mutate(
        OWNER, "update", {"render_layout": "desktop"}, pid, 4, "desktop"
    )
    await upgraded.initialize()
    assert (await upgraded.snapshot(pid, OWNER))["render_layout"] == "desktop"


async def test_layout_migration_is_atomic_and_preserves_explicit_layout(storage):
    first = await storage.mutate(OWNER, "create", {"name": "缺失"}, message_key="1")
    second = await storage.mutate(
        OWNER, "create", {"name": "显式", "render_layout": "desktop"}, message_key="2"
    )
    with sqlite3.connect(storage.path) as connection:
        first_doc = await storage.snapshot(first["plan_id"], OWNER)
        first_doc.pop("render_layout")
        second_doc = await storage.snapshot(second["plan_id"], OWNER)
        second_doc["render_layout"] = "invalid"
        connection.executemany(
            "UPDATE plans SET document=? WHERE plan_id=?",
            [
                (json.dumps(first_doc), first["plan_id"]),
                (json.dumps(second_doc), second["plan_id"]),
            ],
        )
        connection.execute("PRAGMA user_version=1")
    with pytest.raises(PlanError, match="render_layout"):
        await storage.initialize()
    assert "render_layout" not in await storage.snapshot(first["plan_id"], OWNER)
    with sqlite3.connect(storage.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        second_doc["render_layout"] = "desktop"
        connection.execute(
            "UPDATE plans SET document=? WHERE plan_id=?",
            (json.dumps(second_doc), second["plan_id"]),
        )
    await storage.initialize()
    assert (await storage.snapshot(first["plan_id"], OWNER))[
        "render_layout"
    ] == "mobile"
    assert (await storage.snapshot(second["plan_id"], OWNER))[
        "render_layout"
    ] == "desktop"


async def test_future_database_version_is_not_downgraded(storage):
    with sqlite3.connect(storage.path) as connection:
        connection.execute("PRAGMA user_version=4")
    with pytest.raises(PlanError, match="数据库版本"):
        await storage.initialize()
    with sqlite3.connect(storage.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4


@pytest.mark.parametrize(
    "scope,mode,actor,read,write",
    [
        ("person", "private", OWNER, True, True),
        ("person", "private", Actor("qq-1", "owner", "g1"), False, False),
        ("person", "shared", Actor("qq-1", "owner", "g2"), True, True),
        ("person", "shared", Actor("qq-1", "other", "g1"), False, False),
        ("person", "private", Actor("qq-2", "owner"), False, False),
        ("group", "protected", Actor("qq-1", "member", "g1"), True, False),
        ("group", "protected", Actor("qq-1", "owner", "g1"), True, True),
        ("group", "public", Actor("qq-1", "member", "g1"), True, True),
        ("group", "public", Actor("qq-1", "owner", "g2"), False, False),
        ("group", "public", OWNER, False, False),
        ("group", "public", Actor("qq-2", "member", "g1"), False, False),
    ],
)
def test_permission_matrix(scope, mode, actor, read, write):
    creator = Actor("qq-1", "owner", "g1") if scope == "group" else OWNER
    plan = create_plan(
        creator, {"name": "计划", "scope": scope, "mode": mode}, "Asia/Shanghai"
    )
    assert allowed(plan, actor) is read
    assert allowed(plan, actor, True) is write


async def test_all_presets_default_to_table_and_persist(storage):
    from astrbot_plugin_custom_plan.storage import Storage

    for preset in ("generic", "checkin", "goal", "todo"):
        result = await storage.mutate(
            OWNER, "create", {"name": preset, "preset": preset}, message_key=preset
        )
        loaded = await Storage(storage.path).snapshot(result["plan_id"], OWNER)
        assert loaded["view"]["type"] == "table"
        assert loaded["preset"] == preset


async def test_atomic_batch_versions_and_idempotency(storage):
    created = await storage.mutate(
        OWNER, "create", {"name": "阅读", "preset": "goal"}, message_key="create"
    )
    pid = created["plan_id"]
    payload = {"records": [{"values": {"amount": 3}}]}
    result = await storage.mutate(OWNER, "add_records", payload, pid, 1, "m1")
    replay = await storage.mutate(OWNER, "add_records", payload, pid, 2, "m1")
    assert replay["replayed"] and replay["revision"] == result["revision"]
    assert len((await storage.snapshot(pid, OWNER))["records"]) == 1
    with pytest.raises(PlanError, match="版本冲突"):
        await storage.mutate(OWNER, "add_records", payload, pid, 1, "m2")
    with pytest.raises(PlanError):
        await storage.mutate(
            OWNER,
            "add_records",
            {"records": [{"values": {"amount": 5}}, {"values": {"amount": "bad"}}]},
            pid,
            2,
            "m3",
        )
    snapshot = await storage.snapshot(pid, OWNER)
    assert snapshot["revision"] == 2 and len(snapshot["records"]) == 1


async def test_concurrent_updates_do_not_overwrite(storage):
    created = await storage.mutate(OWNER, "create", {"name": "计划"}, message_key="c")
    results = await asyncio.gather(
        *(
            storage.mutate(OWNER, "update", {"name": name}, created["plan_id"], 1, name)
            for name in ("甲", "乙")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, PlanError) for r in results) == 1
    assert (await storage.snapshot(created["plan_id"], OWNER))["revision"] == 2


async def test_undo_delete_and_reauthorization(storage):
    created = await storage.mutate(
        OWNER, "create", {"name": "原名", "mode": "shared"}, message_key="c"
    )
    pid = created["plan_id"]
    await storage.mutate(OWNER, "update", {"name": "新名"}, pid, 1, "rename")
    with pytest.raises(PlanError, match="版本冲突"):
        await storage.mutate(OWNER, "undo", {}, pid, 1, "bad-undo")
    await storage.mutate(OWNER, "undo", {}, pid, 2, "undo")
    assert (await storage.snapshot(pid, OWNER))["name"] == "原名"
    with pytest.raises(PlanError, match="没有可撤销"):
        await storage.mutate(OWNER, "undo", {}, pid, 3, "undo2")
    await storage.mutate(OWNER, "delete", {}, pid, 3, "delete")
    with pytest.raises(PlanError, match="已删除"):
        await storage.snapshot(pid, OWNER)
    await storage.mutate(OWNER, "undo", {}, pid, 4, "restore")
    await storage.mutate(OWNER, "update", {"mode": "private"}, pid, 5, "private")
    group = Actor("qq-1", "owner", "g1")
    assert (await storage.query(group, {}))["total"] == 0
    with pytest.raises(PlanError, match="权限"):
        await storage.snapshot(pid, group)
    with pytest.raises(PlanError, match="权限"):
        await storage.mutate(group, "update", {"name": "新名"}, pid, 1, "rename")


async def test_references_rename_hide_and_block_deletion(storage):
    created = await storage.mutate(
        OWNER, "create", {"name": "累计", "preset": "goal"}, message_key="c"
    )
    pid = created["plan_id"]
    await storage.mutate(
        OWNER,
        "add_records",
        {"records": [{"values": {"amount": 10}}, {"values": {"amount": 20}}]},
        pid,
        1,
        "r",
    )
    snapshot = await storage.snapshot(pid, OWNER)
    fields = deepcopy(snapshot["fields"])
    fields[1]["name"] = "公里数"
    await storage.mutate(OWNER, "fields", {"fields": fields}, pid, 2, "rename")
    with pytest.raises(PlanError, match="字段"):
        await storage.mutate(
            OWNER, "fields", {"fields": [fields[0], fields[2]]}, pid, 3, "remove"
        )
    await storage.mutate(OWNER, "view", {"visible": False}, pid, 3, "hide")
    snapshot = await storage.snapshot(pid, OWNER)
    view = build_view(snapshot, {"page_size": 1}, OWNER.user)
    assert not view["view"]["visible"] and view["blocks"][0]["value"] == 30
    await storage.mutate(
        OWNER,
        "block_delete",
        {"block_id": snapshot["blocks"][0]["block_id"]},
        pid,
        4,
        "delete-block",
    )
    assert len((await storage.snapshot(pid, OWNER))["records"]) == 2


async def test_goal_target_binding_and_stats_ignore_paging(storage):
    created = await storage.mutate(
        OWNER,
        "create",
        {"name": "目标", "preset": "goal", "target": 100},
        message_key="c",
    )
    pid = created["plan_id"]
    await storage.mutate(
        OWNER,
        "add_records",
        {"records": [{"values": {"amount": 10}}, {"values": {"amount": 20}}]},
        pid,
        1,
        "r",
    )
    await storage.mutate(OWNER, "rules", {"target": 60}, pid, 2, "target")
    data = build_view(await storage.snapshot(pid, OWNER), {"page_size": 1}, OWNER.user)
    assert len(data["view"]["rows"]) == 1
    assert data["blocks"][0]["progress"] == 50


async def test_checkin_rules_dates_and_per_user_uniqueness(storage, monkeypatch):
    fixed = date(2026, 9, 21)
    monkeypatch.setattr("astrbot_plugin_custom_plan.domain.today", lambda p: fixed)
    monkeypatch.setattr(
        "astrbot_plugin_custom_plan.presentation.today", lambda p: fixed
    )
    owner = Actor("qq-1", "owner", "g1")
    member = Actor("qq-1", "member", "g1")
    created = await storage.mutate(
        owner,
        "create",
        {"name": "打卡", "preset": "checkin", "mode": "public"},
        message_key="c",
    )
    pid = created["plan_id"]
    await storage.mutate(
        owner,
        "add_records",
        {"records": [{"values": {"date": "2026-09-20", "amount": 1}}]},
        pid,
        1,
        "yesterday",
    )
    await storage.mutate(
        owner,
        "rules",
        {"threshold": 2, "effective_from": "2026-09-21"},
        pid,
        2,
        "threshold",
    )
    payload = {"records": [{"values": {}}]}
    await storage.mutate(owner, "add_records", payload, pid, 3, "today")
    with pytest.raises(PlanError, match="同一天"):
        await storage.mutate(owner, "add_records", payload, pid, 4, "duplicate")
    await storage.mutate(member, "add_records", payload, pid, 4, "member-today")
    with pytest.raises(PlanError, match="提前打卡"):
        await storage.mutate(
            owner,
            "add_records",
            {
                "records": [
                    {"values": {"date": (fixed + timedelta(days=1)).isoformat()}}
                ]
            },
            pid,
            5,
            "future",
        )
    await storage.mutate(owner, "view", {"type": "checkin"}, pid, 5, "view")
    data = build_view(await storage.snapshot(pid, owner), {}, owner.user)["view"]
    assert not data["current"]["done"]
    assert data["cells"][-2]["state"] == "done"
    assert data["current"]["amount"] == 1
    with pytest.raises(PlanError, match="不能早于今天"):
        await storage.mutate(
            owner,
            "rules",
            {"threshold": 10, "effective_from": "2026-09-19"},
            pid,
            6,
            "past-rule",
        )


async def test_task_completion_reopen_and_immutable_author(storage):
    created = await storage.mutate(
        OWNER, "create", {"name": "任务", "preset": "todo"}, message_key="c"
    )
    pid = created["plan_id"]
    added = await storage.mutate(
        OWNER,
        "add_records",
        {"records": [{"values": {"title": "订酒店"}}]},
        pid,
        1,
        "add",
    )
    rid = added["changed_ids"][0]
    await storage.mutate(
        OWNER,
        "update_records",
        {"records": [{"record_id": rid, "values": {"status": "已完成"}}]},
        pid,
        2,
        "done",
    )
    task = (await storage.snapshot(pid, OWNER))["records"][0]
    assert task["completed_at"]
    await storage.mutate(
        OWNER,
        "update_records",
        {"records": [{"record_id": rid, "values": {"due": "2026-10-01"}}]},
        pid,
        3,
        "move",
    )
    assert (await storage.snapshot(pid, OWNER))["records"][0]["completed_at"] == task[
        "completed_at"
    ]
    await storage.mutate(
        OWNER,
        "update_records",
        {"records": [{"record_id": rid, "values": {"status": "待完成"}}]},
        pid,
        4,
        "reopen",
    )
    assert (await storage.snapshot(pid, OWNER))["records"][0]["completed_at"] is None
    with pytest.raises(PlanError):
        await storage.mutate(
            OWNER,
            "add_records",
            {"records": [{"author": "other", "values": {"title": "spoof"}}]},
            pid,
            5,
            "spoof",
        )


async def test_query_filters_and_cross_scope_secrecy(storage):
    created = await storage.mutate(
        OWNER, "create", {"name": "秘密", "preset": "todo"}, message_key="c"
    )
    pid = created["plan_id"]
    await storage.mutate(
        OWNER,
        "add_records",
        {
            "records": [
                {"values": {"title": "a", "due": "2026-09-21"}},
                {"values": {"title": "b"}},
            ]
        },
        pid,
        1,
        "r",
    )
    result = await storage.query(
        OWNER,
        {
            "plan_id": pid,
            "filters": {
                "date_field": "due",
                "start": "2026-09-01",
                "end": "2026-09-30",
            },
        },
    )
    assert result["total"] == 1 and result["records"][0]["values"]["title"] == "a"
    assert "today" in result
    other = Actor("qq-1", "other")
    assert (await storage.query(other, {}))["plans"] == []
    with pytest.raises(PlanError, match="权限"):
        await storage.query(other, {"plan_id": pid})


async def test_block_instances_reorder_and_undo(storage):
    created = await storage.mutate(OWNER, "create", {"name": "笔记"}, message_key="c")
    pid = created["plan_id"]
    first = await storage.mutate(
        OWNER, "block_add", {"type": "notes", "config": {"text": "甲"}}, pid, 1, "a"
    )
    second = await storage.mutate(
        OWNER, "block_add", {"type": "notes", "config": {"text": "乙"}}, pid, 2, "b"
    )
    order = [second["changed_ids"][0], first["changed_ids"][0]]
    await storage.mutate(OWNER, "block_order", {"block_ids": order}, pid, 3, "order")
    assert [
        b["config"]["text"] for b in (await storage.snapshot(pid, OWNER))["blocks"]
    ] == ["乙", "甲"]
    await storage.mutate(OWNER, "undo", {}, pid, 4, "undo")
    assert [
        b["config"]["text"] for b in (await storage.snapshot(pid, OWNER))["blocks"]
    ] == ["甲", "乙"]


@pytest.mark.parametrize("kind", ["checkin", "goal", "todo"])
async def test_generic_plan_binds_existing_data_without_copy(storage, kind):
    created = await storage.mutate(
        OWNER, "create", {"name": "空白起步"}, message_key="c"
    )
    pid = created["plan_id"]
    fields = [
        {"field_id": "d", "name": "日期", "type": "date"},
        {"field_id": "n", "name": "数量", "type": "number"},
        {"field_id": "t", "name": "标题", "type": "text"},
        {
            "field_id": "s",
            "name": "状态",
            "type": "status",
            "options": ["待完成", "已完成"],
        },
    ]
    await storage.mutate(OWNER, "fields", {"fields": fields}, pid, 1, "fields")
    added = await storage.mutate(
        OWNER,
        "add_records",
        {
            "records": [
                {"values": {"d": "2026-09-01", "n": 1, "t": "学习", "s": "已完成"}}
            ]
        },
        pid,
        2,
        "record",
    )
    rules = (
        {
            "kind": kind,
            "title_field": "t",
            "status_field": "s",
            "done_value": "已完成",
            "pending_value": "待完成",
        }
        if kind == "todo"
        else {"kind": kind, "date_field": "d", "amount_field": "n"}
    )
    await storage.mutate(OWNER, "rules", rules, pid, 3, "bind")
    snapshot = await storage.snapshot(pid, OWNER)
    assert snapshot["records"][0]["record_id"] == added["changed_ids"][0]
    assert snapshot["rules"]["kind"] == kind
    if kind == "todo":
        assert snapshot["records"][0]["completed_at"]
    await storage.mutate(OWNER, "undo", {}, pid, 4, "undo")
    snapshot = await storage.snapshot(pid, OWNER)
    assert snapshot["rules"]["kind"] == "generic"
    assert snapshot["records"][0]["record_id"] == added["changed_ids"][0]
