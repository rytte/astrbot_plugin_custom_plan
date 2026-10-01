"""Encode authorized saved images for the current conversation model."""

from __future__ import annotations

import base64
import hashlib

from mcp.types import ImageContent

from .domain import PlanError


def encode_images(images, max_bytes):
    contents = []
    total = 0
    for image in images:
        with image["path"].open("rb") as stream:
            content = stream.read(max_bytes + 1)
        total += len(content)
        if len(content) > max_bytes or total > 50 * 1024 * 1024:
            raise PlanError(
                "本次加载的图片总大小超过50 MB或单张超过配置上限，请减少图片。"
            )
        if hashlib.sha256(content).hexdigest() != image["sha256"]:
            raise PlanError(
                "保存图内容已变化或损坏，请联系管理员恢复备份；未向模型提供图片。"
            )
        contents.append(
            ImageContent(
                type="image",
                data=base64.b64encode(content).decode("ascii"),
                mimeType="image/webp",
            )
        )
    return contents
