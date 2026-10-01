import asyncio
import base64
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest
from astrbot_plugin_custom_plan.domain import Actor, PlanError, reference_basis
from astrbot_plugin_custom_plan.references import encode_images
from astrbot_plugin_custom_plan.renderer import LocalRenderer
from astrbot_plugin_custom_plan.storage import encode
from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image

OWNER = Actor("qq-1", "owner")


def png():
    image = Image.new("RGBA", (40, 30))
    image.putdata(
        [
            (index % 256, index * 7 % 256, index * 13 % 256, index % 256)
            for index in range(1200)
        ]
    )
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


async def create(storage, actor=OWNER, **params):
    result = await storage.mutate(
        actor,
        "create",
        {"name": "图片计划", "preset": "checkin", **params},
        message_key="create",
    )
    return result["plan_id"]


async def add_reference(storage, pid, actor=OWNER, **params):
    revision = (await storage.snapshot(pid, actor))["revision"]
    image = storage.media.prepare(png(), lossless=True)
    result = await storage.mutate(
        actor,
        "reference_add",
        {"image_index": 1, "purpose": "核对截图日期与单位", **params},
        pid,
        revision,
        f"reference-{revision}",
        {1: image},
    )
    return result["changed_ids"][0], image


async def enable(storage, pid, actor=OWNER):
    revision = (await storage.snapshot(pid, actor))["revision"]
    preview = await storage.supervision_preview(
        actor,
        pid,
        revision,
        {"end_date": None, "execution_standard": "当天提交并核对日期及单位"},
        "preview",
    )
    await storage.mutate(
        actor,
        "supervision_enable",
        {"confirmation_token": preview["confirmation_token"]},
        pid,
        revision,
        "confirm",
    )
    return preview


def test_reference_encoding_is_lossless_and_removes_metadata(storage):
    original = png()
    image = storage.media.prepare(original, lossless=True)
    with (
        Image.open(BytesIO(original)) as source,
        Image.open(BytesIO(image.data)) as saved,
    ):
        assert saved.convert("RGBA").tobytes() == source.tobytes()
        assert not saved.getexif()
        assert "icc_profile" not in saved.info


async def test_reference_crud_metadata_permissions_and_undo(storage):
    pid = await create(storage)
    ref_id, image = await add_reference(storage, pid, title="表格样例")
    query = await storage.query(OWNER, {"plan_id": pid})
    reference = query["references"][0]
    assert reference["reference_id"] == ref_id
    assert reference["images"] == [image.image_id]
    assert not reference["as_basis"]
    assert image.image_id in query["image_metadata"]
    assert query["image_limits"]["references_per_plan"] == 5
    assert "path" not in encode(query)
    await storage.mutate(
        OWNER,
        "reference_update",
        {"reference_id": ref_id, "purpose": "更新用途"},
        pid,
        2,
        "update",
    )
    await storage.mutate(
        OWNER, "reference_delete", {"reference_id": ref_id}, pid, 3, "delete"
    )
    assert not (await storage.snapshot(pid, OWNER))["references"]
    await storage.mutate(OWNER, "undo", {}, pid, 4, "undo")
    assert (await storage.snapshot(pid, OWNER))["references"][0][
        "purpose"
    ] == "更新用途"
    with pytest.raises(PlanError, match="权限"):
        await storage.visual_snapshot(Actor("qq-1", "other"), pid)
    with pytest.raises(PlanError, match="权限"):
        await storage.visual_snapshot(Actor("other-platform", "owner"), pid)


@pytest.mark.parametrize(
    "params",
    [
        {"images": ["img_" + "a" * 32]},
        {"image_id": "img_" + "a" * 32},
        {"image_index": True},
        {"purpose": ""},
        {"as_basis": True},
        {"checks": ["日期"]},
        {"as_basis": "true"},
    ],
)
async def test_reference_external_validation(storage, params):
    pid = await create(storage)
    with pytest.raises(PlanError):
        await add_reference(storage, pid, **params)
    assert (await storage.snapshot(pid, OWNER))["revision"] == 1
    assert list(storage.media.directory.iterdir()) == []


async def test_reference_count_and_shared_storage_quota(storage):
    pid = await create(storage)
    storage.media.references_per_plan = 1
    ref_id, _ = await add_reference(storage, pid)
    with pytest.raises(PlanError, match="最多保留 1"):
        await add_reference(storage, pid)
    image = storage.media.prepare(png(), lossless=True)
    storage.media.image_plan_quota_mb = 0
    with pytest.raises(PlanError, match="额度"):
        await storage.mutate(
            OWNER,
            "reference_update",
            {"reference_id": ref_id, "image_index": 1},
            pid,
            2,
            "quota",
            {1: image},
        )
    assert not storage.media.path(image.image_id).exists()


async def test_v6_migration_preserves_old_documents_and_snapshots(storage):
    pid = await create(storage)
    await storage.mutate(OWNER, "update", {"goal": "保留旧文档"}, pid, 1, "update")
    with sqlite3.connect(storage.path) as connection:
        for table, column in (("plans", "document"), ("changes", "previous")):
            for row_id, value in connection.execute(
                f"SELECT rowid,{column} FROM {table} WHERE {column} IS NOT NULL"
            ).fetchall():
                document = json.loads(value)
                document.pop("references", None)
                connection.execute(
                    f"UPDATE {table} SET {column}=? WHERE rowid=?",
                    (encode(document), row_id),
                )
        before = (
            connection.execute("SELECT document FROM plans").fetchall(),
            connection.execute("SELECT previous FROM changes").fetchall(),
        )
        connection.execute("DROP TABLE IF EXISTS image_reviews")
        connection.execute("PRAGMA user_version=6")
    await storage.initialize()
    with sqlite3.connect(storage.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert before == (
            connection.execute("SELECT document FROM plans").fetchall(),
            connection.execute("SELECT previous FROM changes").fetchall(),
        )
    assert (await storage.query(OWNER, {"plan_id": pid}))["references"] == []


async def test_reference_pagination_is_independent_from_records(storage):
    pid = await create(storage)
    storage.media.references_per_plan = 6
    for index in range(6):
        await add_reference(storage, pid, title=f"参考图 {index}")
    first = await storage.query(OWNER, {"plan_id": pid})
    second = await storage.query(OWNER, {"plan_id": pid, "reference_page": 2})
    assert first["reference_total"] == second["reference_total"] == 6
    assert len(first["references"]) == 5 and len(second["references"]) == 1
    assert first["records"] == second["records"] == []
    assert second["references"][0]["title"] == "参考图 5"
    for params in [
        {"reference_page": 2},
        {"plan_id": pid, "reference_page": True},
        {"plan_id": pid, "reference_page": 0},
    ]:
        with pytest.raises(PlanError):
            await storage.query(OWNER, params)


async def test_supervision_freezes_basis_but_not_auxiliary_references(storage):
    pid = await create(storage)
    ref_id, _ = await add_reference(
        storage, pid, checks="日期为当天；单位为公里", as_basis=True
    )
    aux_id, _ = await add_reference(storage, pid, title="辅助示意")
    preview = await enable(storage, pid)
    assert preview["constraints"]["reference_basis"] == reference_basis(
        await storage.snapshot(pid, OWNER)
    )
    assert "冻结" in preview["rules_text"]
    for action, payload in [
        ("reference_update", {"reference_id": ref_id, "as_basis": False}),
        ("reference_update", {"reference_id": ref_id, "purpose": "新标准"}),
        ("reference_update", {"reference_id": ref_id, "checks": "单位为米"}),
        ("reference_update", {"reference_id": ref_id, "title": "改标题"}),
        ("reference_delete", {"reference_id": ref_id}),
        (
            "reference_update",
            {"reference_id": aux_id, "checks": "日期", "as_basis": True},
        ),
    ]:
        with pytest.raises(PlanError, match="参考依据"):
            await storage.mutate(OWNER, action, payload, pid, 4, encode(payload))
    replacement = storage.media.prepare(png(), lossless=True)
    with pytest.raises(PlanError, match="参考依据"):
        await storage.mutate(
            OWNER,
            "reference_update",
            {"reference_id": ref_id, "image_index": 1},
            pid,
            4,
            "replace",
            {1: replacement},
        )
    await storage.mutate(
        OWNER,
        "reference_update",
        {"reference_id": aux_id, "purpose": "仅调整辅助说明"},
        pid,
        4,
        "aux-update",
    )
    snapshot = await storage.visual_snapshot(OWNER, pid, [aux_id], include_basis=True)
    assert {item["reference_id"] for item in snapshot["references"]} == {aux_id, ref_id}
    await storage.mutate(
        OWNER, "reference_delete", {"reference_id": aux_id}, pid, 5, "aux-delete"
    )
    assert len((await storage.snapshot(pid, OWNER))["references"]) == 1


async def test_old_supervision_without_basis_is_not_rewritten(storage):
    pid = await create(storage)
    await enable(storage, pid)
    with sqlite3.connect(storage.path) as connection:
        state = json.loads(
            connection.execute("SELECT document FROM supervision").fetchone()[0]
        )
        state["constraints"].pop("reference_basis")
        state["rules_text"] = "原有监督协议"
        connection.execute("UPDATE supervision SET document=?", (encode(state),))
    await add_reference(storage, pid)
    assert (await storage.supervision_rules(OWNER, pid))["rules_text"] == "原有监督协议"
    with pytest.raises(PlanError, match="参考依据"):
        await add_reference(storage, pid, checks="新依据", as_basis=True)


async def test_v7_migration_preserves_legacy_reviews_and_referenced_images(storage):
    pid = await create(storage)
    ref_id, image = await add_reference(storage, pid)
    evidence = storage.media.prepare(png())
    added = await storage.mutate(
        OWNER,
        "add_records",
        {"records": [{"values": {}, "images": {"add": [1]}}]},
        pid,
        2,
        "record",
        {1: evidence},
    )
    rid = added["changed_ids"][0]
    snapshot = await storage.visual_snapshot(OWNER, pid, record_id=rid)
    legacy = encode(
        {
            "references": snapshot["references"],
            "submission": {"record_id": rid, "images": [evidence.image_id]},
            "report": {"summary": "旧版核对结果"},
        }
    )
    with sqlite3.connect(storage.path) as connection:
        connection.execute(
            "CREATE TABLE image_reviews (id INTEGER PRIMARY KEY, request_key TEXT UNIQUE, plan_id TEXT, document TEXT)"
        )
        connection.execute(
            "INSERT INTO image_reviews(request_key,plan_id,document) VALUES(?,?,?)",
            ("legacy", pid, legacy),
        )
        connection.execute("PRAGMA user_version=7")
        before = connection.execute("SELECT document FROM plans").fetchall()
    await storage.initialize()
    with sqlite3.connect(storage.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert (
            connection.execute("SELECT document FROM image_reviews").fetchone()[0]
            == legacy
        )
        assert connection.execute("SELECT document FROM plans").fetchall() == before
    with pytest.raises(PlanError):
        await storage.query(OWNER, {"plan_id": pid, "review_history": True})
    await storage.mutate(
        OWNER, "reference_delete", {"reference_id": ref_id}, pid, 3, "delete-ref"
    )
    await storage.mutate(
        OWNER,
        "delete_records",
        {"records": [{"record_id": rid}]},
        pid,
        4,
        "delete-record",
    )
    for revision in range(5, 27):
        await storage.mutate(
            OWNER, "update", {"goal": str(revision)}, pid, revision, str(revision)
        )
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    with sqlite3.connect(storage.path) as connection:
        connection.execute("UPDATE media SET orphaned_at=?", (old,))
    await storage.cleanup_media(force=True)
    assert storage.media.path(image.image_id).is_file()
    assert storage.media.path(evidence.image_id).is_file()


class Event:
    def __init__(self, images=None, user="owner", group="", message="1"):
        self.user, self.group = user, group
        self.message_obj = SimpleNamespace(message_id=message, message=images or [])
        self.unified_msg_origin = f"qq-1:{group or user}"
        self.extras, self.sent = {}, []

    def get_platform_id(self):
        return "qq-1"

    def get_sender_id(self):
        return self.user

    def get_group_id(self):
        return self.group

    def is_private_chat(self):
        return not self.group

    def get_extra(self, key):
        return self.extras.get(key)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_messages(self):
        return self.message_obj.message

    def image_result(self, path):
        return ("image", path)

    async def send(self, value):
        self.sent.append(value)


class ConversationContext:
    def __init__(self):
        self.provider = SimpleNamespace(
            provider_config={"modalities": ["text", "image", "tool_use"]}
        )
        self.lookups = 0
        self.callback = None

    async def get_current_chat_provider_id(self, origin):
        self.lookups += 1
        if self.callback:
            callback, self.callback = self.callback, None
            await callback()
        return "current-model"

    def get_provider_by_id(self, provider_id):
        assert provider_id == "current-model"
        return self.provider

    async def llm_generate(self, **params):
        raise AssertionError("图片加载不应发起独立模型请求")


@pytest.fixture
async def plugin(storage):
    from astrbot_plugin_custom_plan.main import CustomPlanPlugin

    plugin = CustomPlanPlugin(ConversationContext(), {"render_enabled": False})
    plugin.storage = storage
    await plugin.initialize()
    yield plugin
    await plugin.terminate()


def image_event(message="image", **kwargs):
    from astrbot.api.message_components import Image as MessageImage

    return Event(
        [MessageImage(file="base64://" + base64.b64encode(png()).decode())],
        message=message,
        **kwargs,
    )


def image_payload(result):
    assert isinstance(result, CallToolResult)
    assert isinstance(result.content[0], TextContent)
    payload = json.loads(result.content[0].text)
    assert payload["ok"]
    assert all(isinstance(item, ImageContent) for item in result.content[1:])
    assert all(item.mimeType == "image/webp" for item in result.content[1:])
    return payload["result"]


async def add_record(storage, pid, actor=OWNER, count=1):
    prepared = {index: storage.media.prepare(png()) for index in range(1, count + 1)}
    revision = (await storage.snapshot(pid, actor))["revision"]
    result = await storage.mutate(
        actor,
        "add_records",
        {"records": [{"values": {}, "images": {"add": list(prepared)}}]},
        pid,
        revision,
        "record-" + str(revision),
        prepared,
    )
    return result["changed_ids"][0], list(prepared.values())


async def test_manage_reference_crud_replay_and_show(plugin, monkeypatch):
    pid = await create(plugin.storage)
    event = image_event()
    params = {"plan_id": pid, "revision": 1, "image_index": 1, "purpose": "截图样例"}
    added = json.loads(await plugin.custom_plan_manage(event, "reference_add", params))
    assert added["ok"]
    ref_id = added["result"]["changed_ids"][0]
    monkeypatch.setattr(
        plugin.storage.media,
        "read_source",
        lambda source: pytest.fail("重试不能重新下载"),
    )
    replay = json.loads(await plugin.custom_plan_manage(event, "reference_add", params))
    assert replay["ok"] and replay["result"]["replayed"]
    shown = json.loads(await plugin.custom_plan_images(event, pid))
    assert shown["ok"] and shown["result"]["count"] == 1
    assert "模型未读图" in shown["result"]["notice"]
    assert plugin.context.lookups == 0
    updated = json.loads(
        await plugin.custom_plan_manage(
            Event(message="update"),
            "reference_update",
            {
                "plan_id": pid,
                "revision": 2,
                "reference_id": ref_id,
                "checks": "日期为当天",
            },
        )
    )
    assert updated["ok"]
    deleted = json.loads(
        await plugin.custom_plan_manage(
            Event(message="delete"),
            "reference_delete",
            {"plan_id": pid, "revision": 3, "reference_id": ref_id},
        )
    )
    assert deleted["ok"]
    assert not (await plugin.storage.snapshot(pid, OWNER))["references"]


async def test_load_returns_actual_images_without_model_call_or_reports(plugin):
    pid = await create(plugin.storage)
    ref_id, reference = await add_reference(plugin.storage, pid, checks="日期为当天")
    before = await plugin.storage.snapshot(pid, OWNER)
    event = image_event()
    result = await plugin.custom_plan_images(event, pid, operation="load")
    payload = image_payload(result)
    assert payload["references"][0]["reference_id"] == ref_id
    assert payload["references"][0]["checks"] == "日期为当天"
    assert payload["image_order"][0]["key"] == "ref:" + ref_id
    assert base64.b64decode(result.content[1].data) == reference.data
    assert payload["record"] is None
    assert not event.sent
    assert await plugin.storage.snapshot(pid, OWNER) == before
    with sqlite3.connect(plugin.storage.path) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='image_reviews'"
            ).fetchone()
            is None
        )
    with pytest.raises(PlanError):
        await plugin.storage.query(OWNER, {"plan_id": pid, "review_history": True})
    for operation in ("reference_read", "compare_images"):
        removed = json.loads(await plugin.execute(event, operation, {"plan_id": pid}))
        assert not removed["ok"]


async def test_load_record_and_references_together_without_saving_current_message(
    plugin,
):
    pid = await create(plugin.storage)
    ref_id, reference = await add_reference(plugin.storage, pid)
    rid, images = await add_record(plugin.storage, pid)
    before = await plugin.storage.snapshot(pid, OWNER)
    event = image_event()
    result = await plugin.custom_plan_images(event, pid, rid, operation="load")
    payload = image_payload(result)
    assert len(result.content) == 3
    assert payload["references"][0]["reference_id"] == ref_id
    assert payload["record"]["record_id"] == rid
    assert [base64.b64decode(item.data) for item in result.content[1:]] == [
        reference.data,
        images[0].data,
    ]
    assert payload["image_order"][1]["key"].startswith("record:" + rid)
    assert not event.sent
    assert await plugin.storage.snapshot(pid, OWNER) == before
    assert set(plugin.storage.media.directory.glob("*.webp")) == {
        plugin.storage.media.path(image.image_id, thumbnail)
        for image in [reference, *images]
        for thumbnail in (False, True)
    }


async def test_record_images_work_without_references_and_allow_single_image_selection(
    plugin,
):
    pid = await create(plugin.storage)
    rid, images = await add_record(plugin.storage, pid, count=2)
    loaded = await plugin.custom_plan_images(
        Event(), pid, rid, images[1].image_id, operation="load", reference_ids=[]
    )
    payload = image_payload(loaded)
    assert payload["references"] == []
    assert len(loaded.content) == 2
    assert base64.b64decode(loaded.content[1].data) == images[1].data
    shown = json.loads(
        await plugin.custom_plan_images(Event(), pid, rid, images[0].image_id)
    )
    assert shown["ok"] and shown["result"]["image_ids"] == [images[0].image_id]


async def test_show_record_defaults_to_attachments_only(plugin):
    pid = await create(plugin.storage)
    await add_reference(plugin.storage, pid)
    rid, images = await add_record(plugin.storage, pid)
    event = Event()
    shown = json.loads(await plugin.custom_plan_images(event, pid, rid))
    assert shown["ok"] and shown["result"]["image_ids"] == [images[0].image_id]
    assert len(event.sent) == 1
    assert plugin.context.lookups == 0


@pytest.mark.parametrize(
    "modalities", [["text", "tool_use"], [], ["image", "tool_use"]]
)
async def test_load_uses_current_model_capabilities(plugin, modalities):
    pid = await create(plugin.storage)
    await add_reference(plugin.storage, pid)
    plugin.context.provider.provider_config["modalities"] = modalities
    result = await plugin.custom_plan_images(Event(), pid, operation="load")
    if modalities and "image" not in modalities:
        result = json.loads(result)
        assert not result["ok"] and "不支持图片" in result["error"]
    else:
        image_payload(result)


async def test_load_with_no_current_provider_fails_explicitly(plugin):
    pid = await create(plugin.storage)
    await add_reference(plugin.storage, pid)
    plugin.context.provider = None
    result = json.loads(await plugin.custom_plan_images(Event(), pid, operation="load"))
    assert not result["ok"] and "会话模型" in result["error"]


async def test_readonly_member_can_load_but_cannot_manage_references(plugin):
    owner = Actor("qq-1", "owner", "group-1")
    pid = await create(plugin.storage, owner, scope="group", mode="protected")
    await add_reference(plugin.storage, pid, owner)
    before = await plugin.storage.snapshot(pid, owner)
    event = image_event(user="member", group="group-1")
    image_payload(await plugin.custom_plan_images(event, pid, operation="load"))
    denied = json.loads(
        await plugin.custom_plan_manage(
            event,
            "reference_add",
            {"plan_id": pid, "revision": 2, "image_index": 1, "purpose": "样例"},
        )
    )
    assert not denied["ok"] and "权限" in denied["error"]
    assert await plugin.storage.snapshot(pid, owner) == before


async def test_supervised_load_always_includes_frozen_basis(plugin):
    pid = await create(plugin.storage)
    ref_id, _ = await add_reference(
        plugin.storage, pid, checks="日期为当天", as_basis=True
    )
    aux_id, _ = await add_reference(plugin.storage, pid, title="辅助示意")
    await enable(plugin.storage, pid)
    result = await plugin.custom_plan_images(
        Event(), pid, operation="load", reference_ids=[aux_id]
    )
    payload = image_payload(result)
    assert {item["reference_id"] for item in payload["references"]} == {ref_id, aux_id}
    assert payload["supervised"] and payload["execution_standard"]
    rid, _ = await add_record(plugin.storage, pid)
    record_only = image_payload(
        await plugin.custom_plan_images(
            Event(), pid, rid, operation="load", reference_ids=[]
        )
    )
    assert [item["reference_id"] for item in record_only["references"]] == [ref_id]


async def test_image_limit_is_checked_before_model_lookup(plugin):
    pid = await create(plugin.storage)
    await add_reference(plugin.storage, pid)
    plugin.storage.media.images_per_record = 8
    rid, images = await add_record(plugin.storage, pid, count=8)
    result = json.loads(
        await plugin.custom_plan_images(Event(), pid, rid, operation="load")
    )
    assert not result["ok"] and "8张" in result["error"]
    assert plugin.context.lookups == 0
    chosen = await plugin.custom_plan_images(
        Event(), pid, rid, images[0].image_id, operation="load"
    )
    assert len(chosen.content) == 3


async def test_tampered_reference_is_rejected_before_returning_images(plugin):
    pid = await create(plugin.storage)
    _, image = await add_reference(plugin.storage, pid)
    plugin.storage.media.path(image.image_id).write_bytes(b"tampered")
    result = json.loads(await plugin.custom_plan_images(Event(), pid, operation="load"))
    assert not result["ok"] and "内容已变化或损坏" in result["error"]


async def test_load_revalidates_revision_after_reading_snapshot(plugin):
    pid = await create(plugin.storage)
    await add_reference(plugin.storage, pid)

    async def change_plan():
        await plugin.storage.mutate(
            OWNER, "update", {"goal": "资料变化"}, pid, 2, "concurrent-update"
        )

    plugin.context.callback = change_plan
    result = json.loads(await plugin.custom_plan_images(Event(), pid, operation="load"))
    assert not result["ok"] and "已变更" in result["error"]


async def test_load_revalidates_permission_before_returning_images(plugin):
    owner = Actor("qq-1", "owner", "group-1")
    pid = await create(plugin.storage, owner, scope="person", mode="shared")
    await add_reference(plugin.storage, pid, owner)

    async def revoke_access():
        await plugin.storage.mutate(
            owner, "update", {"mode": "private"}, pid, 2, "revoke"
        )

    plugin.context.callback = revoke_access
    result = json.loads(
        await plugin.custom_plan_images(
            Event(user="member", group="group-1"), pid, operation="load"
        )
    )
    assert not result["ok"] and "权限" in result["error"]


async def test_load_reuses_bounded_image_queue(plugin):
    pid = await create(plugin.storage)
    await add_reference(plugin.storage, pid)
    plugin.image_pending = 4
    result = json.loads(await plugin.custom_plan_images(Event(), pid, operation="load"))
    assert not result["ok"] and "队列已满" in result["error"]
    assert plugin.image_pending == 4


async def test_concurrent_loads_have_no_model_calls_or_plan_mutations(plugin):
    pid = await create(plugin.storage)
    await add_reference(plugin.storage, pid)
    before = await plugin.storage.snapshot(pid, OWNER)
    results = await asyncio.gather(
        *(
            plugin.custom_plan_images(Event(), pid, operation="load")
            for index in range(2)
        )
    )
    for result in results:
        image_payload(result)
    assert await plugin.storage.snapshot(pid, OWNER) == before


async def test_foreign_record_reference_or_image_ids_cannot_be_loaded(plugin):
    pid = await create(plugin.storage)
    await add_reference(plugin.storage, pid)
    rid, images = await add_record(plugin.storage, pid)
    other = await create(plugin.storage, Actor("qq-1", "other"))
    ref_id, _ = await add_reference(plugin.storage, other, Actor("qq-1", "other"))
    foreign_rid, foreign_images = await add_record(
        plugin.storage, other, Actor("qq-1", "other")
    )
    for params in [
        {"reference_ids": [ref_id]},
        {"record_id": foreign_rid},
        {"record_id": rid, "image_id": foreign_images[0].image_id},
        {"image_id": images[0].image_id},
        {"reference_ids": []},
        {"operation": "compare"},
        {"operation": "read"},
        {"record_id": "https://example.com/image.png"},
    ]:
        result = json.loads(
            await plugin.custom_plan_images(
                Event(), pid, **{"operation": "load", **params}
            )
        )
        assert not result["ok"]


async def test_image_encoder_rejects_oversized_saved_images(storage):
    pid = await create(storage)
    await add_reference(storage, pid)
    snapshot = await storage.visual_snapshot(OWNER, pid)
    with pytest.raises(PlanError, match="配置上限"):
        encode_images(list(snapshot["files"].values()), 1)


async def test_seven_tools_and_refs_command_are_registered(plugin):
    from astrbot.core.provider.register import llm_tools
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    for name in (
        "query",
        "manage",
        "records",
        "configure",
        "render",
        "images",
        "supervision_rules",
    ):
        assert llm_tools.get_func("custom_plan_" + name) is not None
    assert llm_tools.get_func("custom_plan_references") is None
    assert llm_tools.get_func("custom_plan_compare") is None
    tool = llm_tools.get_func("custom_plan_images")
    assert tool.parameters["properties"]["reference_ids"]["items"]["type"] == "string"
    event = Event()
    event.get_message_str = lambda: "plan refs p_demo"
    event.is_at_or_wake_command = True
    matched = []
    for handler in star_handlers_registry.get_handlers_by_module_name(
        plugin.__module__
    ):
        if handler.event_type == EventType.AdapterMessageEvent:
            if all(
                handler_filter.filter(event, {})
                for handler_filter in handler.event_filters
            ):
                matched.append(handler.handler.__name__)
    assert matched == ["references_command"]


async def test_refs_command_still_sends_saved_images(plugin):
    pid = await create(plugin.storage)
    await add_reference(plugin.storage, pid)
    event = Event()
    responses = [response async for response in plugin.references_command(event, pid)]
    assert responses == []
    assert len(event.sent) == 1
    assert plugin.context.lookups == 0


async def test_native_tool_loop_passes_plugin_images_to_current_llm(plugin):
    from unittest.mock import AsyncMock, MagicMock

    from astrbot.core.agent.hooks import BaseAgentRunHooks
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.agent.tool import FunctionTool, ToolSet
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.provider.entities import LLMResponse, ProviderRequest

    pid = await create(plugin.storage)
    _, image = await add_reference(plugin.storage, pid)
    provider = MagicMock()
    provider.provider_config = {"modalities": ["image", "tool_use"]}
    provider.text_chat = AsyncMock(
        side_effect=[
            LLMResponse(
                role="assistant",
                completion_text="",
                tools_call_name=["custom_plan_images"],
                tools_call_args=[{"plan_id": pid, "operation": "load"}],
                tools_call_ids=["call_images"],
            ),
            LLMResponse(role="assistant", completion_text="这是当前模型的读图回答"),
        ]
    )
    plugin.context.provider = provider
    tool = FunctionTool(
        name="custom_plan_images",
        description="加载计划图片",
        parameters={
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "operation": {"type": "string"},
            },
        },
        handler=plugin.custom_plan_images,
    )
    runner = ToolLoopAgentRunner()
    await runner.reset(
        provider=provider,
        request=ProviderRequest(
            prompt="查看参考图", func_tool=ToolSet(tools=[tool]), contexts=[]
        ),
        run_context=ContextWrapper(context=SimpleNamespace(event=Event())),
        tool_executor=FunctionToolExecutor,
        agent_hooks=BaseAgentRunHooks(),
        streaming=False,
    )
    async for response in runner.step_until_done(3):
        pass
    assert provider.text_chat.await_count == 2
    contexts = provider.text_chat.await_args_list[1].kwargs["contexts"]
    image_parts = [
        part
        for message in contexts
        if message["role"] == "user" and isinstance(message["content"], list)
        for part in message["content"]
        if part.get("type") == "image_url"
    ]
    assert len(image_parts) == 1
    assert image_parts[0]["image_url"][
        "url"
    ] == "data:image/webp;base64," + base64.b64encode(image.data).decode("ascii")


@pytest.mark.parametrize("layout", ["mobile", "desktop"])
async def test_reference_previews_render_separately_and_escape_text(
    storage, tmp_path, browser_service, layout
):
    pid = await create(storage, render_layout=layout)
    await add_reference(
        storage, pid, title="<script>坏标题</script>", purpose="核对图中日期"
    )
    plan = await storage.snapshot(pid, OWNER)
    renderer = LocalRenderer(
        tmp_path / "reference-preview",
        {},
        browser_service_resolver=lambda: browser_service,
    )
    renderer.image_loader = storage.image_thumbnail
    await renderer.initialize()
    try:
        html = renderer.html(plan, {}, OWNER.user)
        assert "&lt;script&gt;" in html and "<script>坏标题" not in html
        async with browser_service.session(javascript_enabled=False) as page:
            await page.set_content(html, wait_until="load")
            assert await page.locator(".reference-thumb").count() == 1
            assert await page.locator(".reference-thumb").evaluate(
                "image => image.complete && image.naturalWidth > 0"
            )
            assert await page.locator(".attachment-section").count() == 0
        output = await renderer.render(plan, {}, OWNER.user)
        with Image.open(output) as screenshot:
            assert screenshot.width == (640 if layout == "mobile" else 1000)
    finally:
        await renderer.close()
