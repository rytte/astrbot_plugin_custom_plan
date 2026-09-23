"""SQLite transactions, authorization, optimistic revisions, and undo."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .appearance import DEFAULT_THEME, THEMES
from .domain import (
    Actor,
    PlanError,
    allowed,
    apply_change,
    authorize,
    create_plan,
    now_iso,
    object_keys,
    select_records,
    today,
    validate_plan,
    validate_render_layout,
    validate_render_theme,
)


def encode(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class Storage:
    """Keep one bounded JSON dataset per plan and short undo history.

    Each operation owns its connection in a worker thread. SQLite's write
    transaction serializes revisions across instances as well as async tasks.
    """

    def __init__(
        self,
        path: Path,
        timezone: str = "Asia/Shanghai",
        default_render_layout: str = "mobile",
        default_render_theme: str = DEFAULT_THEME,
        deleted_retention_days: int = 7,
    ):
        self.path = Path(path)
        self.timezone = timezone
        self.default_render_layout = validate_render_layout(
            default_render_layout, "default_render_layout"
        )
        self.default_render_theme = validate_render_theme(
            default_render_theme, "default_render_theme"
        )
        if (
            type(deleted_retention_days) is not int
            or not 1 <= deleted_retention_days <= 3650
        ):
            raise PlanError("deleted_retention_days 必须是 1 到 3650 的整数。")
        self.deleted_retention_days = deleted_retention_days

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise PlanError(f"不支持的计划数据库版本：{version}，当前支持版本 3。")
            if version == 3:
                self._purge_expired_deleted(connection)
                return
            if version == 0:
                for statement in (
                    """CREATE TABLE plans (
                    plan_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    document TEXT NOT NULL
                    )""",
                    "CREATE INDEX plans_scope ON plans(platform, owner, group_id)",
                    """CREATE TABLE changes (
                    plan_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    previous TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(plan_id, revision),
                    FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
                    )""",
                    """CREATE TABLE requests (
                    request_key TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
                    )""",
                ):
                    connection.execute(statement)
            else:
                self._migrate_appearance(connection, version)
            connection.execute("PRAGMA user_version=3")
            self._purge_expired_deleted(connection)

    def _purge_expired_deleted(self, connection) -> None:
        """Permanently remove soft-deleted plans after the configured retention period."""
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self.deleted_retention_days
        )
        rows = connection.execute("SELECT plan_id, document FROM plans").fetchall()
        expired = []
        for row in rows:
            document = json.loads(row["document"])
            if not document.get("deleted"):
                continue
            deletion = connection.execute(
                """
                SELECT created_at
                FROM changes
                WHERE plan_id=? AND action='delete'
                ORDER BY revision DESC
                LIMIT 1
                """,
                (row["plan_id"],),
            ).fetchone()
            if deletion is None:
                continue
            try:
                deleted_at = datetime.fromisoformat(deletion["created_at"])
            except (TypeError, ValueError) as exc:
                raise PlanError("已删除计划的删除时间格式无效，无法执行清理。") from exc
            if deleted_at.tzinfo is None:
                raise PlanError("已删除计划的删除时间缺少时区信息，无法执行清理。")
            if deleted_at <= cutoff:
                expired.append(row["plan_id"])
        for plan_id in expired:
            connection.execute("DELETE FROM requests WHERE plan_id=?", (plan_id,))
            connection.execute("DELETE FROM changes WHERE plan_id=?", (plan_id,))
            connection.execute("DELETE FROM plans WHERE plan_id=?", (plan_id,))

    @staticmethod
    def _migrate_appearance(connection, version: int) -> None:
        """Upgrade v1/v2 documents and undo snapshots once, preserving their look."""
        for table, column in (("plans", "document"), ("changes", "previous")):
            rows = connection.execute(
                f"SELECT rowid, {column} FROM {table} WHERE {column} IS NOT NULL"
            ).fetchall()
            for row in rows:
                document = json.loads(row[column])
                changed = False
                if version == 1 and "render_layout" not in document:
                    document["render_layout"] = "mobile"
                    changed = True
                if "render_theme" not in document:
                    document["render_theme"] = "forest"
                    changed = True
                validate_render_layout(document.get("render_layout"))
                validate_render_theme(document["render_theme"])
                if changed:
                    connection.execute(
                        f"UPDATE {table} SET {column}=? WHERE rowid=?",
                        (encode(document), row["rowid"]),
                    )

    def _load(
        self,
        connection,
        plan_id: str,
        actor: Actor,
        write: bool = False,
        deleted: bool = False,
    ) -> dict:
        row = connection.execute(
            "SELECT document FROM plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise PlanError("计划不存在或当前会话没有操作权限。")
        plan = json.loads(row["document"])
        authorize(plan, actor, write)
        if plan["deleted"] and not deleted:
            raise PlanError("计划已删除，可查询已删除计划并尝试撤销最近变更。")
        return plan

    async def snapshot(
        self, plan_id: str, actor: Actor, include_deleted: bool = False
    ) -> dict:
        return await asyncio.to_thread(self._snapshot, plan_id, actor, include_deleted)

    def _snapshot(self, plan_id, actor, include_deleted):
        with self._connect() as connection:
            connection.execute("BEGIN")
            return self._load(connection, plan_id, actor, deleted=include_deleted)

    async def query(self, actor: Actor, params: dict) -> dict:
        return await asyncio.to_thread(self._query, actor, params)

    def _query(self, actor: Actor, params: dict) -> dict:
        object_keys(
            params,
            {"plan_id", "page", "page_size", "filters", "include_deleted"},
            "查询参数",
        )
        page, size = params.get("page", 1), params.get("page_size", 20)
        if (
            type(page) is not int
            or page < 1
            or type(size) is not int
            or not 1 <= size <= 50
        ):
            raise PlanError("page 为正整数，page_size 为 1～50。")
        if type(params.get("include_deleted", False)) is not bool:
            raise PlanError("include_deleted 必须是布尔值。")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._purge_expired_deleted(connection)
            if not params.get("plan_id"):
                if params.get("filters"):
                    raise PlanError("记录筛选需要指定 plan_id。")
                rows = connection.execute(
                    "SELECT document FROM plans WHERE platform=? AND (owner=? OR group_id=?) ORDER BY rowid DESC",
                    (actor.platform, actor.user, actor.group),
                ).fetchall()
                plans = []
                for row in rows:
                    plan = json.loads(row["document"])
                    if allowed(plan, actor) and (
                        not plan["deleted"] or params.get("include_deleted")
                    ):
                        plans.append(
                            {
                                k: plan[k]
                                for k in (
                                    "plan_id",
                                    "name",
                                    "scope",
                                    "mode",
                                    "preset",
                                    "render_layout",
                                    "render_theme",
                                    "revision",
                                    "deleted",
                                )
                            }
                        )
                return {
                    "plans": plans[(page - 1) * size : page * size],
                    "total": len(plans),
                    "page": page,
                    "page_size": size,
                    "default_view": "table",
                    "default_render_layout": self.default_render_layout,
                    "default_render_theme": self.default_render_theme,
                    "themes": [
                        {"id": key, "name": theme.name} for key, theme in THEMES.items()
                    ],
                    "timezone": self.timezone,
                    "today": today({"timezone": self.timezone}).isoformat(),
                    "presets": ["generic", "checkin", "goal", "todo"],
                    "views": ["table", "checkin", "todo", "calendar"],
                    "blocks": ["statistics", "notes"],
                }
            plan = self._load(
                connection,
                params["plan_id"],
                actor,
                deleted=params.get("include_deleted", False),
            )
            records = select_records(plan, params.get("filters", {}))
            result = deepcopy(plan)
            result.pop("platform")
            result.pop("owner")
            result.pop("group")
            result["records"] = records[(page - 1) * size : page * size]
            result.update(
                total=len(records),
                page=page,
                page_size=size,
                can_write=allowed(plan, actor, True),
                today=today(plan).isoformat(),
            )
            if len(encode(result)) > 64_000:
                raise PlanError("查询内容过多，请减小 page_size 或增加筛选条件。")
            return result

    async def mutate(
        self,
        actor: Actor,
        action: str,
        params: dict,
        plan_id: str = "",
        revision: int = 0,
        message_key: str = "",
    ) -> dict:
        """Commit one operation with a stable request key and revision check.

        Args:
            actor: Event identity, never model-supplied.
            action: Registered operation.
            params: Action payload.
            plan_id: Existing plan ID except when creating.
            revision: Last observed version for an existing plan.
            message_key: Platform message identity for retry deduplication.

        Returns:
            Committed IDs and revision, or the original result on a retry.
        """
        return await asyncio.to_thread(
            self._mutate, actor, action, params, plan_id, revision, message_key
        )

    def _mutate(self, actor, action, params, plan_id, revision, message_key):
        if not isinstance(params, dict):
            raise PlanError("params 必须是对象。")
        if not message_key:
            raise PlanError("缺少可信的消息标识。")
        try:
            request_content = encode(
                [
                    actor.platform,
                    actor.user,
                    actor.group,
                    message_key,
                    action,
                    plan_id,
                    params,
                ]
            )
        except (ValueError, TypeError) as exc:
            raise PlanError("参数必须是有效 JSON，不能包含无穷大或 NaN。") from exc
        if len(request_content.encode("utf-8")) > 128_000:
            raise PlanError("单次操作过大，请分批提交。")
        key = hashlib.sha256(request_content.encode()).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._purge_expired_deleted(connection)
            replay = connection.execute(
                "SELECT plan_id,result FROM requests WHERE request_key=?", (key,)
            ).fetchone()
            if replay:
                current = self._load(
                    connection, replay["plan_id"], actor, write=True, deleted=True
                )
                return {
                    **json.loads(replay["result"]),
                    "replayed": True,
                    "current_revision": current["revision"],
                    "deleted": current["deleted"],
                }
            previous = None
            changed = []
            if action == "create":
                count = connection.execute(
                    "SELECT COUNT(*) FROM plans WHERE platform=? AND owner=?",
                    (actor.platform, actor.user),
                ).fetchone()[0]
                if count >= 100:
                    raise PlanError(
                        "每位用户最多创建 100 个计划（含可恢复的已删除计划）。"
                    )
                plan = create_plan(
                    actor,
                    params,
                    self.timezone,
                    self.default_render_layout,
                    self.default_render_theme,
                )
            else:
                plan = self._load(
                    connection, plan_id, actor, write=True, deleted=action == "undo"
                )
                if type(revision) is not int or revision != plan["revision"]:
                    raise PlanError(
                        f"版本冲突：当前版本为 {plan['revision']}，请重新查询后再操作。"
                    )
                previous = encode(plan)
                if action == "undo":
                    object_keys(params, set(), "撤销参数")
                    entry = connection.execute(
                        "SELECT action,previous FROM changes WHERE plan_id=? AND revision=?",
                        (plan_id, revision),
                    ).fetchone()
                    if (
                        entry is None
                        or entry["previous"] is None
                        or entry["action"] == "undo"
                    ):
                        raise PlanError(
                            "没有可撤销的最近变更；首版不支持连续撤销或撤销创建。"
                        )
                    plan = json.loads(entry["previous"])
                    authorize(plan, actor, True)
                else:
                    changed = apply_change(plan, action, params, actor)
                plan["revision"] = revision + 1
                plan["updated_at"] = now_iso()
            validate_plan(plan)
            serialized = encode(plan)
            if len(serialized.encode("utf-8")) > 4_000_000:
                raise PlanError("计划数据超过 4 MB，请减少长文本或拆分计划。")
            connection.execute(
                "INSERT INTO plans(plan_id,platform,owner,group_id,document) VALUES(?,?,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET document=excluded.document",
                (
                    plan["plan_id"],
                    plan["platform"],
                    plan["owner"],
                    plan["group"],
                    serialized,
                ),
            )
            connection.execute(
                "INSERT INTO changes VALUES(?,?,?,?,?,?)",
                (
                    plan["plan_id"],
                    plan["revision"],
                    actor.user,
                    action,
                    previous,
                    now_iso(),
                ),
            )
            connection.execute(
                "DELETE FROM changes WHERE plan_id=? AND revision < ?",
                (plan["plan_id"], plan["revision"] - 19),
            )
            result = {
                "plan_id": plan["plan_id"],
                "revision": plan["revision"],
                "changed_ids": changed,
                "render_layout": plan["render_layout"],
                "render_theme": plan["render_theme"],
                "deleted": plan["deleted"],
            }
            connection.execute(
                "INSERT INTO requests VALUES(?,?,?,?)",
                (key, plan["plan_id"], encode(result), now_iso()),
            )
            return result
