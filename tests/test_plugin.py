import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_custom_plan.domain import Actor, PlanError, create_plan
from astrbot_plugin_custom_plan.presentation import build_view

pytest.importorskip("astrbot")
CustomPlanPlugin = pytest.importorskip(
    "astrbot_plugin_custom_plan.main"
).CustomPlanPlugin


class Event:
    def __init__(self, user="owner", group="", message="1", nickname=""):
        self.user = user
        self.group = group
        self.nickname = nickname
        self.message_obj = SimpleNamespace(message_id=message)
        self.extras = {}
        self.sent = []

    def get_platform_id(self):
        return "qq-1"

    def get_sender_id(self):
        return self.user

    def get_sender_name(self):
        return self.nickname

    async def get_group(self, group_id=None):
        return None

    def get_group_id(self):
        return self.group

    def is_private_chat(self):
        return not self.group

    def get_extra(self, name):
        return self.extras.get(name)

    def set_extra(self, name, value):
        self.extras[name] = value

    def get_message_str(self):
        return self.message_str

    def get_messages(self):
        return getattr(self.message_obj, "message", [])

    def plain_result(self, value):
        return ("text", value)

    def image_result(self, value):
        return ("image", value)

    async def send(self, message):
        if message[0] == "image":
            assert Path(message[1]).exists()
        self.sent.append(message)


@pytest.fixture
async def plugin(storage, tmp_path):
    plugin = CustomPlanPlugin(SimpleNamespace(), {"render_enabled": False})
    plugin.storage = storage
    await plugin.initialize()
    yield plugin
    await plugin.terminate()


async def test_query_schema_exposes_params_and_reference_instructions(plugin):
    from astrbot.core.provider.register import llm_tools

    tool = llm_tools.get_func("custom_plan_query")
    assert tool.parameters["properties"]["params"]["type"] == "object"
    assert "references" in tool.description
    assert "custom_plan_images" in tool.description


@pytest.mark.parametrize("tool_args", [{}, {"params": {}}, {"params": None}])
async def test_query_without_params_works_through_permission_guarded_executor(
    plugin, monkeypatch, tool_args
):
    from unittest.mock import AsyncMock

    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.tool import FunctionTool
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.provider.func_tool_manager import (
        FunctionToolManager,
        _PermissionGuardedTool,
    )
    from astrbot.core.provider.register import llm_tools

    event = Event()
    created = json.loads(
        await plugin.custom_plan_manage(event, "create", {"name": "查询测试"})
    )
    plan_id = created["result"]["plan_id"]
    registered = llm_tools.get_func("custom_plan_query")
    tool = FunctionTool(
        name=registered.name,
        description=registered.description,
        parameters=registered.parameters,
        handler=plugin.custom_plan_query,
    )
    manager = FunctionToolManager()
    permission_check = AsyncMock(return_value=None)
    monkeypatch.setattr(manager, "_check_tool_permission", permission_check)
    guarded = _PermissionGuardedTool(tool, manager)
    run_context = ContextWrapper(context=SimpleNamespace(event=event))
    responses = [
        response
        async for response in FunctionToolExecutor.execute(
            guarded, run_context, **tool_args
        )
    ]
    assert len(responses) == 1
    result = json.loads(responses[0].content[0].text)
    assert result["ok"]
    assert [plan["plan_id"] for plan in result["result"]["plans"]] == [plan_id]
    permission_check.assert_awaited_once_with("custom_plan_query", run_context)


@pytest.mark.parametrize("params", [[], "", False, 0])
async def test_query_rejects_invalid_falsy_params(plugin, params):
    result = json.loads(await plugin.custom_plan_query(Event(), params))
    assert not result["ok"] and "params 必须是对象" in result["error"]


async def test_query_without_params_only_lists_authorized_plans(plugin):
    created = json.loads(
        await plugin.custom_plan_manage(Event(), "create", {"name": "私有计划"})
    )
    assert created["ok"]
    result = json.loads(await plugin.custom_plan_query(Event("other")))
    assert result["ok"] and result["result"]["total"] == 0


async def test_browser_service_resolution_requires_an_active_plugin(plugin):
    service = object()
    plugin.context.get_registered_star = lambda name: SimpleNamespace(
        activated=True, star_cls=SimpleNamespace(service=service)
    )
    assert plugin.get_browser_service() is service

    plugin.context.get_registered_star = lambda name: None
    with pytest.raises(PlanError, match="请启用 astrbot_plugin_browser"):
        plugin.get_browser_service()


@pytest.mark.parametrize(
    ("message", "handler_name", "params"),
    [
        ("plan", "help_command", {}),
        ("plan list", "list_command", {"page": 1}),
        ("plan list 2", "list_command", {"page": 2}),
        (
            "plan create 阅读",
            "create_command",
            {"name": "阅读", "preset": "generic"},
        ),
        (
            "plan create 阅读打卡 checkin",
            "create_command",
            {"name": "阅读打卡", "preset": "checkin"},
        ),
        ("plan show p_demo", "render_command", {"plan_id": "p_demo", "page": 1}),
        (
            "plan images p_demo r_demo",
            "images_command",
            {"plan_id": "p_demo", "record_id": "r_demo"},
        ),
        (
            "plan show p_demo 2",
            "render_command",
            {"plan_id": "p_demo", "page": 2},
        ),
        (
            'plan exec create {"name": "旅行准备", "preset": "todo"}',
            "operation_command",
            {
                "operation": "create",
                "payload": '{"name": "旅行准备", "preset": "todo"}',
            },
        ),
        ("计划", None, None),
        ("计划列表", None, None),
        ("计划创建 阅读 checkin", None, None),
        ("计划看板 p_demo", None, None),
        ("计划操作 query {}", None, None),
    ],
)
def test_english_commands_match_once_and_parse_arguments(message, handler_name, params):
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    event = Event()
    # AstrBot removes the configured wake prefix before matching commands.
    event.message_str = message
    event.is_at_or_wake_command = True
    matched = []
    for handler in star_handlers_registry.get_handlers_by_module_name(
        CustomPlanPlugin.__module__
    ):
        if handler.event_type != EventType.AdapterMessageEvent:
            continue
        event.extras.pop("parsed_params", None)
        if all(f.filter(event, {}) for f in handler.event_filters):
            matched.append((handler.handler_name, event.get_extra("parsed_params")))

    assert matched == ([(handler_name, params)] if handler_name else [])


async def test_real_astrbot_registration_and_shared_tools(plugin):
    from astrbot.core.provider.register import llm_tools
    from astrbot.core.star.updater import _PluginUpdater

    metadata = _PluginUpdater.inspect_plugin_directory(Path(__file__).parents[1])
    assert metadata["metadata"]["name"] == "astrbot_plugin_custom_plan"

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
    event = Event()
    created = json.loads(
        await plugin.custom_plan_manage(
            event, "create", {"name": "任务", "preset": "todo"}
        )
    )
    assert created["ok"]
    pid = created["result"]["plan_id"]
    result = json.loads(await plugin.custom_plan_query(event, {"plan_id": pid}))
    assert result["result"]["view"]["type"] == "table"
    changed = json.loads(
        await plugin.custom_plan_records(
            event,
            "add",
            {"plan_id": pid, "revision": 1, "records": [{"values": {"title": "学习"}}]},
        )
    )
    assert changed["ok"]
    configured = json.loads(
        await plugin.custom_plan_configure(
            event, "view", {"plan_id": pid, "revision": 2, "type": "calendar"}
        )
    )
    assert configured["ok"]
    forbidden = json.loads(
        await plugin.custom_plan_query(Event("other"), {"plan_id": pid})
    )
    assert not forbidden["ok"] and "学习" not in json.dumps(
        forbidden, ensure_ascii=False
    )


async def test_text_fallback_and_image_cleanup(plugin, tmp_path, monkeypatch):
    event = Event()
    created = json.loads(
        await plugin.custom_plan_manage(event, "create", {"name": "计划"})
    )["result"]
    result = json.loads(await plugin.custom_plan_render(event, created["plan_id"], {}))
    assert result["result"]["sent"] == "text"
    image = tmp_path / "image.png"

    async def render(*args):
        image.write_bytes(b"test")
        return image

    monkeypatch.setattr(plugin.renderer, "render", render)
    result = json.loads(await plugin.custom_plan_render(event, created["plan_id"], {}))
    assert result["result"]["sent"] == "image"
    assert not image.exists()


async def test_render_without_options_uses_defaults(plugin, tmp_path, monkeypatch):
    event = Event()
    created = json.loads(
        await plugin.custom_plan_manage(event, "create", {"name": "计划"})
    )["result"]
    image = tmp_path / "default-options.png"
    seen_options = []

    async def render(plan, options, actor_user, user_names):
        seen_options.append(options)
        image.write_bytes(b"test")
        return image

    monkeypatch.setattr(plugin.renderer, "render", render)
    result = json.loads(await plugin.custom_plan_render(event, created["plan_id"]))
    assert result["ok"] and result["result"]["sent"] == "image"
    assert seen_options == [{}]
    assert not image.exists()


async def test_midnight_switch_restart_render_and_undo_through_tools(
    plugin, browser_service
):
    from astrbot_plugin_custom_plan.renderer import LocalRenderer
    from astrbot_plugin_custom_plan.storage import Storage
    from PIL import Image

    created = json.loads(
        await plugin.custom_plan_manage(
            Event(message="create"),
            "create",
            {"name": "主题切换", "preset": "todo", "render_theme": "forest"},
        )
    )
    assert created["ok"]
    pid = created["result"]["plan_id"]
    changed = json.loads(
        await plugin.custom_plan_manage(
            Event(message="theme"),
            "update",
            {"plan_id": pid, "revision": 1, "render_theme": "midnight"},
        )
    )
    assert changed["ok"] and changed["result"]["render_theme"] == "midnight"
    # Reopen storage and renderer with a different creation default to simulate restart.
    plugin.storage = Storage(plugin.storage.path, default_render_theme="forest")
    await plugin.storage.initialize()
    await plugin.renderer.close()
    plugin.renderer = LocalRenderer(
        plugin.renderer.directory,
        {"render_timeout": 90},
        browser_service_resolver=lambda: browser_service,
    )
    await plugin.renderer.initialize()
    for theme, color in (("midnight", (11, 18, 32)), ("forest", (237, 243, 239))):
        if theme == "forest":
            undone = json.loads(
                await plugin.custom_plan_manage(
                    Event(message="undo-theme"), "undo", {"plan_id": pid, "revision": 2}
                )
            )
            assert undone["ok"]
        state = json.loads(await plugin.custom_plan_query(Event(), {"plan_id": pid}))
        assert state["result"]["render_theme"] == theme
        assert state["result"]["render_layout"] == "mobile"
        event = Event()
        sent_paths = []

        async def capture(message):
            assert message[0] == "image"
            path = Path(message[1])
            with Image.open(path) as image:
                assert image.width == 640
                assert image.convert("RGB").getpixel((0, 0)) == color
            sent_paths.append(path)

        event.send = capture
        result = json.loads(await plugin.custom_plan_render(event, pid, {}))
        assert result["ok"] and result["result"]["sent"] == "image"
        assert len(sent_paths) == 1 and not sent_paths[0].exists()
        assert browser_service.ready
        assert browser_service._browser.contexts == []


@pytest.mark.parametrize("fail_render", [False, True])
async def test_permission_rechecked_after_render_even_on_fallback(
    plugin, tmp_path, monkeypatch, fail_render
):
    created = json.loads(
        await plugin.custom_plan_manage(
            Event(), "create", {"name": "私人数据", "mode": "shared"}
        )
    )["result"]
    pid = created["plan_id"]
    image = tmp_path / "private.png"

    async def render(*args):
        await plugin.storage.mutate(
            Actor("qq-1", "owner"), "update", {"mode": "private"}, pid, 1, "restrict"
        )
        if fail_render:
            raise PlanError("模拟失败")
        image.write_bytes(b"test")
        return image

    monkeypatch.setattr(plugin.renderer, "render", render)
    group = Event(group="g1")
    result = json.loads(await plugin.custom_plan_render(group, pid, {}))
    assert not result["ok"]
    assert group.sent == []
    assert not image.exists()


async def test_unknown_arguments_cannot_spoof_identity(plugin):
    result = json.loads(
        await plugin.custom_plan_manage(
            Event(), "create", {"name": "恶意参数", "owner": "other"}
        )
    )
    assert not result["ok"]
    assert (
        json.loads(await plugin.custom_plan_query(Event(), {}))["result"]["total"] == 0
    )


async def test_supervision_tools_and_commands_share_enforcement(plugin):
    created = json.loads(
        await plugin.custom_plan_manage(
            Event(message="create"), "create", {"name": "阅读", "preset": "checkin"}
        )
    )["result"]
    pid = created["plan_id"]
    preview = json.loads(
        await plugin.custom_plan_manage(
            Event(message="preview"),
            "supervision_preview",
            {
                "plan_id": pid,
                "revision": 1,
                "end_date": None,
                "execution_standard": "每天完成计划要求的任务，并按实际进度如实记录。",
            },
        )
    )["result"]
    params = {
        "plan_id": pid,
        "revision": 1,
        "confirmation_token": preview["confirmation_token"],
    }
    same_message = json.loads(
        await plugin.custom_plan_manage(
            Event(message="preview"), "supervision_enable", params
        )
    )
    assert not same_message["ok"]
    enabled = json.loads(
        await plugin.custom_plan_manage(
            Event(message="confirm"), "supervision_enable", params
        )
    )
    assert enabled["ok"] and enabled["result"]["supervision"]["active"]
    queried = json.loads(await plugin.custom_plan_query(Event(), {"plan_id": pid}))
    assert "rules_text" not in json.dumps(queried)
    rules = json.loads(await plugin.custom_plan_supervision_rules(Event(), pid))
    assert rules["result"]["rules_text"] == preview["rules_text"]
    added = json.loads(
        await plugin.custom_plan_records(
            Event(message="add"),
            "add",
            {"plan_id": pid, "revision": 2, "records": [{"values": {"amount": 20}}]},
        )
    )["result"]
    corrected = json.loads(
        await plugin.custom_plan_records(
            Event(message="correct"),
            "correct",
            {
                "plan_id": pid,
                "revision": 3,
                "reason": "多写一个零",
                "records": [
                    {"record_id": added["changed_ids"][0], "values": {"amount": 2}}
                ],
            },
        )
    )
    assert corrected["ok"]
    history = json.loads(
        await plugin.custom_plan_query(
            Event(), {"plan_id": pid, "supervision_history": True}
        )
    )
    assert history["result"]["entries"][0]["document"]["reason"] == "多写一个零"
    result = [
        message
        async for message in plugin.operation_command(
            Event(message="command-delete"),
            "delete",
            json.dumps({"plan_id": pid, "revision": 4}),
        )
    ]
    assert "监督期间禁止删除" in result[0][1]
    configuration = json.loads(
        await plugin.custom_plan_configure(
            Event(message="change-rules"),
            "rules",
            {
                "plan_id": pid,
                "revision": 4,
                "threshold": 0.5,
                "effective_from": queried["result"]["today"],
            },
        )
    )
    assert not configuration["ok"]
    assert (await plugin.storage.snapshot(pid, Actor("qq-1", "owner")))["revision"] == 4
    switched = json.loads(
        await plugin.custom_plan_configure(
            Event(message="switch-view"),
            "view",
            {"plan_id": pid, "revision": 4, "type": "todo"},
        )
    )
    assert switched["ok"] and switched["result"]["supervision"]["active"]
    snapshot = await plugin.storage.snapshot(pid, Actor("qq-1", "owner"))
    assert snapshot["view"]["type"] == "todo" and snapshot["rules"]["kind"] == "checkin"
    assert len(snapshot["fields"]) == 3 and snapshot["revision"] == 5


@pytest.mark.parametrize("view_type", ["checkin", "todo"])
@pytest.mark.parametrize(
    "mode,viewer,expected,lookup_count",
    [
        ("public", "member", "成员昵称", 0),
        ("protected", "member", "创建者昵称", 1),
        ("protected", "owner", "创建者昵称", 0),
    ],
)
async def test_group_render_uses_platform_nickname_for_displayed_user(
    plugin, tmp_path, monkeypatch, view_type, mode, viewer, expected, lookup_count
):
    owner = Event(group="g1", nickname="创建者昵称")
    pid = json.loads(
        await plugin.custom_plan_manage(
            owner, "create", {"name": "学习", "preset": "checkin", "mode": mode}
        )
    )["result"]["plan_id"]
    await plugin.custom_plan_configure(
        owner, "view", {"plan_id": pid, "revision": 1, "type": view_type}
    )
    event = Event(
        user=viewer,
        group="g1",
        nickname="创建者昵称" if viewer == "owner" else "成员昵称",
    )
    lookups = []

    async def get_group(group_id):
        lookups.append(group_id)
        return SimpleNamespace(
            group_id="g1",
            members=[
                SimpleNamespace(user_id="owner", nickname="创建者昵称"),
                SimpleNamespace(user_id="member", nickname="成员昵称"),
            ],
        )

    captured = []

    async def render(plan, options, actor_user, user_names):
        captured.append(user_names)
        assert (
            build_view(plan, options, actor_user, user_names)["view"]["scope_label"]
            == expected
        )
        path = tmp_path / "nickname.png"
        path.write_bytes(b"test")
        return path

    monkeypatch.setattr(event, "get_group", get_group)
    monkeypatch.setattr(plugin.renderer, "render", render)
    before = await plugin.storage.snapshot(pid, plugin.actor(event))
    result = json.loads(await plugin.custom_plan_render(event, pid, {}))
    assert result["ok"] and result["result"]["sent"] == "image"
    assert len(lookups) == lookup_count
    assert captured == [{viewer if mode == "public" else "owner": expected}]
    assert await plugin.storage.snapshot(pid, plugin.actor(event)) == before


@pytest.mark.parametrize(
    "failure", ["unsupported", "missing", "blank", "wrong_group", "timeout"]
)
async def test_unavailable_group_nickname_does_not_prevent_image(
    plugin, tmp_path, monkeypatch, failure
):
    owner = Event(group="g1")
    pid = json.loads(
        await plugin.custom_plan_manage(
            owner, "create", {"name": "学习", "preset": "checkin"}
        )
    )["result"]["plan_id"]
    await plugin.custom_plan_configure(
        owner, "view", {"plan_id": pid, "revision": 1, "type": "checkin"}
    )
    event = Event(user="member", group="g1", nickname="不能误用查询者昵称")

    async def get_group(group_id):
        if failure == "timeout":
            raise TimeoutError
        if failure == "unsupported":
            return None
        return SimpleNamespace(
            group_id="other" if failure == "wrong_group" else "g1",
            members=[]
            if failure == "missing"
            else [
                SimpleNamespace(
                    user_id="owner",
                    nickname="  " if failure == "blank" else "其他群昵称",
                )
            ],
        )

    captured = []

    async def render(plan, options, actor_user, user_names):
        captured.append(user_names)
        path = tmp_path / "no-name.png"
        path.write_bytes(b"test")
        return path

    monkeypatch.setattr(event, "get_group", get_group)
    monkeypatch.setattr(plugin.renderer, "render", render)
    result = json.loads(await plugin.custom_plan_render(event, pid, {}))
    assert result["ok"] and result["result"]["sent"] == "image"
    assert captured == [{}]


async def test_personal_plan_never_looks_up_nickname_even_in_group(plugin, monkeypatch):
    event = Event(group="g1", nickname="无需展示")
    actor = plugin.actor(event)
    plan = create_plan(
        actor,
        {"name": "个人", "preset": "checkin", "scope": "person", "mode": "shared"},
        "Asia/Shanghai",
    )
    plan["view"]["type"] = "checkin"
    calls = []

    async def get_group(group_id):
        calls.append(group_id)

    monkeypatch.setattr(event, "get_group", get_group)
    assert await plugin.render_user_names(event, plan, actor) == {}
    assert calls == []


@pytest.fixture
def image_event():
    from io import BytesIO

    from astrbot.api.message_components import Image
    from PIL import Image as RasterImage

    stream = BytesIO()
    RasterImage.new("RGB", (60, 40), "blue").save(stream, format="PNG")
    event = Event(message="image-message")
    event.message_obj.message = [Image.fromBytes(stream.getvalue())]
    return event


async def test_message_images_are_saved_only_explicitly_and_sent_privately(
    plugin, image_event
):
    created = json.loads(
        await plugin.custom_plan_manage(
            image_event, "create", {"name": "图片打卡", "preset": "checkin"}
        )
    )
    pid = created["result"]["plan_id"]
    assert not list(plugin.storage.media.directory.iterdir())
    query = json.loads(await plugin.custom_plan_query(image_event, {"plan_id": pid}))
    assert query["result"]["message_images"] == [{"index": 1}]
    added = json.loads(
        await plugin.custom_plan_records(
            image_event,
            "add",
            {
                "plan_id": pid,
                "revision": 1,
                "records": [{"values": {}, "images": {"add": [1]}}],
            },
        )
    )
    assert added["ok"]
    rid = added["result"]["changed_ids"][0]
    stored = (await plugin.storage.query(plugin.actor(image_event), {"plan_id": pid}))[
        "records"
    ][0]
    assert len(stored["images"]) == 1
    denied_event = Event(user="other")
    denied = json.loads(await plugin.custom_plan_images(denied_event, pid, rid))
    assert not denied["ok"] and not denied_event.sent
    viewed = json.loads(await plugin.custom_plan_images(image_event, pid, rid))
    assert viewed["ok"] and viewed["result"]["count"] == 1
    assert Path(image_event.sent[0][1]).is_file()
    assert "path" not in viewed["result"]
    wrong_id = json.loads(
        await plugin.custom_plan_images(image_event, pid, rid, "img_" + "0" * 32)
    )
    assert not wrong_id["ok"] and len(image_event.sent) == 1


async def test_upload_retry_does_not_require_an_expired_source(
    plugin, image_event, monkeypatch
):
    pid = json.loads(
        await plugin.custom_plan_manage(
            image_event, "create", {"name": "图片", "preset": "checkin"}
        )
    )["result"]["plan_id"]
    params = {
        "plan_id": pid,
        "revision": 1,
        "records": [{"values": {}, "images": {"add": [1]}}],
    }
    first = json.loads(await plugin.custom_plan_records(image_event, "add", params))
    assert first["ok"]

    async def expired(source):
        raise AssertionError("An idempotent retry must not download again")

    monkeypatch.setattr(plugin.storage.media, "read_source", expired)
    image_event.message_obj.message = []
    replay = json.loads(await plugin.custom_plan_records(image_event, "add", params))
    assert replay["ok"] and replay["result"]["replayed"]
    assert replay["result"]["changed_ids"] == first["result"]["changed_ids"]
    assert len(list(plugin.storage.media.directory.glob("*.webp"))) == 2


async def test_untrusted_sources_indices_and_read_only_uploads_are_rejected_before_read(
    plugin, image_event, monkeypatch
):
    owner = Event(group="g1", message="create")
    pid = json.loads(
        await plugin.custom_plan_manage(
            owner, "create", {"name": "图片", "preset": "checkin"}
        )
    )["result"]["plan_id"]
    reads = []

    async def track(source):
        reads.append(source)
        raise AssertionError("No source should be read")

    monkeypatch.setattr(plugin.storage.media, "read_source", track)
    image_event.group = "g1"
    for additions in (
        ["http://127.0.0.1/image"],
        ["C:\\secret.png"],
        [True],
        [0],
        [2],
        [1, 1],
    ):
        result = json.loads(
            await plugin.custom_plan_records(
                image_event,
                "add",
                {
                    "plan_id": pid,
                    "revision": 1,
                    "records": [{"values": {}, "images": {"add": additions}}],
                },
            )
        )
        assert not result["ok"]
    image_event.user = "member"
    denied = json.loads(
        await plugin.custom_plan_records(
            image_event,
            "add",
            {
                "plan_id": pid,
                "revision": 1,
                "records": [{"values": {}, "images": {"add": [1]}}],
            },
        )
    )
    assert not denied["ok"] and "权限" in denied["error"]
    assert not reads and not list(plugin.storage.media.directory.iterdir())


async def test_image_delivery_rechecks_record_between_sends(
    plugin, image_event, monkeypatch
):
    pid = json.loads(
        await plugin.custom_plan_manage(
            image_event, "create", {"name": "图片", "preset": "checkin"}
        )
    )["result"]["plan_id"]
    image_event.message_obj.message *= 2
    added = json.loads(
        await plugin.custom_plan_records(
            image_event,
            "add",
            {
                "plan_id": pid,
                "revision": 1,
                "records": [{"values": {}, "images": {"add": [1, 2]}}],
            },
        )
    )
    rid = added["result"]["changed_ids"][0]
    send = image_event.send

    async def remove_after_send(message):
        await send(message)
        await plugin.storage.mutate(
            plugin.actor(image_event),
            "delete_records",
            {"records": [{"record_id": rid}]},
            pid,
            2,
            "concurrent-delete",
        )

    monkeypatch.setattr(image_event, "send", remove_after_send)
    result = json.loads(await plugin.custom_plan_images(image_event, pid, rid))
    assert not result["ok"] and result["sent_count"] == 1
    assert len(image_event.sent) == 1
