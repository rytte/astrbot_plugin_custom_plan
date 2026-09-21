import json
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


async def test_real_astrbot_registration_and_shared_tools(plugin):
    from astrbot.core.provider.register import llm_tools
    from astrbot.core.star.updater import _PluginUpdater

    metadata = _PluginUpdater.inspect_plugin_directory(Path(__file__).parents[1])
    assert metadata["metadata"]["name"] == "astrbot_plugin_custom_plan"

    for name in ("query", "manage", "records", "configure", "render"):
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
