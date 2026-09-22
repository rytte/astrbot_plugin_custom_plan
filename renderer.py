"""Bounded, offline Jinja2 and Chromium screenshot rendering."""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from .appearance import LAYOUTS, THEMES
from .domain import PlanError, now_iso, validate_render_theme
from .presentation import build_view, display


class LocalRenderer:
    def __init__(self, directory: Path, config: dict):
        self.directory = Path(directory)
        self.config = config
        self.root = Path(__file__).parent
        self.environment = Environment(
            loader=FileSystemLoader(self.root / "templates"),
            autoescape=select_autoescape(("html",)),
            undefined=StrictUndefined,
        )
        self.environment.filters["display"] = display
        self.environment.filters["localtime"] = lambda value, zone: (
            datetime.fromisoformat(value)
            .astimezone(ZoneInfo(zone))
            .strftime("%Y-%m-%d %H:%M")
        )
        self.browser = None
        self.playwright = None
        self.lock = asyncio.Lock()
        self.pending = 0
        self.closed = False
        self.tasks: set[asyncio.Task] = set()
        self.font_css = ""
        assets = self.root / "assets"
        self.base_styles = (assets / "base.css").read_text(encoding="utf-8")
        self.layout_styles = {
            key: (assets / layout.stylesheet).read_text(encoding="utf-8")
            for key, layout in LAYOUTS.items()
        }
        self.theme_styles = {
            key: (assets / theme.stylesheet).read_text(encoding="utf-8")
            for key, theme in THEMES.items()
        }

    async def initialize(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        font_path = self.config.get("font_path", "")
        if font_path:
            path = Path(font_path)
            if not path.is_file() or path.stat().st_size > 30_000_000:
                raise PlanError("字体文件不存在或超过 30 MB。")
            data = await asyncio.to_thread(path.read_bytes)
            self.font_css = (
                "@font-face{font-family:PlanFont;src:url(data:font/ttf;base64,"
                + base64.b64encode(data).decode("ascii")
                + ");font-display:block;}"
            )

    def html(self, plan: dict, options: dict, actor_user: str) -> str:
        data = build_view(plan, options, actor_user)
        layout = plan["render_layout"]
        theme = validate_render_theme(plan.get("render_theme"))
        definition = LAYOUTS[layout]
        data["view"]["template"] = definition.template_prefix + data["view"]["template"]
        styles = "\n".join(
            (
                self.base_styles,
                self.layout_styles[layout],
                self.theme_styles[theme],
                f":root{{--board-width:{definition.width}px;}}",
            )
        )
        return self.environment.get_template("board.html").render(
            **data,
            styles=styles,
            font_css=self.font_css,
            layout=layout,
            theme=theme,
            generated_at=now_iso(),
        )

    async def render(self, plan: dict, options: dict, actor_user: str) -> Path:
        """Render a snapshot while bounding queue length, pixels, and duration.

        Args:
            plan: Authorized versioned snapshot.
            options: Bounded paging options.
            actor_user: Trusted user identity for the check-in panel.

        Returns:
            An owned temporary image path to remove after sending.

        Raises:
            PlanError: Rendering is disabled, overloaded, or cannot fit.
        """
        if self.closed or not self.config.get("render_enabled", True):
            raise PlanError("本地图片渲染未启用。")
        if self.pending >= self.config.get("render_queue_size", 4) + 1:
            raise PlanError("截图队列已满，请稍后重试。")
        self.pending += 1
        task = asyncio.current_task()
        self.tasks.add(task)
        path = self.directory / ("plan-" + uuid4().hex + ".png")
        try:
            async with asyncio.timeout(self.config.get("render_timeout", 40)):
                async with self.lock:
                    if self.closed:
                        raise PlanError("插件正在卸载。")
                    html = await asyncio.to_thread(self.html, plan, options, actor_user)
                    if self.browser is None or not self.browser.is_connected():
                        if self.playwright is None:
                            from playwright.async_api import async_playwright

                            self.playwright = await async_playwright().start()
                        launch = {"headless": True}
                        if self.config.get("browser_executable"):
                            launch["executable_path"] = self.config[
                                "browser_executable"
                            ]
                        self.browser = await self.playwright.chromium.launch(**launch)
                    context = await self.browser.new_context(
                        viewport={
                            "width": LAYOUTS[plan["render_layout"]].width,
                            "height": 800,
                        },
                        device_scale_factor=1,
                        java_script_enabled=False,
                        service_workers="block",
                    )
                    try:
                        await context.route("**/*", lambda route: route.abort())
                        page = await context.new_page()
                        await page.set_content(html, wait_until="load")
                        await page.evaluate("document.fonts.ready")
                        board = page.locator(".board")
                        box = await board.bounding_box()
                        if (
                            box is None
                            or box["width"] * box["height"] > 8_000_000
                            or box["height"] > 8000
                        ):
                            raise PlanError(
                                "看板过长，请减少 page_size、隐藏部分区块或调整展示字段。"
                            )
                        await board.screenshot(
                            path=str(path), animations="disabled", timeout=15_000
                        )
                    finally:
                        with suppress(Exception):
                            async with asyncio.timeout(5):
                                await context.close()
                    return path
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            self.pending -= 1
            self.tasks.discard(task)

    async def close(self):
        self.closed = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self.lock:
            try:
                if self.browser is not None:
                    await self.browser.close()
            finally:
                self.browser = None
                if self.playwright is not None:
                    await self.playwright.stop()
                    self.playwright = None
