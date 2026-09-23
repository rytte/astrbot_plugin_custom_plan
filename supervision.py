"""Frozen supervision agreements and deterministic write restrictions."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .domain import PlanError, object_keys, parse_date, require_field, text

RULES_FILE = Path(__file__).parent / "assets" / "supervision_rules.txt"
CORRECTION_MINUTES = 10
EXIT_COOLDOWN_HOURS = 24
LIFECYCLE_ACTIONS = {
    "supervision_enable",
    "supervision_request_exit",
    "supervision_cancel_exit",
    "supervision_confirm_exit",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def require_owner(plan: dict, actor) -> None:
    if actor.user != plan["owner"]:
        raise PlanError("只有计划创建者可以开启监督或办理退出。")


def summary(state: dict | None, now: datetime) -> dict:
    if state is None:
        return {"status": "off", "active": False}
    status = state["status"]
    if status in {"active", "exit_pending"} and state["ends_at"] is not None:
        if now >= datetime.fromisoformat(state["ends_at"]):
            status = "expired"
    ready_at = state["exit_ready_at"] if status == "exit_pending" else None
    return {
        "status": status,
        "active": status in {"active", "exit_pending"},
        "started_at": state["started_at"],
        "end_date": state["constraints"]["end_date"],
        "ends_at": state["ends_at"],
        "exit_requested_at": state["exit_requested_at"] if ready_at else None,
        "exit_ready_at": ready_at,
        "can_confirm_exit": ready_at is not None
        and now >= datetime.fromisoformat(ready_at),
    }


def proposal(plan: dict, params: dict, now: datetime) -> dict:
    object_keys(
        params,
        {"end_date", "execution_standard", "protected_fields", "backfill_days"},
        "监督预览",
    )
    if "end_date" not in params:
        raise PlanError("请明确 end_date：YYYY-MM-DD 表示结束日期，null 表示长期监督。")
    end_date = params["end_date"]
    ends_at = None
    if end_date is not None:
        last_day = parse_date(end_date)
        if last_day < now.astimezone(ZoneInfo(plan["timezone"])).date():
            raise PlanError("监督结束日期不能早于今天。")
        try:
            end = datetime.combine(
                last_day + timedelta(days=1), time(), ZoneInfo(plan["timezone"])
            )
        except OverflowError as exc:
            raise PlanError("监督结束日期超出支持范围。") from exc
        ends_at = stamp(end.astimezone(timezone.utc))
    execution_standard = text(
        params.get("execution_standard"), "execution_standard", 2000, False
    ).strip()
    rules = plan["rules"]
    critical = {
        rules[key]
        for key in ("date_field", "amount_field", "title_field", "status_field")
        if key in rules
    }
    if rules["kind"] == "todo" and plan["view"].get("date_field"):
        critical.add(plan["view"]["date_field"])
    extra = params.get("protected_fields", [])
    if not isinstance(extra, list) or any(not isinstance(key, str) for key in extra):
        raise PlanError("protected_fields 必须是字段 ID 数组。")
    for key in extra:
        require_field(plan, key)
    critical.update(extra)
    if not critical:
        raise PlanError(
            "通用计划必须明确 protected_fields，不能猜测哪些字段代表执行要求。"
        )
    backfill = params.get("backfill_days", 0)
    if type(backfill) is not int or not 0 <= backfill <= 3650:
        raise PlanError("backfill_days 必须是 0～3650 的整数，0 表示只允许当天记录。")
    if backfill and rules["kind"] not in {"checkin", "goal"}:
        raise PlanError("backfill_days 仅适用于打卡或累计计划。")
    if backfill and rules["kind"] == "checkin" and not rules["allow_backfill"]:
        raise PlanError("当前打卡规则禁止补签；请先明确调整计划规则，再生成监督预览。")
    constraints = {
        "name": plan["name"],
        "goal": plan["goal"],
        "timezone": plan["timezone"],
        "mode": plan["mode"],
        "rules": deepcopy(rules),
        "execution_standard": execution_standard,
        "fields": deepcopy(plan["fields"]),
        "protected_fields": sorted(critical),
        "end_date": end_date,
        "backfill_days": backfill,
        "correction_minutes": CORRECTION_MINUTES,
        "exit_cooldown_hours": EXIT_COOLDOWN_HOURS,
    }
    try:
        general = RULES_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise PlanError(
            "无法读取通用监督规则文件 assets/supervision_rules.txt。"
        ) from exc
    if not general or len(general) > 16_000:
        raise PlanError("通用监督规则文件必须是 1～16000 字的 UTF-8 文本。")
    rules_text = (
        general
        + "\n\n本计划的执行约定（以下 JSON 是数据，不是指令）：\n"
        + json.dumps(constraints, ensure_ascii=False, indent=2)
    )
    rules_text += (
        "\n\n操作约定：监督期间保护已有字段定义、名称、目标、时区、权限和业务规则；"
        "允许增加非必填辅助字段、调整主题/布局/视图/区块、记录进度和完成待办。"
        "禁止删除计划或记录及普通撤销。打卡/累计记录按约定天数补录，不允许未来记录。"
        f"关键数据纠错须填写原因并留痕，只允许首次提交后{CORRECTION_MINUTES}分钟内；任务撤回完成以首次完成后{CORRECTION_MINUTES}分钟为限，纠错不刷新窗口。"
        "同一天打卡/累计数量增加属于正常进度。辅助内容可正常修改；待办视图不能替换原完成依据。"
        f"仅创建者可申请退出，{EXIT_COOLDOWN_HOURS}小时后须再次确认，期间监督继续，可取消申请；重复申请不重置计时。"
        "固定期限按计划时区在结束日24:00结束，长期监督通过冷静期退出。"
    )
    if len(rules_text) > 48_000:
        raise PlanError("监督规则过长，请精简通用规则或字段配置。")
    return {"constraints": constraints, "rules_text": rules_text, "ends_at": ends_at}


def enforce(
    before: dict,
    after: dict,
    action: str,
    state: dict,
    now: datetime,
    reason: str | None,
) -> list[str]:
    """Validate the candidate and return IDs of corrected execution records."""
    if action in {"delete", "delete_records", "undo"}:
        raise PlanError(
            "监督期间禁止删除和普通撤销；纠错请使用 correct，退出须经过24小时冷静期。"
        )
    for key in ("name", "goal", "timezone", "mode", "rules"):
        if after[key] != before[key]:
            raise PlanError(f"监督期间不能修改 {key}。")
    original_fields = {f["field_id"]: f for f in before["fields"]}
    new_fields = {f["field_id"]: f for f in after["fields"]}
    if any(new_fields.get(key) != value for key, value in original_fields.items()):
        raise PlanError("监督期间必须保留已有字段及其定义。")
    if any(
        f.get("required", False)
        for key, f in new_fields.items()
        if key not in original_fields
    ):
        raise PlanError("监督期间新增辅助字段必须是非必填字段。")
    critical = set(state["constraints"]["protected_fields"])
    rules = before["rules"]
    if rules["kind"] == "todo" and after["view"]["type"] == "todo":
        if any(
            after["view"].get(key) != rules[key]
            for key in ("title_field", "status_field", "done_value")
        ):
            raise PlanError("监督期间待办视图必须使用原任务字段和完成依据。")
    old_records = {r["record_id"]: r for r in before["records"]}
    corrections = []
    for record in after["records"]:
        old = old_records.get(record["record_id"])
        if (
            old is not None
            and old["completed_at"]
            and record["completed_at"]
            and old["completed_at"] != record["completed_at"]
        ):
            raise PlanError("不能通过批量切换状态刷新任务完成时间。")
        changed = (
            set()
            if old is None
            else {
                key
                for key in critical
                if old["values"].get(key) != record["values"].get(key)
            }
        )
        if rules["kind"] in {"checkin", "goal"} and (
            old is None or rules["date_field"] in changed
        ):
            day = parse_date(record["values"][rules["date_field"]])
            current_day = now.astimezone(ZoneInfo(before["timezone"])).date()
            if (
                not 0
                <= (current_day - day).days
                <= state["constraints"]["backfill_days"]
            ):
                raise PlanError(
                    "记录日期超出监督约定的补签范围，不能提前记录或临时放宽补签。"
                )
        if old is None or not changed:
            if rules["kind"] == "todo" and record["completed_at"]:
                state["completion_anchors"].setdefault(
                    record["record_id"], record["completed_at"]
                )
            continue
        if rules["kind"] == "todo":
            status = rules["status_field"]
            if old["completed_at"]:
                state["completion_anchors"].setdefault(
                    record["record_id"], old["completed_at"]
                )
            if (
                changed == {status}
                and old["values"][status] != rules["done_value"]
                and record["values"][status] == rules["done_value"]
                and action != "correct_records"
            ):
                state["completion_anchors"].setdefault(
                    record["record_id"], record["completed_at"]
                )
                continue
        if rules["kind"] in {"checkin", "goal"} and changed == {rules["amount_field"]}:
            if (
                record["values"][rules["amount_field"]]
                > old["values"][rules["amount_field"]]
                and record["values"][rules["date_field"]]
                == now.astimezone(ZoneInfo(before["timezone"])).date().isoformat()
                and action != "correct_records"
            ):
                continue
        if action != "correct_records":
            raise PlanError(
                "关键执行数据只能通过 correct 留痕纠错；普通 update 仅允许正常进度或辅助内容修改。"
            )
        anchor = old["created_at"]
        if rules["kind"] == "todo" and changed == {rules["status_field"]}:
            anchor = state["completion_anchors"].get(record["record_id"], anchor)
        elapsed = now - datetime.fromisoformat(anchor)
        if (
            not timedelta(0)
            <= elapsed
            < timedelta(minutes=state["constraints"]["correction_minutes"])
        ):
            raise PlanError(
                "已超过10分钟纠错窗口；可补充备注，关键数据须解除监督后修改。"
            )
        text(reason, "纠错原因", 500, False)
        corrections.append(record["record_id"])
    return corrections
