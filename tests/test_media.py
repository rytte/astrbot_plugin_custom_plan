import asyncio
import base64
import json
import os
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
from uuid import uuid4

import pytest
from astrbot_plugin_custom_plan.domain import Actor, PlanError
from astrbot_plugin_custom_plan.media import (
    MediaLibrary,
    PublicResolver,
    public_address,
    validate_remote_url,
)
from astrbot_plugin_custom_plan.presentation import build_view
from astrbot_plugin_custom_plan.renderer import LocalRenderer
from astrbot_plugin_custom_plan.storage import Storage
from PIL import Image

OWNER = Actor("qq-1", "owner")


def image_bytes(size=(80, 60), color="red", image_format="PNG", **kwargs):
    stream = BytesIO()
    Image.new("RGB", size, color).save(stream, format=image_format, **kwargs)
    return stream.getvalue()


async def create_plan(storage, **params):
    result = await storage.mutate(
        OWNER,
        "create",
        {"name": "附图", "preset": "checkin", **params},
        message_key=uuid4().hex,
    )
    return result["plan_id"]


async def change(storage, pid, action, params, prepared=None, key=None):
    plan = await storage.snapshot(pid, OWNER)
    return await storage.mutate(
        OWNER, action, params, pid, plan["revision"], key or uuid4().hex, prepared
    )


async def add_image(storage, pid):
    image = storage.media.prepare(image_bytes())
    result = await change(
        storage,
        pid,
        "add_records",
        {"records": [{"values": {}, "images": {"add": [1]}}]},
        {1: image},
    )
    return result["changed_ids"][0], image


def test_preparation_removes_metadata_orients_and_generates_thumbnail(tmp_path):
    media = MediaLibrary(tmp_path)
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "private metadata"
    prepared = media.prepare(image_bytes((400, 200), image_format="JPEG", exif=exif))
    assert (prepared.width, prepared.height) == (200, 400)
    with Image.open(BytesIO(prepared.data)) as full:
        assert full.format == "WEBP" and not full.getexif()
        assert not full.info.get("icc_profile")
    with Image.open(BytesIO(prepared.thumbnail)) as small:
        assert small.width <= 320 and small.height <= 240
    media.write(prepared)
    assert media.path(prepared.image_id).read_bytes() == prepared.data
    assert media.thumbnail_url(prepared.image_id).startswith("data:image/webp;base64,")
    assert not list(tmp_path.glob("*.tmp-*"))


@pytest.mark.parametrize(
    "data,error",
    [(b"<svg></svg>", "格式"), (b"", "为空"), (image_bytes(image_format="GIF"), "GIF")],
)
def test_invalid_images_are_rejected(tmp_path, data, error):
    with pytest.raises(PlanError, match=error):
        MediaLibrary(tmp_path).prepare(data)


def test_pixel_and_size_limits_before_decoding(tmp_path):
    media = MediaLibrary(tmp_path, {"image_max_pixels": 1_000_000, "image_max_mb": 1})
    with pytest.raises(PlanError, match="像素"):
        media.prepare(image_bytes((1001, 1000)))
    with pytest.raises(PlanError, match="大小"):
        media.prepare(b"x" * (1024 * 1024 + 1))
    with pytest.raises(PlanError, match="ID"):
        media.path("../../secret")


def test_animated_webp_is_rejected(tmp_path):
    stream = BytesIO()
    Image.new("RGB", (10, 10), "red").save(
        stream,
        format="WEBP",
        save_all=True,
        append_images=[Image.new("RGB", (10, 10), "blue")],
        duration=100,
    )
    with pytest.raises(PlanError, match="动态"):
        MediaLibrary(tmp_path).prepare(stream.getvalue())


@pytest.mark.parametrize(
    "config",
    [
        {"images_per_record": True},
        {"image_max_mb": 0},
        {"image_cleanup_days": -1},
        {"image_max_pixels": "20000000"},
    ],
)
def test_configuration_is_bounded(tmp_path, config):
    with pytest.raises(PlanError):
        MediaLibrary(tmp_path, config)


async def test_cancelled_conversion_waits_for_worker_before_releasing_slot(
    tmp_path, monkeypatch
):
    media = MediaLibrary(tmp_path)
    started, finished = threading.Event(), threading.Event()
    convert = media.prepare

    def slow(data):
        started.set()
        finished.wait(3)
        return convert(data)

    monkeypatch.setattr(media, "prepare", slow)
    task = asyncio.create_task(media.prepare_async(image_bytes()))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        finished.set()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_local_base64_and_data_sources_are_bounded(tmp_path):
    media = MediaLibrary(tmp_path / "media", {"image_max_mb": 1})
    content = image_bytes()
    path = tmp_path / "message.png"
    path.write_bytes(content)
    encoded = base64.b64encode(content).decode("ascii")
    for source in (
        str(path),
        path.as_uri(),
        "base64://" + encoded,
        "data:image/png;base64," + encoded,
    ):
        assert await media.read_source(source) == content
    for source in (
        "file://server/share/photo.png",
        "relative.png",
        "base64://!",
        "base64://" + "A" * 1_500_000,
    ):
        with pytest.raises(PlanError):
            await media.read_source(source)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a",
        "http://10.0.0.1/a",
        "https://[::1]/a",
        "http://169.254.169.254/a",
        "http://[::ffff:127.0.0.1]/a",
        "http://224.0.0.1/a",
        "https://example.com:8080/a",
        "https://user:pass@example.com/a",
        "file:///tmp/a",
        "https://example.com:bad/a",
    ],
)
def test_remote_addresses_reject_private_or_ambiguous_targets(url):
    with pytest.raises(PlanError):
        validate_remote_url(url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.0.1",
        "::1",
        "::ffff:10.0.0.1",
        "ff02::1",
        "224.0.0.1",
    ],
)
def test_public_address_checks(address):
    assert not public_address(address)
    assert public_address("8.8.8.8")


async def test_resolver_rejects_mixed_public_and_private_dns(monkeypatch):
    resolver = PublicResolver()

    async def resolve(*args):
        return [{"host": "8.8.8.8"}, {"host": "127.0.0.1"}]

    monkeypatch.setattr(resolver.resolver, "resolve", resolve)
    try:
        with pytest.raises(PlanError, match="内网"):
            await resolver.resolve("example.com", 443)
    finally:
        await resolver.close()


@pytest.mark.parametrize(
    "status,length,chunks,headers,success",
    [
        (200, None, [b"image"], {}, True),
        (302, None, [], {}, False),
        (200, 2_000_000, [], {}, False),
        (200, None, [b"x" * 700_000, b"x" * 700_000], {}, False),
        (200, None, [], {"Content-Encoding": "gzip"}, False),
    ],
)
async def test_download_stream_limits_and_redirect_policy(
    tmp_path, monkeypatch, status, length, chunks, headers, success
):
    import astrbot_plugin_custom_plan.media as module

    class Response:
        def __init__(self):
            self.status = status
            self.content_length = length
            self.headers = headers
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def iter_chunked(self, size):
            for chunk in chunks:
                yield chunk

    class Session:
        def __init__(self, **kwargs):
            assert not kwargs["trust_env"] and not kwargs["auto_decompress"]
            self.connector = kwargs["connector"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def get(self, source, **kwargs):
            assert not kwargs["allow_redirects"]
            return Response()

    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    media = MediaLibrary(tmp_path, {"image_max_mb": 1})
    if success:
        assert await media.read_source("https://example.com/a") == b"image"
    else:
        with pytest.raises(PlanError):
            await media.read_source("https://example.com/a")


async def test_images_are_persisted_with_private_metadata_and_idempotency(storage):
    pid = await create_plan(storage)
    prepared = storage.media.prepare(image_bytes())
    params = {"records": [{"values": {}, "images": {"add": [1]}}]}
    first = await storage.mutate(
        OWNER, "add_records", params, pid, 1, "message", {1: prepared}
    )
    duplicate = await storage.mutate(
        OWNER,
        "add_records",
        params,
        pid,
        1,
        "message",
        {1: storage.media.prepare(image_bytes(color="blue"))},
    )
    assert duplicate["replayed"] and first["changed_ids"] == duplicate["changed_ids"]
    queried = await storage.query(OWNER, {"plan_id": pid})
    assert queried["records"][0]["images"] == [prepared.image_id]
    assert queried["image_metadata"][prepared.image_id]["author"] == OWNER.user
    assert queried["image_storage_bytes"] == prepared.size
    assert str(storage.media.directory) not in json.dumps(queried)
    assert "base64" not in json.dumps(queried)
    reopened = Storage(storage.path)
    await reopened.initialize()
    images = await reopened.record_images(OWNER, pid, first["changed_ids"][0])
    assert images[0]["path"].read_bytes() == prepared.data
    assert len(list(storage.media.directory.glob("*.webp"))) == 2


async def test_one_upload_can_be_shared_by_records_without_duplicate_files(storage):
    pid = await create_plan(storage, preset="generic")
    image = storage.media.prepare(image_bytes())
    await change(
        storage,
        pid,
        "add_records",
        {
            "records": [
                {"values": {}, "images": {"add": [1]}},
                {"values": {}, "images": {"add": [1]}},
            ]
        },
        {1: image},
    )
    assert len(list(storage.media.directory.glob("*.webp"))) == 2
    result = await storage.query(OWNER, {"plan_id": pid})
    assert result["image_storage_bytes"] == image.size
    assert all(record["images"] == [image.image_id] for record in result["records"])


async def test_untrusted_additions_and_foreign_removals_do_not_modify_records(storage):
    pid = await create_plan(storage)
    rid, image = await add_image(storage, pid)
    before = await storage.snapshot(pid, OWNER)
    for patch in (
        {"add": [image.image_id]},
        {"add": [1]},
        {"add": [True]},
        {"add": [1, 1]},
        {"remove": ["img_" + "0" * 32]},
    ):
        with pytest.raises(PlanError):
            await change(
                storage,
                pid,
                "update_records",
                {"records": [{"record_id": rid, "values": {}, "images": patch}]},
            )
        assert await storage.snapshot(pid, OWNER) == before


async def test_per_record_limit_and_batch_validation_are_atomic(storage):
    pid = await create_plan(storage, preset="generic")
    prepared = {index: storage.media.prepare(image_bytes()) for index in range(1, 5)}
    with pytest.raises(PlanError, match="最多附"):
        await change(
            storage,
            pid,
            "add_records",
            {"records": [{"values": {}, "images": {"add": [1, 2, 3, 4]}}]},
            prepared,
        )
    assert not (await storage.snapshot(pid, OWNER))["records"]
    with pytest.raises(PlanError, match="未知字段"):
        await change(
            storage,
            pid,
            "add_records",
            {
                "records": [
                    {"values": {}, "images": {"add": [1]}},
                    {"values": {"unknown": "x"}},
                ]
            },
            prepared,
        )
    assert not list(storage.media.directory.glob("*.webp"))


@pytest.mark.parametrize("quota", ["image_plan_quota_mb", "image_total_quota_mb"])
async def test_quotas_fail_before_writing(tmp_path, quota):
    storage = Storage(tmp_path / "plans.sqlite3", media_config={quota: 1})
    await storage.initialize()
    pid = await create_plan(storage)
    image = storage.media.prepare(image_bytes())
    large = replace(image, data=image.data + b"x" * 1_048_576)
    with pytest.raises(PlanError, match="额度"):
        await change(
            storage,
            pid,
            "add_records",
            {"records": [{"values": {}, "images": {"add": [1]}}]},
            {1: large},
        )
    assert (await storage.snapshot(pid, OWNER))["revision"] == 1
    assert not list(storage.media.directory.iterdir())


async def test_write_failure_rolls_back_and_orphan_files_are_reclaimed(
    storage, monkeypatch
):
    pid = await create_plan(storage)
    image = storage.media.prepare(image_bytes())
    write = storage.media.write

    def fail(prepared):
        write(prepared)
        raise OSError("disk failure")

    monkeypatch.setattr(storage.media, "write", fail)
    with pytest.raises(OSError):
        await change(
            storage,
            pid,
            "add_records",
            {"records": [{"values": {}, "images": {"add": [1]}}]},
            {1: image},
        )
    assert (await storage.snapshot(pid, OWNER))["revision"] == 1
    with sqlite3.connect(storage.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert not list(storage.media.directory.iterdir())
    write(image)
    old = (datetime.now(timezone.utc) - timedelta(days=3)).timestamp()
    for path in storage.media.directory.iterdir():
        os.utime(path, (old, old))
    await storage.cleanup_media(force=True)
    assert not list(storage.media.directory.iterdir())


async def test_cross_plan_image_id_cannot_overwrite_an_existing_file(storage):
    first_pid = await create_plan(storage)
    rid, image = await add_image(storage, first_pid)
    second_pid = await create_plan(storage)
    different = storage.media.prepare(image_bytes(color="blue"))
    collision = replace(different, image_id=image.image_id)
    with pytest.raises(PlanError, match="其他计划"):
        await change(
            storage,
            second_pid,
            "add_records",
            {"records": [{"values": {}, "images": {"add": [1]}}]},
            {1: collision},
        )
    assert (await storage.record_images(OWNER, first_pid, rid))[0][
        "path"
    ].read_bytes() == image.data
    assert not (await storage.snapshot(second_pid, OWNER))["records"]


async def test_unlink_undo_and_reference_aware_cleanup(storage):
    pid = await create_plan(storage)
    rid, image = await add_image(storage, pid)
    await change(
        storage,
        pid,
        "update_records",
        {
            "records": [
                {"record_id": rid, "values": {}, "images": {"remove": [image.image_id]}}
            ]
        },
    )
    await storage.cleanup_media(force=True)
    assert storage.media.path(image.image_id).exists()
    with sqlite3.connect(storage.path) as connection:
        assert connection.execute("SELECT orphaned_at FROM media").fetchone()[0] is None
    await change(storage, pid, "undo", {})
    assert (await storage.record_images(OWNER, pid, rid))[0][
        "image_id"
    ] == image.image_id
    await change(storage, pid, "delete_records", {"records": [{"record_id": rid}]})
    with sqlite3.connect(storage.path) as connection:
        connection.execute("DELETE FROM changes WHERE plan_id=?", (pid,))
    await storage.cleanup_media(force=True)
    assert storage.media.path(image.image_id).exists()
    with sqlite3.connect(storage.path) as connection:
        connection.execute(
            "UPDATE media SET orphaned_at=?",
            ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),),
        )
    await storage.cleanup_media(force=True)
    assert not storage.media.path(image.image_id).exists()
    assert not storage.media.path(image.image_id, True).exists()


async def test_missing_files_and_unauthorized_readers_fail_closed(storage):
    pid = await create_plan(storage)
    rid, image = await add_image(storage, pid)
    with pytest.raises(PlanError, match="权限"):
        await storage.record_images(Actor("qq-1", "other"), pid, rid)
    with pytest.raises(PlanError, match="权限"):
        await storage.record_images(Actor("different-platform", "owner"), pid, rid)
    storage.media.path(image.image_id).unlink()
    with pytest.raises(PlanError, match="缺失"):
        await storage.record_images(OWNER, pid, rid)


async def test_v5_upgrade_only_adds_media_table(storage):
    pid = await create_plan(storage)
    before = await storage.snapshot(pid, OWNER)
    with sqlite3.connect(storage.path) as connection:
        connection.execute("DROP TABLE media")
        connection.execute("PRAGMA user_version=5")
    await storage.initialize()
    assert await storage.snapshot(pid, OWNER) == before
    with sqlite3.connect(storage.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert connection.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0


@pytest.mark.parametrize("layout", ["mobile", "desktop"])
@pytest.mark.parametrize(
    "view_type,preset",
    [
        ("table", "checkin"),
        ("todo", "checkin"),
        ("todo", "todo"),
        ("calendar", "checkin"),
        ("checkin", "checkin"),
    ],
)
async def test_attachment_counts_render_without_reading_record_images(
    storage, tmp_path, layout, view_type, preset
):
    pid = await create_plan(storage, render_layout=layout, preset=preset)
    rid, image = await add_image(storage, pid)
    await change(storage, pid, "view", {"type": view_type})
    plan = await storage.snapshot(pid, OWNER)
    view = build_view(plan, {}, OWNER.user)["view"]
    if view_type == "checkin":
        assert view["current"]["image_count"] == 1
    elif view_type == "calendar":
        assert sum(cell["image_count"] for cell in view["cells"]) == 1
    else:
        assert view["rows"][0]["image_count"] == 1
        assert rid in view["rows"][0].get(
            "record_ids", [view["rows"][0].get("record_id")]
        )
    renderer = LocalRenderer(tmp_path / "render", {})

    def reject_record_image_load(plan_id, image_id):
        pytest.fail("数量标记不应读取记录附图")

    renderer.image_loader = reject_record_image_load
    html = renderer.html(plan, {}, OWNER.user)
    assert 'aria-label="附图 1 张"' in html
    assert '<div class="attachment-section">' not in html
    assert "data:image/webp;base64," not in html
    assert "img-src data:" in html
    assert str(storage.media.directory) not in html
    assert image.image_id not in html
    storage.media.path(image.image_id, True).unlink()
    assert 'aria-label="附图 1 张"' in renderer.html(plan, {}, OWNER.user)
    await change(storage, pid, "view", {"visible": False})
    hidden_html = renderer.html(await storage.snapshot(pid, OWNER), {}, OWNER.user)
    assert '<span class="attachment-count"' not in hidden_html


@pytest.mark.parametrize("view_type", ["table", "todo"])
async def test_attachment_counts_follow_pagination_and_filtering(storage, view_type):
    pid = await create_plan(storage, preset="todo")
    await change(storage, pid, "view", {"type": view_type})
    image = storage.media.prepare(image_bytes())
    await change(
        storage,
        pid,
        "add_records",
        {
            "records": [
                {"values": {"title": "first"}, "images": {"add": [1]}},
                {"values": {"title": "second"}},
            ]
        },
        {1: image},
    )
    plan = await storage.snapshot(pid, OWNER)
    first = build_view(plan, {"page_size": 1}, OWNER.user)["view"]["rows"]
    second = build_view(plan, {"page": 2, "page_size": 1}, OWNER.user)["view"]["rows"]
    assert first[0]["image_count"] == 1
    assert second[0]["image_count"] == 0
    await change(storage, pid, "view", {"filters": {"equals": {"title": "second"}}})
    assert (
        build_view(await storage.snapshot(pid, OWNER), {}, OWNER.user)["view"]["rows"][
            0
        ]["image_count"]
        == 0
    )


async def test_attachment_counts_include_all_images_across_records(storage):
    pid = await create_plan(storage, preset="todo")
    prepared = {index: storage.media.prepare(image_bytes()) for index in range(1, 4)}
    await change(
        storage,
        pid,
        "add_records",
        {
            "records": [
                {"values": {}, "images": {"add": [1, 2, 3]}} for unused in range(3)
            ]
        },
        prepared,
    )
    rows = build_view(await storage.snapshot(pid, OWNER), {}, OWNER.user)["view"][
        "rows"
    ]
    assert [row["image_count"] for row in rows] == [3, 3, 3]


@pytest.mark.parametrize("mode", ["public", "protected"])
@pytest.mark.parametrize("view_type", ["checkin", "todo"])
async def test_checkin_attachment_counts_follow_group_statistical_user(
    storage, mode, view_type
):
    owner = Actor("qq-1", "owner", "g1")
    member = Actor("qq-1", "member", "g1")
    created = await storage.mutate(
        owner,
        "create",
        {"name": "群打卡", "preset": "checkin", "mode": mode},
        message_key="create-group",
    )
    pid = created["plan_id"]
    first = storage.media.prepare(image_bytes())
    second = storage.media.prepare(image_bytes(color="blue"))
    third = storage.media.prepare(image_bytes(color="green"))
    await storage.mutate(
        owner,
        "add_records",
        {"records": [{"values": {}, "images": {"add": [1, 2]}}]},
        pid,
        1,
        "owner-image",
        {1: first, 2: third},
    )
    await storage.mutate(owner, "view", {"type": view_type}, pid, 2, "checkin-view")
    if mode == "public":
        await storage.mutate(
            member,
            "add_records",
            {"records": [{"values": {}, "images": {"add": [1]}}]},
            pid,
            3,
            "member-image",
            {1: second},
        )
    plan = await storage.snapshot(pid, member)
    view = build_view(plan, {}, member.user)["view"]
    row = view["current"] if view_type == "checkin" else view["rows"][0]
    assert row["image_count"] == (1 if mode == "public" else 2)


@pytest.mark.parametrize("layout", ["mobile", "desktop"])
@pytest.mark.parametrize("view_type", ["table", "todo", "calendar", "checkin"])
async def test_browser_renders_attachment_counts_inside_view(
    storage, tmp_path, browser_service, layout, view_type
):
    pid = await create_plan(storage, render_layout=layout)
    images = {
        index: storage.media.prepare(image_bytes((400, 300), color=color))
        for index, color in enumerate(("#fdbf72", "#7ab8ff", "#89dcb6"), 1)
    }
    await change(
        storage,
        pid,
        "add_records",
        {
            "records": [
                {
                    "values": {"note": "训练完成，保存照片作为记录附件。"},
                    "images": {"add": [1, 2, 3]},
                }
            ]
        },
        images,
    )
    await change(storage, pid, "view", {"type": view_type})
    plan = await storage.snapshot(pid, OWNER)
    renderer = LocalRenderer(
        tmp_path / "preview", {}, browser_service_resolver=lambda: browser_service
    )
    renderer.image_loader = storage.image_thumbnail
    await renderer.initialize()
    try:
        async with browser_service.session(
            javascript_enabled=False,
            viewport={"width": 640 if layout == "mobile" else 1000, "height": 800},
        ) as page:
            await page.set_content(
                renderer.html(plan, {}, OWNER.user), wait_until="load"
            )
            assert await page.locator("img").count() == 0
            badges = page.locator(".attachment-count")
            assert await badges.count() == 1
            assert await badges.get_attribute("aria-label") == "附图 3 张"
            assert await badges.evaluate(
                "badge => { const b = badge.getBoundingClientRect(); "
                "const p = badge.closest('.panel').getBoundingClientRect(); "
                "return b.width > 0 && b.left >= p.left && b.right <= p.right; }"
            )
        output = await renderer.render(plan, {}, OWNER.user)
        with Image.open(output) as screenshot:
            assert screenshot.width == (640 if layout == "mobile" else 1000)
            assert screenshot.height < 3000
        assert output.stat().st_size > 10_000
    finally:
        await renderer.close()
