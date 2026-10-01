"""Deterministic view models and trusted template registries."""

from __future__ import annotations

import calendar
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .appearance import LAYOUTS
from .domain import (
    PlanError,
    checkin_user,
    month_bounds,
    object_keys,
    optional_field_id,
    select_records,
    today,
    todo_done,
    todo_title_field,
    validate_render_layout,
)

VIEW_TEMPLATES = {
    name: f"views/{name}.html" for name in ("table", "checkin", "todo", "calendar")
}
BLOCK_TEMPLATES = {name: f"blocks/{name}.html" for name in ("statistics", "notes")}
VIEW_NAMES = {
    "table": "通用表格",
    "checkin": "打卡面板",
    "todo": "待办列表",
    "calendar": "日历",
}


def display(value) -> str:
    if value is None:
        return "—"
    if type(value) is float:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return str(value)


def checkin_days(plan: dict, records: list[dict]) -> dict:
    rules = plan["rules"]
    quantities = defaultdict(float)
    counts = defaultdict(int)
    for record in records:
        key = record["values"][rules["date_field"]]
        quantities[key] += record["values"][rules["amount_field"]]
        counts[key] += 1
    days = {}
    for key, quantity in quantities.items():
        threshold = next(
            v["value"] for v in reversed(rules["thresholds"]) if v["from"] <= key
        )
        days[key] = {
            "amount": quantity,
            "count": counts[key],
            "threshold": threshold,
            "done": quantity >= threshold,
        }
    return days


def checkin_records(
    plan: dict, records: list[dict], actor_user: str
) -> tuple[str, list[dict]]:
    user = checkin_user(plan, actor_user)
    return user, [r for r in records if r["author"] == user]


def record_date(plan: dict, record: dict) -> str:
    for identifier in (
        plan["rules"].get("date_field"),
        plan["view"].get("date_field"),
        "date",
    ):
        if optional_field_id(plan, identifier, "date"):
            value = record["values"].get(identifier)
            if value:
                return value
    return (
        datetime.fromisoformat(record["created_at"])
        .astimezone(ZoneInfo(plan["timezone"]))
        .date()
        .isoformat()
    )


def todo_details(plan: dict, records: list[dict], day: str) -> dict:
    title_field = todo_title_field(plan)
    titles = list(
        dict.fromkeys(
            record["values"][title_field]
            for record in records
            if title_field and record["values"].get(title_field)
        )
    )
    notes = (
        [record["values"]["note"] for record in records if record["values"].get("note")]
        if optional_field_id(plan, "note", "text")
        else []
    )
    show_due = optional_field_id(plan, "due", "date") is not None
    due_dates = (
        list(
            dict.fromkeys(
                record["values"]["due"]
                for record in records
                if record["values"].get("due")
            )
        )
        if show_due
        else []
    )
    return {
        "date": day,
        "record_ids": [record["record_id"] for record in records],
        "title": "、".join(titles) if titles else f"{day} · {plan['name']}",
        "notes": notes,
        "show_due": show_due,
        "due_dates": due_dates,
    }


def checkin_todo_rows(plan: dict, records: list[dict], actor_user: str) -> list[dict]:
    _, personal = checkin_records(plan, records, actor_user)
    days = checkin_days(plan, personal)
    grouped = defaultdict(list)
    for record in personal:
        grouped[record["values"][plan["rules"]["date_field"]]].append(record)
    unit = next(
        f.get("unit", "")
        for f in plan["fields"]
        if f["field_id"] == plan["rules"]["amount_field"]
    )
    rows = [
        {
            **details,
            **todo_details(plan, grouped[day], day),
            "unit": unit,
            "state_label": "已达标" if details["done"] else "未达标",
            "completed_at": None,
        }
        for day, details in days.items()
    ]
    if not plan["view"].get("sort_field"):
        rows.sort(key=lambda row: (row["done"], row["date"]))
    return rows


def todo_rows(plan: dict, records: list[dict], actor_user: str) -> list[dict]:
    if plan["rules"]["kind"] == "checkin":
        return checkin_todo_rows(plan, records, actor_user)
    amount_field = plan["view"]["amount_field"]
    unit = next(
        definition.get("unit", "")
        for definition in plan["fields"]
        if definition["field_id"] == amount_field
    )
    rows = []
    for record in records:
        done = todo_done(record, amount_field)
        rows.append(
            {
                **todo_details(plan, [record], record_date(plan, record)),
                "record_id": record["record_id"],
                "amount": record["values"].get(amount_field),
                "threshold": None,
                "unit": unit,
                "done": done,
                "state_label": "已完成" if done else "待完成",
                "completed_at": record["completed_at"],
            }
        )
    if not plan["view"].get("sort_field"):
        rows.sort(key=lambda row: row["done"])
    return rows


def build_view(
    plan: dict,
    options: dict,
    actor_user: str,
    user_names: dict[str, str] | None = None,
) -> dict:
    """Compute bounded presentation data without mutating the source snapshot.

    Args:
        plan: Authorized immutable database snapshot.
        options: Per-render paging and month selection.
        actor_user: Trusted requesting user for public-group check-in views.
        user_names: Platform-resolved nicknames keyed by user ID, for display only.

    Returns:
        Template data containing a view and independently computed blocks.
    """
    object_keys(options, {"page", "page_size", "column_page", "month"}, "看板选项")
    layout = validate_render_layout(plan.get("render_layout"))
    page, size, column_page = (
        options.get("page", 1),
        options.get("page_size", LAYOUTS[layout].page_size),
        options.get("column_page", 1),
    )
    if (
        type(page) is not int
        or page < 1
        or type(size) is not int
        or not 1 <= size <= 30
        or type(column_page) is not int
        or column_page < 1
    ):
        raise PlanError("page/column_page 为正整数，page_size 为 1～30。")
    config = plan["view"]
    kind = config["type"]
    checkin_list = kind == "todo" and plan["rules"]["kind"] == "checkin"
    records = select_records(plan, config.get("filters", {}))
    sort_field = config.get("sort_field")
    if sort_field:
        populated = [r for r in records if r["values"].get(sort_field) is not None]
        missing = [r for r in records if r["values"].get(sort_field) is None]
        records = (
            sorted(
                populated,
                key=lambda r: r["values"][sort_field],
                reverse=config.get("descending", False),
            )
            + missing
        )
    current_day = today(plan)
    list_rows = todo_rows(plan, records, actor_user) if kind == "todo" else []
    total = len(list_rows) if kind == "todo" else len(records)
    page_count = max(1, math.ceil(total / size))
    if config.get("visible", True) and kind in {"table", "todo"} and page > page_count:
        raise PlanError(f"记录只有 {page_count} 页。")
    view = {
        "type": kind,
        "template": VIEW_TEMPLATES[kind],
        "name": VIEW_NAMES[kind],
        "visible": config.get("visible", True),
        "total": total,
        "page": page,
        "page_size": size,
        "pages": page_count,
        "filtered": bool(config.get("filters")),
        "today": current_day.isoformat(),
        "checkin_list": checkin_list,
        "item_unit": "天" if checkin_list else "项",
        "pending_label": "未达标" if checkin_list else "未完成",
        "completed_label": "已达标" if checkin_list else "已完成",
        "scope_label": (user_names or {}).get(checkin_user(plan, actor_user), "")
        if plan["scope"] == "group" and (kind == "checkin" or checkin_list)
        else "",
    }
    if not view["visible"]:
        pass
    elif kind == "table":
        mapping = {f["field_id"]: f for f in plan["fields"]}
        fields = [mapping[f] for f in config.get("fields", [])] or plan["fields"]
        column_pages = max(1, math.ceil(len(fields) / 6))
        if column_page > column_pages:
            raise PlanError(f"字段只有 {column_pages} 页。")
        fields = fields[(column_page - 1) * 6 : column_page * 6]
        view.update(
            fields=fields,
            column_page=column_page,
            column_pages=column_pages,
            rows=[
                {
                    "record_id": r["record_id"],
                    "cells": [display(r["values"].get(f["field_id"])) for f in fields],
                }
                for r in records[(page - 1) * size : page * size]
            ],
        )
    elif kind == "todo":
        view.update(
            completed=sum(row["done"] for row in list_rows),
            rows=list_rows[(page - 1) * size : page * size],
        )
    elif kind == "checkin":
        _, personal = checkin_records(plan, records, actor_user)
        days = checkin_days(plan, personal)
        cells = []
        for offset in range(27, -1, -1):
            day = current_day - timedelta(days=offset)
            details = days.get(day.isoformat())
            cells.append(
                {
                    "date": day.isoformat(),
                    "label": day.strftime("%m/%d"),
                    "state": "done"
                    if details and details["done"]
                    else "partial"
                    if details
                    else "empty",
                    "amount": display(details["amount"]) if details else "—",
                    "today": day == current_day,
                }
            )
        last = current_day
        if not days.get(last.isoformat(), {}).get("done"):
            last -= timedelta(days=1)
        streak = 0
        while days.get(last.isoformat(), {}).get("done"):
            streak += 1
            last -= timedelta(days=1)
        amount_field = next(
            f for f in plan["fields"] if f["field_id"] == plan["rules"]["amount_field"]
        )
        threshold = next(
            t["value"]
            for t in reversed(plan["rules"]["thresholds"])
            if t["from"] <= current_day.isoformat()
        )
        current = days.get(current_day.isoformat(), {"done": False, "amount": 0})
        view.update(
            cells=cells,
            streak=streak,
            current=current,
            threshold=threshold,
            unit=amount_field.get("unit", ""),
            days_done=sum(cell["state"] == "done" for cell in cells),
        )
    elif kind == "calendar":
        month = options.get("month", current_day.strftime("%Y-%m"))
        start, end = month_bounds(month)
        grouped = defaultdict(list)
        undated = 0
        for record in records:
            day = record["values"].get(config["date_field"])
            if not day:
                undated += 1
            elif start.isoformat() <= day <= end.isoformat():
                grouped[day].append(record)
        cells = []
        for week in calendar.Calendar(firstweekday=0).monthdayscalendar(
            start.year, start.month
        ):
            for day in week:
                key = date(start.year, start.month, day).isoformat() if day else ""
                entries = grouped.get(key, [])
                labels = []
                for record in entries if layout == "mobile" else entries[:2]:
                    if config.get("title_field"):
                        label = str(
                            record["values"].get(config["title_field"]) or "未命名"
                        )
                    elif config.get("amount_field"):
                        label = display(record["values"].get(config["amount_field"]))
                    else:
                        label = "记录"
                    labels.append(label[:12] + ("…" if len(label) > 12 else ""))
                cells.append(
                    {
                        "day": day,
                        "date": key,
                        "today": key == current_day.isoformat(),
                        "labels": labels,
                        "count": len(entries),
                    }
                )
        view.update(
            month=month,
            cells=cells,
            undated=undated,
            monthly_count=sum(len(items) for items in grouped.values()),
        )
    visible_records = []
    if view["visible"]:
        if kind == "table":
            visible_records = records[(page - 1) * size : page * size]
        elif kind == "todo":
            visible_ids = {
                identifier for row in view["rows"] for identifier in row["record_ids"]
            }
            visible_records = [
                record for record in records if record["record_id"] in visible_ids
            ]
        elif kind == "checkin":
            visible_records = [
                record
                for record in personal
                if record["values"][plan["rules"]["date_field"]]
                == current_day.isoformat()
            ]
        elif kind == "calendar":
            visible_records = [
                record for entries in grouped.values() for record in entries
            ]
    image_count = sum(len(record.get("images", [])) for record in visible_records)
    image_records = []
    remaining = 6
    for record in visible_records:
        if remaining and record.get("images"):
            images = record["images"][:remaining]
            image_records.append(
                {
                    "record_id": record["record_id"],
                    "date": record_date(plan, record),
                    "images": [
                        {"image_id": identifier, "src": ""} for identifier in images
                    ],
                }
            )
            remaining -= len(images)
    blocks = []
    for block in plan["blocks"]:
        if not block.get("visible", True):
            continue
        data = {**block, "template": BLOCK_TEMPLATES[block["type"]]}
        if block["type"] == "notes":
            data["text"] = block["config"].get("text", "")
        else:
            settings = block["config"]
            selected = select_records(plan, settings.get("filters", {}))
            op = settings["operation"]
            values = (
                [
                    r["values"][settings["field_id"]]
                    for r in selected
                    if r["values"].get(settings.get("field_id")) is not None
                ]
                if op != "count"
                else []
            )
            value = (
                len(selected)
                if op == "count"
                else sum(values)
                if op == "sum"
                else (
                    sum(values) / len(values)
                    if op == "average"
                    else min(values)
                    if op == "min"
                    else max(values)
                )
                if values
                else None
            )
            data.update(
                value=value,
                scope_label="区块指定筛选范围"
                if settings.get("filters")
                else "全部记录",
                operation_name={
                    "count": "记录数",
                    "sum": "合计",
                    "average": "平均",
                    "min": "最小",
                    "max": "最大",
                }[op],
                unit="条"
                if op == "count"
                else next(
                    f.get("unit", "")
                    for f in plan["fields"]
                    if f["field_id"] == settings["field_id"]
                ),
            )
            target = (
                plan["rules"]["target"]
                if settings.get("target_from_goal")
                else settings.get("target")
            )
            data.update(
                target=target,
                progress=max(0, min(100, value / target * 100))
                if target and value is not None
                else None,
            )
        blocks.append(data)
    return {
        "plan": plan,
        "view": view,
        "blocks": blocks,
        "image_records": image_records,
        "image_count": image_count,
        "references": [
            {
                **item,
                "images": [
                    {"image_id": identifier, "src": ""} for identifier in item["images"]
                ],
            }
            for item in plan.get("references", [])[:3]
        ],
        "reference_count": len(plan.get("references", [])),
        "supervised": plan.get("supervision", {}).get("active", False),
    }
