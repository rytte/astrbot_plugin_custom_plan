import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_custom_plan.domain import Actor, PlanError

pytest.importorskip("astrbot")
CustomPlanPlugin = pytest.importorskip(
    "astrbot_plugin_custom_plan.main"
).CustomPlanPlugin


class Event:
    def __init__(self, user="owner", group="", message="1"):
        self.user = user
        self.group = group
        self.message_obj = SimpleNamespace(message_id=message)
        self.extras = {}
        self.sent = []

    def get_platform_id(self):
        return "qq-1"

    def get_sender_id(self):
        return self.user

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


@pytest.mark.skipif(
    not os.environ.get("CUSTOM_PLAN_BROWSER"), reason="Requires local Chromium"
)
async def test_midnight_switch_restart_render_and_undo_through_tools(plugin):
    from astrbot_plugin_custom_plan.renderer import LocalRenderer
    from astrbot_plugin_custom_plan.storage import Storage
    from PIL import Image

    created = json.loads(
        await plugin.custom_plan_manage(
            Event(message="create"), "create", {"name": "主题切换", "preset": "todo"}
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
        {"browser_executable": os.environ["CUSTOM_PLAN_BROWSER"], "render_timeout": 90},
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
