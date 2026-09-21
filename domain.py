"""Validated plan documents shared by all views and model tools."""

from __future__ import annotations

import calendar
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VIEWS = {"table", "checkin", "todo", "calendar"}
BLOCKS = {"statistics", "notes"}
MAX_RECORDS = 2000
MAX_FIELDS = 24
MAX_BLOCKS = 12


class PlanError(ValueError):
    """An actionable validation or authorization failure."""


@dataclass(frozen=True)
class Actor:
    platform: str
    user: str
    group: str = ""

    def __post_init__(self):
        if not self.platform or not self.user:
            raise PlanError("无法识别当前平台或用户。")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today(plan: dict) -> date:
    return datetime.now(ZoneInfo(plan["timezone"])).date()


def object_keys(value, allowed: set[str], label: str) -> dict:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise PlanError(f"{label}必须是对象。")
    unexpected = set(value) - allowed
    if unexpected:
        raise PlanError(f"{label}包含不支持的参数：{', '.join(sorted(unexpected))}")
    return value


def text(value, label: str, maximum: int = 500, empty: bool = True) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not empty and not value.strip())
    ):
        raise PlanError(
            f"{label}必须是{'非空' if not empty else ''}文本，最多 {maximum} 字。"
        )
    return value


def number(value, label: str) -> float | int:
    if type(value) not in (float, int) or not math.isfinite(value) or abs(value) > 1e12:
        raise PlanError(f"{label}必须是有限数值，绝对值不超过 10¹²。")
    return value


def parse_date(value) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise PlanError("日期必须是 YYYY-MM-DD。")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PlanError("日期不存在。") from exc


def allowed(plan: dict, actor: Actor, write: bool = False) -> bool:
    if plan["platform"] != actor.platform:
        return False
    if plan["scope"] == "person":
        return plan["owner"] == actor.user and (
            not actor.group or plan["mode"] == "shared"
        )
    if not actor.group or plan["group"] != actor.group:
        return False
    return not write or plan["owner"] == actor.user or plan["mode"] == "public"


def authorize(plan: dict, actor: Actor, write: bool = False) -> None:
    if not allowed(plan, actor, write):
        raise PlanError("计划不存在或当前会话没有操作权限。")


def field(field_id: str, name: str, kind: str, **kwargs) -> dict:
    return {
        "field_id": field_id,
        "name": name,
        "type": kind,
        "required": False,
        **kwargs,
    }


def create_plan(actor: Actor, params: dict, default_timezone: str) -> dict:
    """Build a preset with one dataset and a default table view.

    Args:
        actor: Trusted event identity.
        params: Validated creation parameters, never ownership identifiers.
        default_timezone: Administrator-selected default timezone.

    Returns:
        A complete document ready for transactional storage.
    """
    object_keys(
        params,
        {"name", "goal", "preset", "scope", "mode", "timezone", "target", "unit"},
        "创建参数",
    )
    preset = params.get("preset", "generic")
    if preset not in {"generic", "checkin", "goal", "todo"}:
        raise PlanError("预设必须是 generic、checkin、goal 或 todo。")
    if "target" in params and preset != "goal":
        raise PlanError("target 仅用于 goal 预设；打卡每日阈值通过 rules 配置。")
    if "unit" in params and preset not in {"checkin", "goal"}:
        raise PlanError("unit 仅用于 checkin/goal 预设；通用字段单位通过 fields 配置。")
    scope = params.get("scope", "group" if actor.group else "person")
    if scope not in {"person", "group"} or (scope == "group" and not actor.group):
        raise PlanError("群计划只能在所属群中创建。")
    timestamp = now_iso()
    plan = {
        "plan_id": "p_" + uuid4().hex[:16],
        "platform": actor.platform,
        "owner": actor.user,
        "group": actor.group if scope == "group" else "",
        "scope": scope,
        "mode": params.get("mode", "protected" if scope == "group" else "private"),
        "name": params.get("name", ""),
        "goal": params.get("goal", ""),
        "timezone": params.get("timezone", default_timezone),
        "preset": preset,
        "revision": 1,
        "created_at": timestamp,
        "updated_at": timestamp,
        "deleted": False,
        "fields": [],
        "records": [],
        "rules": {"kind": "generic"},
        "view": {
            "type": "table",
            "visible": True,
            "fields": [],
            "filters": {},
            "sort_field": "",
            "descending": False,
        },
        "blocks": [],
    }
    if preset in {"checkin", "goal"}:
        unit = text(params.get("unit", "次"), "单位", 20)
        plan["fields"] = [
            field("date", "日期", "date", required=True),
            field("amount", "数量", "number", required=True, unit=unit),
            field("note", "备注", "text"),
        ]
        plan["view"].update(date_field="date", amount_field="amount")
        plan["rules"] = {"kind": preset, "date_field": "date", "amount_field": "amount"}
        if preset == "checkin":
            plan["rules"].update(
                allow_backfill=True,
                multiple_per_day=False,
                thresholds=[{"from": "0001-01-01", "value": 1}],
            )
        else:
            plan["rules"]["target"] = params.get("target", 100)
            plan["blocks"] = [
                {
                    "block_id": "b_" + uuid4().hex[:12],
                    "type": "statistics",
                    "title": "累计目标",
                    "visible": True,
                    "config": {
                        "operation": "sum",
                        "field_id": "amount",
                        "target_from_goal": True,
                        "filters": {},
                    },
                }
            ]
    elif preset == "todo":
        plan["fields"] = [
            field("title", "任务", "text", required=True),
            field(
                "status", "状态", "status", required=True, options=["待完成", "已完成"]
            ),
            field("due", "截止日期", "date"),
            field("note", "备注", "text"),
        ]
        plan["rules"] = {
            "kind": "todo",
            "title_field": "title",
            "status_field": "status",
            "done_value": "已完成",
            "pending_value": "待完成",
        }
        plan["view"].update(
            title_field="title",
            status_field="status",
            done_value="已完成",
            date_field="due",
        )
    validate_plan(plan)
    authorize(plan, actor, True)
    return plan


def require_field(plan: dict, field_id, kinds: set[str] | None = None) -> dict:
    result = next((f for f in plan["fields"] if f["field_id"] == field_id), None)
    if result is None or (kinds and result["type"] not in kinds):
        raise PlanError(f"字段 {field_id!s} 不存在或类型不符合要求，请先查询计划字段。")
    return result


def validate_filters(plan: dict, filters: dict) -> None:
    object_keys(filters, {"equals", "date_field", "start", "end"}, "筛选条件")
    equals = filters.get("equals", {})
    if not isinstance(equals, dict):
        raise PlanError("equals 必须是 field_id 到值的对象。")
    for key, value in equals.items():
        validate_value(require_field(plan, key), value, required=False)
    if any(key in filters for key in ("date_field", "start", "end")):
        require_field(plan, filters.get("date_field"), {"date"})
        start = parse_date(filters["start"]) if filters.get("start") else date.min
        end = parse_date(filters["end"]) if filters.get("end") else date.max
        if start > end:
            raise PlanError("筛选开始日期不能晚于结束日期。")


def select_records(plan: dict, filters: dict | None = None) -> list[dict]:
    filters = filters or {}
    validate_filters(plan, filters)
    result = []
    for record in plan["records"]:
        values = record["values"]
        if any(
            values.get(key) != value for key, value in filters.get("equals", {}).items()
        ):
            continue
        if filters.get("date_field"):
            value = values.get(filters["date_field"])
            if (
                not value
                or value < filters.get("start", "0001-01-01")
                or value > filters.get("end", "9999-12-31")
            ):
                continue
        result.append(record)
    return result


def validate_value(definition: dict, value, required: bool = True) -> None:
    if value is None:
        if required and definition.get("required"):
            raise PlanError(f"字段 {definition['name']} 必填。")
        return
    kind = definition["type"]
    if kind == "number":
        number(value, definition["name"])
    elif kind == "date":
        parse_date(value)
    else:
        text(
            value,
            definition["name"],
            500,
            empty=not (required and definition.get("required")),
        )
        if kind == "status" and value not in definition["options"]:
            raise PlanError(
                f"{definition['name']} 只允许：{', '.join(definition['options'])}"
            )


def validate_plan(plan: dict) -> None:
    """Validate references and the complete document before committing.

    Args:
        plan: Candidate document including all records and presentation settings.

    Raises:
        PlanError: A field, reference, business rule, or resource limit is invalid.
    """
    text(plan["name"], "名称", 80, False)
    text(plan["goal"], "目标", 500)
    try:
        ZoneInfo(text(plan["timezone"], "时区", 80, False))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PlanError("无效的 IANA 时区，例如 Asia/Shanghai。") from exc
    if plan["mode"] not in (
        {"private", "shared"} if plan["scope"] == "person" else {"protected", "public"}
    ):
        raise PlanError("个人计划支持 private/shared，群计划支持 protected/public。")
    if not isinstance(plan["fields"], list) or len(plan["fields"]) > MAX_FIELDS:
        raise PlanError(f"最多 {MAX_FIELDS} 个字段。")
    ids = set()
    for definition in plan["fields"]:
        object_keys(
            definition,
            {"field_id", "name", "type", "unit", "required", "options"},
            "字段",
        )
        identifier = definition.get("field_id")
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,39}", identifier)
            or identifier in ids
        ):
            raise PlanError(
                "field_id 必须是唯一的 1～40 位英文、数字或下划线，且以字母开头。"
            )
        ids.add(identifier)
        text(definition.get("name"), "字段名称", 40, False)
        if definition.get("type") not in {"text", "number", "date", "status"}:
            raise PlanError("字段类型支持 text/number/date/status。")
        if type(definition.get("required", False)) is not bool:
            raise PlanError("required 必须是布尔值。")
        text(definition.get("unit", ""), "单位", 20)
        if definition["type"] == "status":
            options = definition.get("options")
            if not isinstance(options, list) or not 1 <= len(options) <= 20:
                raise PlanError("状态字段需要 1～20 个 options。")
            for option in options:
                text(option, "状态值", 40, False)
            if len(set(options)) != len(options):
                raise PlanError("状态值不能重复。")
    rules = plan["rules"]
    kind = rules["kind"]
    if kind == "checkin":
        if any(
            type(rules[key]) is not bool
            for key in ("allow_backfill", "multiple_per_day")
        ):
            raise PlanError("打卡开关必须是布尔值。")
        if not 1 <= len(rules["thresholds"]) <= 365:
            raise PlanError("打卡阈值规则最多保留 365 个生效日期。")
        for threshold in rules["thresholds"]:
            parse_date(threshold["from"])
            if number(threshold["value"], "每日阈值") <= 0:
                raise PlanError("每日阈值必须大于 0。")
    if kind in {"checkin", "goal"}:
        require_field(plan, rules["date_field"], {"date"})
        require_field(plan, rules["amount_field"], {"number"})
    if kind == "goal" and number(rules["target"], "目标值") <= 0:
        raise PlanError("目标值必须大于 0。")
    if kind == "todo":
        require_field(plan, rules["title_field"], {"text"})
        status = require_field(plan, rules["status_field"], {"status"})
        if (
            rules["done_value"] not in status["options"]
            or rules["pending_value"] not in status["options"]
            or rules["done_value"] == rules["pending_value"]
        ):
            raise PlanError("任务状态选项必须保留完成和待完成状态。")
    view = object_keys(
        plan["view"],
        {
            "type",
            "visible",
            "fields",
            "date_field",
            "amount_field",
            "title_field",
            "status_field",
            "done_value",
            "filters",
            "sort_field",
            "descending",
        },
        "主视图",
    )
    if (
        view.get("type") not in VIEWS
        or type(view.get("visible", True)) is not bool
        or type(view.get("descending", False)) is not bool
    ):
        raise PlanError(
            "主视图支持 table/checkin/todo/calendar，visible/descending 必须是布尔值。"
        )
    columns = view.get("fields", [])
    if not isinstance(columns, list) or len(columns) > MAX_FIELDS:
        raise PlanError("视图 fields 必须是字段 ID 数组。")
    for key in columns:
        require_field(plan, key)
    for key, kinds in (
        ("date_field", {"date"}),
        ("amount_field", {"number"}),
        ("title_field", {"text"}),
        ("status_field", {"status"}),
    ):
        if view.get(key):
            require_field(plan, view[key], kinds)
    if view.get("sort_field"):
        require_field(plan, view["sort_field"])
    if view["type"] in {"calendar", "checkin"}:
        require_field(plan, view.get("date_field"), {"date"})
    if view["type"] == "checkin" and (
        kind != "checkin" or view["date_field"] != rules["date_field"]
    ):
        raise PlanError("打卡面板需要 checkin 预设及其打卡日期字段。")
    if view["type"] == "todo":
        require_field(plan, view.get("title_field"), {"text"})
        status = require_field(plan, view.get("status_field"), {"status"})
        if view.get("done_value") not in status["options"]:
            raise PlanError("待办视图需要指定有效的 done_value。")
    validate_filters(plan, view.get("filters", {}))
    if len(plan["records"]) > MAX_RECORDS:
        raise PlanError(f"首版每个计划最多 {MAX_RECORDS} 条记录。")
    dates = set()
    for record in plan["records"]:
        values = record["values"]
        if set(values) - ids:
            raise PlanError("记录包含未知字段。")
        for definition in plan["fields"]:
            validate_value(definition, values.get(definition["field_id"]))
        if kind == "todo":
            text(values.get(rules["title_field"]), "任务名称", 500, False)
            if (
                values.get(rules["status_field"])
                not in require_field(plan, rules["status_field"])["options"]
            ):
                raise PlanError("任务必须包含有效状态。")
        if kind in {"checkin", "goal"}:
            parse_date(values.get(rules["date_field"]))
            if number(values.get(rules["amount_field"]), "数量") < 0:
                raise PlanError("打卡或累计数量不能为负数；请修改原记录来更正。")
        if kind == "checkin":
            day = values.get(rules["date_field"])
            if not day:
                raise PlanError("打卡日期必填。")
            day_key = (record["author"], day)
            if not rules["multiple_per_day"] and day_key in dates:
                raise PlanError("同一用户同一天只能有一条打卡，请修改已有记录。")
            dates.add(day_key)
    if len(plan["blocks"]) > MAX_BLOCKS:
        raise PlanError(f"最多 {MAX_BLOCKS} 个扩展区块。")
    for block in plan["blocks"]:
        if block["type"] not in BLOCKS or type(block.get("visible", True)) is not bool:
            raise PlanError("区块支持 statistics/notes，visible 必须是布尔值。")
        text(block.get("title", ""), "区块标题", 80)
        config = block["config"]
        if block["type"] == "notes":
            object_keys(config, {"text"}, "随笔配置")
            text(config.get("text", ""), "随笔", 2000)
        else:
            object_keys(
                config,
                {"operation", "field_id", "filters", "target", "target_from_goal"},
                "统计配置",
            )
            if config.get("operation") not in {"count", "sum", "average", "min", "max"}:
                raise PlanError("统计支持 count/sum/average/min/max。")
            if config["operation"] != "count":
                require_field(plan, config.get("field_id"), {"number"})
            validate_filters(plan, config.get("filters", {}))
            if "target" in config and number(config["target"], "统计目标值") <= 0:
                raise PlanError("统计目标值必须大于 0。")
            if "target_from_goal" in config:
                if type(config["target_from_goal"]) is not bool:
                    raise PlanError("target_from_goal 必须是布尔值。")
                if config["target_from_goal"] and (
                    kind != "goal"
                    or config.get("field_id") != rules["amount_field"]
                    or config["operation"] != "sum"
                    or "target" in config
                ):
                    raise PlanError(
                        "绑定计划目标需使用累计目标的数量字段求和，且不能另设 target。"
                    )


def apply_change(plan: dict, action: str, params: dict, actor: Actor) -> list[str]:
    """Apply one bounded operation; storage validates and commits atomically.

    Args:
        plan: Isolated mutable candidate document.
        action: Registered operation name.
        params: Operation-specific payload without identity fields.
        actor: Trusted event actor.

    Returns:
        IDs created or changed by the operation.
    """
    changed = []
    if action == "update":
        object_keys(params, {"name", "goal", "mode", "timezone"}, "计划修改")
        if (
            "timezone" in params
            and params["timezone"] != plan["timezone"]
            and plan["records"]
        ):
            raise PlanError("已有记录的计划暂不支持更改时区，避免改变日期语义。")
        plan.update(params)
    elif action == "delete":
        object_keys(params, set(), "删除参数")
        plan["deleted"] = True
    elif action == "fields":
        object_keys(params, {"fields"}, "字段配置")
        definitions = params.get("fields")
        if not isinstance(definitions, list):
            raise PlanError(
                "fields 必须是完整的字段定义数组，已有字段沿用原 field_id。"
            )
        old_ids = {f["field_id"] for f in plan["fields"]}
        plan["fields"] = deepcopy(definitions)
        new_ids = {
            f.get("field_id")
            for f in definitions
            if isinstance(f, dict) and isinstance(f.get("field_id"), str)
        }
        for record in plan["records"]:
            for removed in old_ids - new_ids:
                record["values"].pop(removed, None)
    elif action == "view":
        object_keys(
            params,
            {
                "type",
                "visible",
                "fields",
                "date_field",
                "amount_field",
                "title_field",
                "status_field",
                "done_value",
                "filters",
                "sort_field",
                "descending",
            },
            "视图配置",
        )
        plan["view"].update(deepcopy(params))
    elif action == "rules":
        kind = plan["rules"]["kind"]
        if kind == "generic":
            new_kind = params.get("kind")
            if new_kind in {"checkin", "goal"}:
                keys = {"kind", "date_field", "amount_field"} | (
                    {"threshold", "allow_backfill", "multiple_per_day"}
                    if new_kind == "checkin"
                    else {"target"}
                )
                object_keys(params, keys, "绑定业务规则")
                rules = {
                    "kind": new_kind,
                    "date_field": params.get("date_field"),
                    "amount_field": params.get("amount_field"),
                }
                if new_kind == "checkin":
                    rules.update(
                        allow_backfill=params.get("allow_backfill", True),
                        multiple_per_day=params.get("multiple_per_day", False),
                        thresholds=[
                            {"from": "0001-01-01", "value": params.get("threshold", 1)}
                        ],
                    )
                else:
                    rules["target"] = params.get("target", 100)
            elif new_kind == "todo":
                object_keys(
                    params,
                    {
                        "kind",
                        "title_field",
                        "status_field",
                        "done_value",
                        "pending_value",
                    },
                    "绑定任务规则",
                )
                rules = {
                    "kind": "todo",
                    "title_field": params.get("title_field"),
                    "status_field": params.get("status_field"),
                    "done_value": params.get("done_value", "已完成"),
                    "pending_value": params.get("pending_value", "待完成"),
                }
                for record in plan["records"]:
                    if (
                        record["values"].get(rules["status_field"])
                        == rules["done_value"]
                    ):
                        record["completed_at"] = now_iso()
            else:
                raise PlanError(
                    "通用计划可绑定 kind=checkin/goal/todo 及已有字段 ID；绑定前应确认数据含义。"
                )
            plan["rules"] = rules
            plan["preset"] = new_kind
            for key in (
                "date_field",
                "amount_field",
                "title_field",
                "status_field",
                "done_value",
            ):
                if key in rules:
                    plan["view"].setdefault(key, rules[key])
        elif kind == "checkin":
            object_keys(
                params,
                {"threshold", "effective_from", "allow_backfill", "multiple_per_day"},
                "打卡规则",
            )
            for key in ("allow_backfill", "multiple_per_day"):
                if key in params:
                    if type(params[key]) is not bool:
                        raise PlanError(f"{key} 必须是布尔值。")
                    plan["rules"][key] = params[key]
            if "threshold" in params:
                value = number(params["threshold"], "每日达标数量")
                start = parse_date(params.get("effective_from"))
                if value <= 0 or start < today(plan):
                    raise PlanError("阈值必须大于 0，生效日期不能早于今天。")
                thresholds = [
                    v
                    for v in plan["rules"]["thresholds"]
                    if v["from"] != start.isoformat()
                ]
                thresholds.append({"from": start.isoformat(), "value": value})
                plan["rules"]["thresholds"] = sorted(
                    thresholds, key=lambda v: v["from"]
                )
            elif "effective_from" in params:
                raise PlanError("effective_from 需与 threshold 一起提供。")
        elif kind == "goal":
            object_keys(params, {"target"}, "累计目标规则")
            plan["rules"].update(params)
        else:
            raise PlanError("该预设没有可修改的业务规则；字段和视图可分别配置。")
    elif action in {"add_records", "update_records", "delete_records"}:
        object_keys(params, {"records"}, "记录操作")
        items = params.get("records")
        if not isinstance(items, list) or not 1 <= len(items) <= 50:
            raise PlanError("records 必须包含 1～50 条操作。")
        rules = plan["rules"]
        for item in items:
            object_keys(
                item,
                {"values"}
                if action == "add_records"
                else (
                    {"record_id", "values"}
                    if action == "update_records"
                    else {"record_id"}
                ),
                "记录",
            )
            record = None
            if action != "add_records":
                record = next(
                    (
                        r
                        for r in plan["records"]
                        if r["record_id"] == item.get("record_id")
                    ),
                    None,
                )
                if record is None:
                    raise PlanError("记录不存在，请先查询有效的 record_id。")
            if action == "delete_records":
                plan["records"].remove(record)
                changed.append(record["record_id"])
                continue
            values = item.get("values")
            if not isinstance(values, dict):
                raise PlanError("values 必须是 field_id 到值的对象。")
            if record is None:
                record = {
                    "record_id": "r_" + uuid4().hex[:12],
                    "values": {},
                    "author": actor.user,
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                    "completed_at": None,
                }
                if rules["kind"] in {"checkin", "goal"}:
                    record["values"][rules["date_field"]] = today(plan).isoformat()
                if rules["kind"] == "checkin":
                    record["values"][rules["amount_field"]] = 1
                if rules["kind"] == "todo":
                    record["values"][rules["status_field"]] = rules["pending_value"]
                plan["records"].append(record)
            previous = deepcopy(record["values"])
            record["values"].update(deepcopy(values))
            record["updated_at"] = now_iso()
            if rules["kind"] == "checkin":
                day = parse_date(record["values"].get(rules["date_field"]))
                if day > today(plan):
                    raise PlanError("不能提前打卡。未来安排请使用待办记录。")
                if (
                    not rules["allow_backfill"]
                    and day < today(plan)
                    and (
                        action == "add_records"
                        or previous.get(rules["date_field"]) != day.isoformat()
                    )
                ):
                    raise PlanError("此计划不允许补签。")
            if rules["kind"] == "todo":
                complete = (
                    record["values"].get(rules["status_field"]) == rules["done_value"]
                )
                record["completed_at"] = (
                    (record["completed_at"] or now_iso()) if complete else None
                )
            changed.append(record["record_id"])
    elif action == "block_add":
        object_keys(params, {"type", "title", "config", "visible"}, "区块")
        block = {
            "block_id": "b_" + uuid4().hex[:12],
            "type": params.get("type"),
            "title": params.get("title", ""),
            "visible": params.get("visible", True),
            "config": deepcopy(params.get("config", {})),
        }
        plan["blocks"].append(block)
        changed.append(block["block_id"])
    elif action in {"block_update", "block_delete"}:
        object_keys(
            params,
            {"block_id", "title", "config", "visible"}
            if action == "block_update"
            else {"block_id"},
            "区块操作",
        )
        block = next(
            (b for b in plan["blocks"] if b["block_id"] == params.get("block_id")), None
        )
        if block is None:
            raise PlanError("区块不存在。")
        changed.append(block["block_id"])
        if action == "block_delete":
            plan["blocks"].remove(block)
        else:
            block.update({k: deepcopy(v) for k, v in params.items() if k != "block_id"})
    elif action == "block_order":
        object_keys(params, {"block_ids"}, "区块排序")
        order = params.get("block_ids")
        if (
            not isinstance(order, list)
            or not all(isinstance(i, str) for i in order)
            or len(order) != len(plan["blocks"])
            or set(order) != {b["block_id"] for b in plan["blocks"]}
        ):
            raise PlanError("排序需要提供全部区块 ID，各出现一次。")
        mapping = {b["block_id"]: b for b in plan["blocks"]}
        plan["blocks"] = [mapping[key] for key in order]
    else:
        raise PlanError("不支持的操作。")
    validate_plan(plan)
    return changed


def month_bounds(value: str) -> tuple[date, date]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
        raise PlanError("月份格式必须是 YYYY-MM。")
    start = parse_date(value + "-01")
    return start, start.replace(day=calendar.monthrange(start.year, start.month)[1])
