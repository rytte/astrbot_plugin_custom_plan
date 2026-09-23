import asyncio
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from astrbot_plugin_custom_plan import domain, supervision
from astrbot_plugin_custom_plan import storage as storage_module
from astrbot_plugin_custom_plan.domain import Actor, PlanError
from astrbot_plugin_custom_plan.renderer import LocalRenderer
from astrbot_plugin_custom_plan.storage import Storage

OWNER = Actor("qq-1", "owner")


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 9, 23, 4, tzinfo=timezone.utc))
    monkeypatch.setattr(supervision, "utc_now", lambda: clock.now)
    monkeypatch.setattr(domain, "now_iso", lambda: clock.now.isoformat())
    monkeypatch.setattr(storage_module, "now_iso", lambda: clock.now.isoformat())
    monkeypatch.setattr(
        domain,
        "today",
        lambda plan: clock.now.astimezone(ZoneInfo(plan["timezone"])).date(),
    )
    return clock


async def create(storage, preset="goal", actor=OWNER, **params):
    return (
        await storage.mutate(
            actor,
            "create",
            {"name": "阅读计划", "preset": preset, **params},
            message_key=uuid4().hex,
        )
    )["plan_id"]


async def change(storage, pid, action, params=None, actor=OWNER, key=None):
    plan = await storage.query(actor, {"plan_id": pid})
    return await storage.mutate(
        actor,
        action,
        {} if params is None else params,
        pid,
        plan["revision"],
        key or uuid4().hex,
    )


async def enable(storage, pid, actor=OWNER, **params):
    plan = await storage.query(actor, {"plan_id": pid})
    params.setdefault(
        "execution_standard", "每天完成计划要求的任务，并按实际进度如实记录。"
    )
    preview = await storage.supervision_preview(
        actor, pid, plan["revision"], {"end_date": None, **params}, "preview"
    )
    result = await change(
        storage,
        pid,
        "supervision_enable",
        {"confirmation_token": preview["confirmation_token"]},
        actor=actor,
    )
    assert result["supervision"]["active"]
    return preview


async def add(storage, pid, values, actor=OWNER):
    return (
        await change(
            storage, pid, "add_records", {"records": [{"values": values}]}, actor
        )
    )["changed_ids"][0]


async def test_rules_are_confirmed_frozen_and_only_read_explicitly(
    storage, tmp_path, monkeypatch, clock
):
    path = tmp_path / "rules.txt"
    path.write_text("固定行为规则一", encoding="utf-8")
    monkeypatch.setattr(supervision, "RULES_FILE", path)
    pid = await create(storage)
    preview = await storage.supervision_preview(
        OWNER,
        pid,
        1,
        {
            "end_date": None,
            "execution_standard": "每天阅读至少20页，按实际阅读量记录。",
        },
        "preview",
    )
    assert not (await storage.query(OWNER, {"plan_id": pid}))["supervision"]["active"]
    with pytest.raises(PlanError, match="下一条消息"):
        await change(
            storage,
            pid,
            "supervision_enable",
            {"confirmation_token": preview["confirmation_token"]},
            key="preview",
        )
    path.write_text("固定行为规则二", encoding="utf-8")
    await change(
        storage,
        pid,
        "supervision_enable",
        {"confirmation_token": preview["confirmation_token"]},
    )
    reopened = Storage(storage.path)
    await reopened.initialize()
    loaded = await reopened.supervision_rules(OWNER, pid)
    assert loaded["rules_text"] == preview["rules_text"]
    assert (
        loaded["constraints"]["execution_standard"]
        == "每天阅读至少20页，按实际阅读量记录。"
    )
    assert (
        "固定行为规则一" in loaded["rules_text"]
        and "固定行为规则二" not in loaded["rules_text"]
    )
    for result in (
        await reopened.query(OWNER, {}),
        await reopened.query(OWNER, {"plan_id": pid}),
        await reopened.query(OWNER, {"plan_id": pid, "supervision_history": True}),
    ):
        assert "rules_text" not in json.dumps(result)
        assert "固定行为规则一" not in json.dumps(result, ensure_ascii=False)
    with pytest.raises(PlanError, match="权限"):
        await reopened.supervision_rules(Actor("qq-1", "other"), pid)


async def test_preview_is_invalidated_by_any_plan_change(storage, clock):
    pid = await create(storage)
    preview = await storage.supervision_preview(
        OWNER,
        pid,
        1,
        {
            "end_date": None,
            "execution_standard": "满足目标并如实记录。",
        },
        "preview",
    )
    await change(storage, pid, "rules", {"target": 5})
    with pytest.raises(PlanError, match="预览已失效"):
        await change(
            storage,
            pid,
            "supervision_enable",
            {"confirmation_token": preview["confirmation_token"]},
        )
    assert not (await storage.query(OWNER, {"plan_id": pid}))["supervision"]["active"]


@pytest.mark.parametrize(
    "params,match",
    [
        ({}, "明确 end_date"),
        ({"end_date": "bad"}, "日期"),
        (
            {
                "end_date": None,
                "execution_standard": "满足目标并如实记录。",
                "backfill_days": True,
            },
            "backfill_days",
        ),
        (
            {
                "end_date": None,
                "execution_standard": "满足目标并如实记录。",
                "backfill_days": -1,
            },
            "backfill_days",
        ),
        (
            {
                "end_date": None,
                "execution_standard": "满足目标并如实记录。",
                "protected_fields": ["missing"],
            },
            "字段",
        ),
        ({"end_date": None}, "非空"),
        ({"end_date": None, "execution_standard": "  "}, "非空"),
    ],
)
async def test_invalid_agreement_rejected(storage, params, match):
    pid = await create(storage)
    with pytest.raises(PlanError, match=match):
        await storage.supervision_preview(OWNER, pid, 1, params, "p")


async def test_missing_or_empty_rule_file_fails_without_enabling(
    storage, monkeypatch, tmp_path
):
    pid = await create(storage)
    path = tmp_path / "missing.txt"
    monkeypatch.setattr(supervision, "RULES_FILE", path)
    with pytest.raises(PlanError, match="规则文件"):
        await enable(storage, pid)
    path.write_text(" ", encoding="utf-8")
    with pytest.raises(PlanError, match="规则文件"):
        await enable(storage, pid)


@pytest.mark.parametrize(
    "action,params",
    [
        ("update", {"goal": "降低要求"}),
        ("update", {"name": "另一个目标"}),
        ("update", {"mode": "shared"}),
        ("update", {"timezone": "UTC"}),
        ("rules", {"target": 1}),
        ("delete", {}),
        ("undo", {}),
    ],
)
async def test_protected_writes_are_atomic_and_cannot_undo_supervision(
    storage, action, params, clock
):
    pid = await create(storage)
    await enable(storage, pid)
    before = await storage.snapshot(pid, OWNER)
    with pytest.raises(PlanError):
        await change(storage, pid, action, params)
    assert await storage.snapshot(pid, OWNER) == before


async def test_fields_and_appearance_can_extend_without_weakening_rules(storage, clock):
    pid = await create(storage)
    await enable(storage, pid)
    plan = await storage.snapshot(pid, OWNER)
    definitions = deepcopy(plan["fields"])
    definitions.append({"field_id": "reflection", "name": "感想", "type": "text"})
    await change(storage, pid, "fields", {"fields": definitions})
    await change(
        storage, pid, "update", {"render_theme": "midnight", "render_layout": "desktop"}
    )
    with pytest.raises(PlanError, match="已有字段"):
        await change(storage, pid, "fields", {"fields": definitions[:-1]})
    modified = deepcopy(definitions)
    modified[0]["name"] = "换个含义"
    with pytest.raises(PlanError, match="已有字段"):
        await change(storage, pid, "fields", {"fields": modified})
    definitions.append(
        {"field_id": "extra", "name": "强制项", "type": "text", "required": True}
    )
    with pytest.raises(PlanError, match="非必填"):
        await change(storage, pid, "fields", {"fields": definitions})
    rules = await storage.supervision_rules(OWNER, pid)
    assert "reflection" not in rules["constraints"]["protected_fields"]


async def test_progress_correction_window_audit_and_batch_rollback(storage, clock):
    pid = await create(storage)
    await enable(storage, pid)
    first = await add(storage, pid, {"amount": 20})
    item = {"record_id": first, "values": {"amount": 2}}
    with pytest.raises(PlanError, match="correct"):
        await change(storage, pid, "update_records", {"records": [item]})
    with pytest.raises(PlanError, match="纠错原因"):
        await change(storage, pid, "correct_records", {"records": [item]})
    clock.now += timedelta(minutes=9, seconds=59)
    await change(
        storage, pid, "correct_records", {"records": [item], "reason": "多输了一个零"}
    )
    history = await storage.query(OWNER, {"plan_id": pid, "supervision_history": True})
    correction = history["entries"][0]["document"]["corrections"][0]
    assert correction["before"]["values"]["amount"] == 20
    assert correction["after"]["values"]["amount"] == 2
    assert correction["reason"] == "多输了一个零"
    assert history["entries"][0]["actor"] == OWNER.user
    clock.now += timedelta(seconds=1)
    with pytest.raises(PlanError, match="10分钟"):
        await change(
            storage,
            pid,
            "correct_records",
            {
                "records": [{"record_id": first, "values": {"amount": 1}}],
                "reason": "再次纠错",
            },
        )
    second = await add(storage, pid, {"amount": 8})
    before = await storage.snapshot(pid, OWNER)
    with pytest.raises(PlanError, match="10分钟"):
        await change(
            storage,
            pid,
            "correct_records",
            {
                "records": [
                    {"record_id": second, "values": {"amount": 1}},
                    {"record_id": first, "values": {"amount": 1}},
                ],
                "reason": "批量",
            },
        )
    assert await storage.snapshot(pid, OWNER) == before
    # Increasing today's amount is ordinary progress; it does not reopen corrections.
    await change(
        storage,
        pid,
        "update_records",
        {"records": [{"record_id": first, "values": {"amount": 3}}]},
    )
    with pytest.raises(PlanError, match="禁止删除"):
        await change(
            storage, pid, "delete_records", {"records": [{"record_id": first}]}
        )
    for i in range(21):
        await change(
            storage,
            pid,
            "update_records",
            {"records": [{"record_id": first, "values": {"note": str(i)}}]},
        )
    assert (await storage.query(OWNER, {"plan_id": pid, "supervision_history": True}))[
        "entries"
    ][0] == history["entries"][0]


async def test_todo_completion_corrections_do_not_refresh_window(storage, clock):
    pid = await create(storage, "todo")
    record = await add(storage, pid, {"title": "阅读"})
    await enable(storage, pid)
    clock.now += timedelta(days=1)
    await change(
        storage,
        pid,
        "update_records",
        {"records": [{"record_id": record, "values": {"status": "已完成"}}]},
    )
    clock.now += timedelta(minutes=5)
    await change(
        storage,
        pid,
        "correct_records",
        {
            "records": [{"record_id": record, "values": {"status": "待完成"}}],
            "reason": "点错状态",
        },
    )
    await change(
        storage,
        pid,
        "update_records",
        {"records": [{"record_id": record, "values": {"status": "已完成"}}]},
    )
    clock.now += timedelta(minutes=5)
    before = await storage.snapshot(pid, OWNER)
    with pytest.raises(PlanError, match="刷新任务完成时间"):
        await change(
            storage,
            pid,
            "update_records",
            {
                "records": [
                    {"record_id": record, "values": {"status": "待完成"}},
                    {"record_id": record, "values": {"status": "已完成"}},
                ]
            },
        )
    assert await storage.snapshot(pid, OWNER) == before
    with pytest.raises(PlanError, match="10分钟"):
        await change(
            storage,
            pid,
            "correct_records",
            {
                "records": [{"record_id": record, "values": {"status": "待完成"}}],
                "reason": "不能刷新",
            },
        )
    with pytest.raises(PlanError):
        await change(
            storage,
            pid,
            "update_records",
            {"records": [{"record_id": record, "values": {"due": "2099-01-01"}}]},
        )


async def test_backfill_range_is_locked_and_actual_submission_time_is_kept(
    storage, clock
):
    pid = await create(storage, "checkin")
    await enable(storage, pid)
    day = domain.today(await storage.snapshot(pid, OWNER))
    with pytest.raises(PlanError, match="补签范围"):
        await add(storage, pid, {"date": (day - timedelta(days=1)).isoformat()})
    second = await create(storage, "checkin")
    await enable(storage, second, backfill_days=2)
    rid = await add(storage, second, {"date": (day - timedelta(days=2)).isoformat()})
    rec = (await storage.query(OWNER, {"plan_id": second}))["records"][0]
    assert (
        rec["record_id"] == rid
        and datetime.fromisoformat(rec["created_at"]) == clock.now
    )
    with pytest.raises(PlanError):
        await add(storage, second, {"date": (day + timedelta(days=1)).isoformat()})
    with pytest.raises(PlanError, match="补签范围"):
        await add(storage, second, {"date": (day - timedelta(days=3)).isoformat()})
    with pytest.raises(PlanError):
        await change(storage, second, "rules", {"allow_backfill": False})


async def test_exit_requires_owner_cooldown_and_later_confirmation(storage, clock):
    pid = await create(storage)
    await enable(storage, pid)
    request = await change(storage, pid, "supervision_request_exit", key="exit-request")
    requested_at = request["supervision"]["exit_requested_at"]
    clock.now += timedelta(hours=23, minutes=59, seconds=59)
    repeat = await change(storage, pid, "supervision_request_exit")
    assert repeat["supervision"]["exit_requested_at"] == requested_at
    with pytest.raises(PlanError, match="24小时"):
        await change(storage, pid, "supervision_confirm_exit")
    with pytest.raises(PlanError):
        await change(storage, pid, "rules", {"target": 1})
    clock.now += timedelta(seconds=1)
    assert (await storage.query(OWNER, {"plan_id": pid}))["supervision"]["active"]
    with pytest.raises(PlanError, match="新消息"):
        await change(storage, pid, "supervision_confirm_exit", key="exit-request")
    released = await change(storage, pid, "supervision_confirm_exit")
    assert released["supervision"]["status"] == "released"
    with pytest.raises(PlanError, match="没有可撤销"):
        await change(storage, pid, "undo")
    await change(storage, pid, "rules", {"target": 1})
    # Undoing a normal change after release cannot re-enable supervision.
    await change(storage, pid, "undo")
    assert not (await storage.query(OWNER, {"plan_id": pid}))["supervision"]["active"]


async def test_cancel_exit_and_request_again_starts_new_cooldown(storage, clock):
    pid = await create(storage)
    await enable(storage, pid)
    await change(storage, pid, "supervision_request_exit")
    clock.now += timedelta(hours=20)
    cancelled = await change(storage, pid, "supervision_cancel_exit")
    assert cancelled["supervision"]["exit_ready_at"] is None
    new = await change(storage, pid, "supervision_request_exit")
    assert datetime.fromisoformat(
        new["supervision"]["exit_ready_at"]
    ) == clock.now + timedelta(hours=24)


async def test_group_members_cannot_enable_exit_or_bypass_restrictions(storage, clock):
    owner = Actor("qq-1", "owner", "g")
    member = Actor("qq-1", "member", "g")
    pid = await create(storage, "goal", owner, mode="public")
    with pytest.raises(PlanError, match="创建者"):
        await storage.supervision_preview(
            member,
            pid,
            1,
            {
                "end_date": None,
                "execution_standard": "满足目标并如实记录。",
            },
            "m",
        )
    await enable(storage, pid, owner)
    assert (await storage.supervision_rules(member, pid))["supervision"]["active"]
    await add(storage, pid, {"amount": 1}, member)
    with pytest.raises(PlanError, match="创建者"):
        await change(storage, pid, "supervision_request_exit", actor=member)
    with pytest.raises(PlanError):
        await change(storage, pid, "rules", {"target": 1}, actor=member)
    with pytest.raises(PlanError, match="权限"):
        await storage.supervision_rules(OWNER, pid)


async def test_expiration_uses_plan_timezone_and_removes_shield(
    storage, clock, tmp_path
):
    clock.now = datetime(2026, 9, 23, 15, 59, 59, tzinfo=timezone.utc)
    pid = await create(storage)
    await enable(storage, pid, end_date="2026-09-23")
    renderer = LocalRenderer(tmp_path, {})
    assert '<svg class="supervision-mark"' in renderer.html(
        await storage.snapshot(pid, OWNER), {}, OWNER.user
    )
    clock.now += timedelta(seconds=1)
    plan = await storage.snapshot(pid, OWNER)
    assert plan["supervision"]["status"] == "expired"
    assert '<svg class="supervision-mark"' not in renderer.html(plan, {}, OWNER.user)
    await change(storage, pid, "rules", {"target": 1})


async def test_generic_plan_requires_explicit_protected_fields(storage, clock):
    pid = await create(storage, "generic")
    await change(
        storage,
        pid,
        "fields",
        {
            "fields": [
                {"field_id": "execution", "name": "执行", "type": "number"},
                {"field_id": "memo", "name": "备注", "type": "text"},
            ]
        },
    )
    with pytest.raises(PlanError, match="protected_fields"):
        await enable(storage, pid)
    await enable(storage, pid, protected_fields=["execution"])
    rid = await add(storage, pid, {"execution": 10})
    clock.now += timedelta(minutes=15)
    await change(
        storage,
        pid,
        "update_records",
        {"records": [{"record_id": rid, "values": {"memo": "补充说明"}}]},
    )
    with pytest.raises(PlanError):
        await change(
            storage,
            pid,
            "update_records",
            {"records": [{"record_id": rid, "values": {"execution": 1}}]},
        )


async def test_v3_upgrade_preserves_documents_and_undo_history(storage):
    pid = await create(storage)
    await change(storage, pid, "update", {"name": "更新后"})
    with sqlite3.connect(storage.path) as connection:
        documents = connection.execute("SELECT * FROM plans").fetchall()
        changes = connection.execute("SELECT * FROM changes").fetchall()
        for table in ("supervision", "supervision_log", "supervision_previews"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=3")
    await storage.initialize()
    await storage.initialize()
    with sqlite3.connect(storage.path) as connection:
        assert connection.execute("SELECT * FROM plans").fetchall() == documents
        assert connection.execute("SELECT * FROM changes").fetchall() == changes
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
    assert (await storage.query(OWNER, {"plan_id": pid}))["supervision"][
        "status"
    ] == "off"
    await change(storage, pid, "undo")
    assert (await storage.query(OWNER, {"plan_id": pid}))["name"] == "阅读计划"


async def test_enable_is_atomic_and_serialized(storage, clock):
    pid = await create(storage)
    preview = await storage.supervision_preview(
        OWNER,
        pid,
        1,
        {
            "end_date": None,
            "execution_standard": "满足目标并如实记录。",
        },
        "p",
    )
    results = await asyncio.gather(
        *(
            storage.mutate(
                OWNER,
                "supervision_enable",
                {"confirmation_token": preview["confirmation_token"]},
                pid,
                1,
                f"c{i}",
            )
            for i in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, PlanError) for result in results) == 1
    assert (await storage.query(OWNER, {"plan_id": pid}))["revision"] == 2
    with sqlite3.connect(storage.path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM supervision_log").fetchone()[0]
            == 1
        )


async def test_purge_removes_supervision_agreements_and_audit(storage, clock):
    pid = await create(storage)
    await enable(storage, pid)
    await change(storage, pid, "supervision_request_exit")
    clock.now += timedelta(hours=24)
    await change(storage, pid, "supervision_confirm_exit")
    await change(storage, pid, "delete")
    with sqlite3.connect(storage.path) as connection:
        connection.execute(
            "UPDATE changes SET created_at=? WHERE plan_id=? AND action='delete'",
            ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(), pid),
        )
    await storage.query(OWNER, {})
    with sqlite3.connect(storage.path) as connection:
        for table in (
            "plans",
            "changes",
            "requests",
            "supervision",
            "supervision_log",
            "supervision_previews",
        ):
            assert (
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE plan_id=?", (pid,)
                ).fetchone()[0]
                == 0
            )


async def test_supervised_view_cannot_substitute_completion_criteria(storage, clock):
    pid = await create(storage, "todo")
    await enable(storage, pid)
    fields = (await storage.query(OWNER, {"plan_id": pid}))["fields"]
    fields.append(
        {
            "field_id": "extra_status",
            "name": "辅助状态",
            "type": "status",
            "options": ["是", "否"],
        }
    )
    await change(storage, pid, "fields", {"fields": fields})
    with pytest.raises(PlanError, match="完成依据"):
        await change(
            storage,
            pid,
            "view",
            {"type": "todo", "status_field": "extra_status", "done_value": "是"},
        )
    await change(storage, pid, "view", {"type": "todo"})


async def test_batch_corrections_are_audited_per_record_and_replay_is_safe(
    storage, clock
):
    pid = await create(storage)
    await enable(storage, pid)
    first = await add(storage, pid, {"amount": 100})
    second = await add(storage, pid, {"amount": 200})
    params = {
        "records": [
            {"record_id": first, "values": {"amount": 10}},
            {"record_id": second, "values": {"amount": 20}},
        ],
        "reason": "两个数都多写了零",
    }
    result = await change(storage, pid, "correct_records", params, key="correct-once")
    replayed = await change(storage, pid, "correct_records", params, key="correct-once")
    assert replayed["replayed"] and replayed["revision"] == result["revision"]
    history = [
        await storage.query(
            OWNER, {"plan_id": pid, "supervision_history": True, "page": p}
        )
        for p in (1, 2)
    ]
    assert history[0]["total"] == 3
    assert {
        item["entries"][0]["document"]["corrections"][0]["record_id"]
        for item in history
    } == {first, second}
    assert all(item["entries"][0]["revision"] == result["revision"] for item in history)
