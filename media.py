"""Bounded message-image ingestion and private persistent media files."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname
from uuid import uuid4

import aiohttp
from PIL import Image, ImageOps, UnidentifiedImageError

from .domain import PlanError

IMAGE_ID = re.compile(r"img_[0-9a-f]{32}\Z")
LIMITS = {
    "references_per_plan": (5, 1, 30),
    "images_per_record": (3, 1, 30),
    "image_max_mb": (10, 1, 50),
    "image_max_pixels": (20_000_000, 1_000_000, 50_000_000),
    "image_plan_quota_mb": (100, 1, 100_000),
    "image_total_quota_mb": (1024, 1, 1_000_000),
    "image_cleanup_days": (2, 1, 3650),
}


def public_address(address: str) -> bool:
    parsed = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    return parsed.is_global and not (
        parsed.is_multicast or parsed.is_reserved or parsed.is_unspecified
    )


def validate_remote_url(source: str) -> None:
    try:
        parsed = urlsplit(source)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and not public_address(str(address)):
            raise ValueError
    except ValueError as exc:
        raise PlanError("图片下载只接受公共 HTTP/HTTPS 地址和标准端口。") from exc


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        addresses = await self.resolver.resolve(host, port, family)
        if not addresses or any(not public_address(item["host"]) for item in addresses):
            raise PlanError("图片地址指向内网或保留地址，已拒绝下载。")
        return addresses

    async def close(self):
        await self.resolver.close()


@dataclass(frozen=True)
class PreparedImage:
    image_id: str
    data: bytes
    thumbnail: bytes
    width: int
    height: int
    sha256: str

    @property
    def size(self) -> int:
        return len(self.data) + len(self.thumbnail)


class MediaLibrary:
    def __init__(self, directory: Path, config: dict | None = None):
        self.directory = Path(directory)
        config = config or {}
        for key, (default, low, high) in LIMITS.items():
            value = config.get(key, default)
            if type(value) is not int or not low <= value <= high:
                raise PlanError(f"{key} 必须是 {low} 到 {high} 的整数。")
            setattr(self, key, value)
        self.max_bytes = self.image_max_mb * 1024 * 1024

    def path(self, image_id: str, thumbnail: bool = False) -> Path:
        if not isinstance(image_id, str) or not IMAGE_ID.fullmatch(image_id):
            raise PlanError("图片 ID 无效。")
        return self.directory / (image_id + (".thumb.webp" if thumbnail else ".webp"))

    def _read_local(self, source: str) -> bytes:
        if source.startswith("file:"):
            parsed = urlsplit(source)
            if parsed.netloc not in {"", "localhost"}:
                raise PlanError("不接受网络共享中的图片。")
            source = url2pathname(parsed.path)
        path = Path(source)
        if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
            raise PlanError("消息图片必须提供有效的本地绝对路径。")
        try:
            if not path.is_file() or path.stat().st_size > self.max_bytes:
                raise PlanError("消息图片不存在或超过单张大小限制。")
            with path.open("rb") as stream:
                return stream.read(self.max_bytes + 1)
        except OSError as exc:
            raise PlanError("无法读取当前消息中的图片。") from exc

    async def read_source(self, source: str) -> bytes:
        if not isinstance(source, str) or not source:
            raise PlanError("当前消息图片没有可用的数据来源。")
        if source.startswith(("http://", "https://")):
            validate_remote_url(source)
            resolver = PublicResolver()
            try:
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(resolver=resolver),
                    timeout=aiohttp.ClientTimeout(total=20, connect=5, sock_read=10),
                    trust_env=False,
                    auto_decompress=False,
                    headers={"Accept-Encoding": "identity"},
                    cookie_jar=aiohttp.DummyCookieJar(),
                ) as session:
                    async with session.get(source, allow_redirects=False) as response:
                        if response.status != 200:
                            raise PlanError(
                                "图片下载失败；不接受重定向，请重新发送图片。"
                            )
                        if (
                            response.headers.get("Content-Encoding", "identity").lower()
                            != "identity"
                        ):
                            raise PlanError(
                                "不接受额外压缩的图片响应，请重新发送图片。"
                            )
                        if (
                            response.content_length
                            and response.content_length > self.max_bytes
                        ):
                            raise PlanError("图片超过单张大小限制。")
                        content = bytearray()
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            content.extend(chunk)
                            if len(content) > self.max_bytes:
                                raise PlanError("图片超过单张大小限制。")
                        return bytes(content)
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                raise PlanError("无法安全下载当前消息图片，请重新发送。") from exc
            finally:
                await resolver.close()
        if source.startswith("base64://") or source.startswith("data:image/"):
            encoded = (
                source[9:]
                if source.startswith("base64://")
                else source.partition(",")[2]
            )
            if len(encoded) > (self.max_bytes + 2) // 3 * 4:
                raise PlanError("图片超过单张大小限制。")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, base64.binascii.Error) as exc:
                raise PlanError("消息图片编码无效。") from exc
            if len(content) > self.max_bytes:
                raise PlanError("图片超过单张大小限制。")
            return content
        return await asyncio.to_thread(self._read_local, source)

    def prepare(self, data: bytes, lossless: bool = False) -> PreparedImage:
        if not data or len(data) > self.max_bytes:
            raise PlanError("图片为空或超过单张大小限制。")
        try:
            with Image.open(BytesIO(data)) as source:
                if source.format not in {"JPEG", "PNG", "WEBP"}:
                    raise PlanError("附图仅支持 JPEG、PNG、WebP，不支持 SVG 或 GIF。")
                if source.width * source.height > self.image_max_pixels:
                    raise PlanError("图片像素过多，请缩小图片后再上传。")
                if getattr(source, "is_animated", False):
                    raise PlanError("暂不支持动态图片，请发送静态图片。")
                source.load()
                with ImageOps.exif_transpose(source) as oriented:
                    mode = (
                        "RGBA"
                        if "A" in oriented.getbands() or "transparency" in oriented.info
                        else "RGB"
                    )
                    with oriented.convert(mode) as converted:
                        image = Image.frombytes(
                            converted.mode, converted.size, converted.tobytes()
                        )
            with image:
                full = BytesIO()
                image.save(
                    full,
                    format="WEBP",
                    quality=90,
                    method=4,
                    lossless=lossless,
                    exact=lossless,
                )
                width, height = image.size
                image.thumbnail((320, 240))
                small = BytesIO()
                image.save(small, format="WEBP", quality=75, method=4)
            content, thumbnail = full.getvalue(), small.getvalue()
            if len(content) > self.max_bytes:
                raise PlanError("处理后的图片仍超过大小限制，请缩小图片。")
            return PreparedImage(
                "img_" + uuid4().hex,
                content,
                thumbnail,
                width,
                height,
                hashlib.sha256(content).hexdigest(),
            )
        except PlanError:
            raise
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            Image.DecompressionBombError,
        ) as exc:
            raise PlanError("图片损坏或格式无效，无法保存。") from exc

    async def prepare_async(self, data: bytes, lossless: bool = False) -> PreparedImage:
        options = {"lossless": True} if lossless else {}
        worker = asyncio.create_task(asyncio.to_thread(self.prepare, data, **options))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            await asyncio.gather(worker, return_exceptions=True)
            raise

    def write(self, image: PreparedImage) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        for thumbnail, content in ((False, image.data), (True, image.thumbnail)):
            path = self.path(image.image_id, thumbnail)
            temporary = path.with_suffix(".tmp-" + uuid4().hex)
            try:
                with temporary.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

    def thumbnail_url(self, image_id: str) -> str:
        try:
            with self.path(image_id, True).open("rb") as stream:
                content = stream.read(200_001)
            if len(content) > 200_000:
                return ""
            return "data:image/webp;base64," + base64.b64encode(content).decode("ascii")
        except OSError:
            return ""


def referenced_images(document) -> set[str]:
    result = set()
    if isinstance(document, dict):
        for key, value in document.items():
            if key == "images" and isinstance(value, list):
                result.update(
                    item
                    for item in value
                    if isinstance(item, str) and IMAGE_ID.fullmatch(item)
                )
            else:
                result.update(referenced_images(value))
    elif isinstance(document, list):
        for value in document:
            result.update(referenced_images(value))
    return result
