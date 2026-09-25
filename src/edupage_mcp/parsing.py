"""Turn raw EduPage payloads into plain, agent-friendly dicts.

Everything here is a pure function of the JSON EduPage sends back, so it can be
tested without a network. EduPage's field names are Slovak (``nazov`` = name,
``popis`` = description, ``vlastnik`` = owner, ``cas_udalosti`` = event time);
the output uses English names throughout.
"""

from __future__ import annotations

import json
import unicodedata
from datetime import date, datetime, tzinfo
from typing import Any, Iterable

from edupage_api.timeline import EventType

# Timeline types that are bookkeeping for the web app, never shown to people.
HELPER_PREFIX = "h_"
# Shown in the web app but rarely what a person is asking about: EduPage's own
# product tips, "new menu published", "new timetable published", and the
# account's own read receipts.
SYSTEM_TYPES = frozenset({"genotif", "contest", "confirmation", "stravamenu", "timetable"})
MESSAGE_TYPES = frozenset({"sprava", "chat"})
EXAM_TYPES = frozenset({"bexam", "sexam", "oexam", "rexam", "pexam", "testing", "etesthw"})
MEAL_SLOTS = {
    "0": "breakfast",
    "1": "snack",
    "2": "lunch",
    "3": "afternoon_snack",
    "4": "dinner",
}


# -- small helpers -------------------------------------------------------------


def fold(text: str) -> str:
    """Case- and accent-insensitive form for matching names typed by a person."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


def loads(value: Any) -> Any:
    """EduPage nests JSON documents as strings inside JSON; decode if needed."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def local_timestamp(value: str | None, tz: tzinfo) -> str | None:
    """'2026-09-25 13:04:00' (school-local, naive) -> ISO 8601 with offset."""
    if not value:
        return None
    try:
        parsed = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            return value
    return parsed.replace(tzinfo=tz).isoformat()


def date_part(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def type_name(code: str | None) -> str | None:
    """'sprava' -> 'message', 'sexam' -> 'short_exam', unknown codes unchanged."""
    if not code:
        return None
    parsed = EventType.parse(code)
    return parsed.name.lower() if parsed is not None else code


def _clean(values: Iterable[Any]) -> list[Any]:
    return [v for v in values if v not in (None, "")]


class Directory:
    """Look up teachers, subjects, classrooms, classes and students by ID.

    Wraps the ``dbi`` block EduPage ships with the login page. IDs arrive as
    strings or ints, and can be negative.
    """

    def __init__(self, dbi: dict[str, Any] | None):
        self.dbi = dbi or {}

    def _get(self, group: str, item_id: Any) -> dict[str, Any] | None:
        table = self.dbi.get(group)
        if not isinstance(table, dict) or item_id in (None, ""):
            return None
        return table.get(str(item_id))

    @staticmethod
    def _person_name(row: dict[str, Any] | None) -> str | None:
        if not row:
            return None
        return " ".join(_clean([row.get("firstname"), row.get("lastname")])).strip() or None

    def teacher(self, teacher_id: Any) -> str | None:
        return self._person_name(self._get("teachers", teacher_id))

    def student(self, student_id: Any) -> str | None:
        return self._person_name(self._get("students", student_id))

    def subject(self, subject_id: Any) -> dict[str, Any] | None:
        row = self._get("subjects", subject_id)
        if row is None:
            return None
        return {"id": str(subject_id), "name": row.get("name"), "short": row.get("short")}

    def subject_name(self, subject_id: Any) -> str | None:
        row = self._get("subjects", subject_id)
        return (row.get("name") or row.get("short")) if row else None

    def classroom(self, classroom_id: Any) -> str | None:
        row = self._get("classrooms", classroom_id)
        return (row.get("short") or row.get("name")) if row else None

    def class_name(self, class_id: Any) -> str | None:
        row = self._get("classes", class_id)
        return (row.get("short") or row.get("name")) if row else None

    def class_row(self, class_id: Any) -> dict[str, Any] | None:
        return self._get("classes", class_id)

    def event_type(self, code: str | None) -> str | None:
        row = self._get("event_types", code)
        return row.get("name") if row else None

    def absence_type(self, type_id: Any) -> str | None:
        row = self._get("studentabsent_types", type_id)
        return row.get("name") if row else None

    def rows(self, group: str) -> list[dict[str, Any]]:
        table = self.dbi.get(group)
        if isinstance(table, dict):
            return [row for key, row in table.items() if key and isinstance(row, dict)]
        return []


# -- timetable -----------------------------------------------------------------


def parse_timetable(items: list[dict[str, Any]], directory: Directory) -> list[dict[str, Any]]:
    """Lessons from ``currenttt.js`` ``ttitems``, sorted by date and start time.

    A substituted lesson comes back twice: the original with ``removed`` set
    and its replacement with ``changed`` set. Both are kept so the caller can
    say what it replaced.
    """
    lessons = []
    for item in items or []:
        if not item.get("date"):
            continue
        kind = item.get("type") or "card"
        subject = directory.subject(item.get("subjectid"))
        lesson = {
            "date": item["date"],
            "period": item.get("uniperiod") or None,
            "periods": int(item.get("durationperiods") or 1),
            "start_time": item.get("starttime") or None,
            "end_time": item.get("endtime") or None,
            "kind": "lesson" if kind == "card" else kind,
            "subject": subject["name"] if subject else item.get("name"),
            "subject_short": subject["short"] if subject else None,
            "teachers": _clean(directory.teacher(t) for t in item.get("teacherids") or []),
            "classrooms": _clean(directory.classroom(c) for c in item.get("classroomids") or []),
            "classes": _clean(directory.class_name(c) for c in item.get("classids") or []),
            "groups": _clean(item.get("groupnames") or []),
            "changed": bool(item.get("changed")),
            "cancelled": bool(item.get("removed")),
        }
        if item.get("name") and subject:
            lesson["name"] = item["name"]
        # "2026-09-29:F4836CB2..." -> the homework/exam attached to this lesson.
        event_ids = [str(e).split(":", 1)[-1] for e in item.get("eventids") or []]
        if event_ids:
            lesson["homework_ids"] = [f"subid:{e}" for e in event_ids]
        lessons.append(lesson)

    lessons.sort(key=lambda l: (l["date"], l["start_time"] or "", l["cancelled"]))
    return lessons


def group_by_date(lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    days: dict[str, list[dict[str, Any]]] = {}
    for lesson in lessons:
        days.setdefault(lesson["date"], []).append(
            {k: v for k, v in lesson.items() if k != "date"}
        )
    return [
        {"date": day, "weekday": date.fromisoformat(day).strftime("%A"), "lessons": entries}
        for day, entries in sorted(days.items())
    ]


def bell_schedule(periods: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {"period": p.get("short") or p.get("id"), "start_time": p.get("starttime"),
         "end_time": p.get("endtime")}
        for p in periods or []
        if isinstance(p, dict)
    ]


# -- timeline ------------------------------------------------------------------


def attachments(data: Any, base_url: str) -> list[dict[str, str]]:
    raw = data.get("attachements") if isinstance(data, dict) else None
    if isinstance(raw, dict):
        return [
            {"name": name, "url": url if url.startswith("http") else base_url + url}
            for url, name in raw.items()
        ]
    return []


def parse_timeline_item(
    item: dict[str, Any], user_props: dict[str, Any], tz: tzinfo, base_url: str
) -> dict[str, Any]:
    data = loads(item.get("data"))
    data = data if isinstance(data, dict) else {}
    props = user_props.get(str(item.get("timelineid"))) or {}
    props = props if isinstance(props, dict) else {}

    code = item.get("typ")
    text = data.get("messageContent") or item.get("text") or data.get("nazov") or ""
    entry: dict[str, Any] = {
        "id": str(item.get("timelineid")),
        "type": type_name(code),
        "type_code": code,
        "timestamp": local_timestamp(item.get("timestamp"), tz),
        "created_at": local_timestamp(item.get("cas_pridania"), tz),
        "author": item.get("vlastnik_meno") or None,
        "recipient": item.get("user_meno") or None,
        "text": text.strip(),
    }
    if item.get("reakcia_na"):
        entry["reply_to"] = str(item["reakcia_na"])
    replies = int(item.get("pocet_reakcii") or 0)
    if replies:
        entry["reply_count"] = replies
    if event_date := date_part(item.get("cas_udalosti")):
        entry["event_date"] = event_date
    if files := attachments(data, base_url):
        entry["attachments"] = files
    if data.get("receipt") in ("1", 1, True):
        entry["confirmation_requested"] = True
    if props.get("doneMaxCas"):
        entry["done_at"] = local_timestamp(props["doneMaxCas"], tz)
    if props.get("starred") == "1":
        entry["starred"] = True
    return entry


def is_visible(item: dict[str, Any], include_system: bool) -> bool:
    code = item.get("typ") or ""
    if item.get("removed") == "1" or code.startswith(HELPER_PREFIX):
        return False
    return include_system or code not in SYSTEM_TYPES


def belongs_to_student(item: dict[str, Any], student_id: str, child_groups: dict[str, list[str]]) -> bool:
    """False if a parent's timeline item is addressed only to a different child.

    A parent's timeline mixes every child at the school. Items addressed to a
    group that only another child is in (their class, their courses, the child
    themself) are dropped; anything shared or addressed to the parent stays.
    """
    if len(child_groups) < 2:
        return True
    target = item.get("user") or ""
    if _addressed_to(target, str(student_id), child_groups):
        return True
    return not any(
        _addressed_to(target, str(other), child_groups)
        for other in child_groups
        if str(other) != str(student_id)
    )


SHARED_GROUPS = frozenset({"*", "Student*", "StudentOnly*", "Rodic*"})


def _addressed_to(target: str, student_id: str, child_groups: dict[str, list[str]]) -> bool:
    """True if ``target`` is a group only this child's side of the family is in."""
    if target in SHARED_GROUPS:
        return False
    if target in (child_groups.get(student_id) or []):
        return True
    # The parent-of-this-child groups: StudRodic<id> and RStud-<parent>@<id>.
    return target == f"StudRodic{student_id}" or (
        target.startswith("RStud") and target.endswith(f"@{student_id}")
    )


def thread_messages(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Nest replies under the message they answer, newest thread first."""
    by_id = {e["id"]: {**e, "replies": []} for e in entries}
    roots = []
    for entry in by_id.values():
        parent = by_id.get(entry.get("reply_to", ""))
        if parent is not None:
            parent["replies"].append(entry)
        else:
            roots.append(entry)
    for entry in by_id.values():
        entry["replies"].sort(key=lambda r: r.get("timestamp") or "")
        if not entry["replies"]:
            del entry["replies"]
    roots.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    return roots


# -- homework and exams ----------------------------------------------------------


def homework_kind(code: str | None) -> str:
    if code in (None, "", "hw", "homework"):
        return "homework"
    if code in EXAM_TYPES:
        return "exam"
    return "event"


def parse_homeworks(
    homeworks: list[dict[str, Any]],
    timeline_items: list[dict[str, Any]],
    user_props: dict[str, Any],
    directory: Directory,
    base_url: str,
) -> list[dict[str, Any]]:
    """Homework and announced exams from the timeline's ``homeworks`` feed.

    The feed has the assignment itself; whether it was ticked off, and any
    attachments, live on the matching ``homework`` timeline item.
    """
    by_ineid: dict[str, dict[str, Any]] = {}
    for item in timeline_items or []:
        if item.get("typ") == "homework" and item.get("ineid"):
            by_ineid[str(item["ineid"])] = item

    out = []
    for hw in homeworks or []:
        hw_id = str(hw.get("homeworkid") or "")
        code = hw.get("typ")
        et = hw.get("et") if isinstance(hw.get("et"), dict) else {}
        kind = homework_kind(code)
        entry: dict[str, Any] = {
            "id": hw_id,
            "kind": kind,
            "type": et.get("name") or directory.event_type(code)
            or ("Homework" if kind == "homework" else type_name(code)),
            "title": (hw.get("name") or "").strip(),
            "details": (hw.get("details") or "").strip() or None,
            "subject": hw.get("predmet_meno") or directory.subject_name(hw.get("subjectid")),
            "course": hw.get("kurz_meno") or None,
            "teacher": hw.get("ucitel_meno") or hw.get("autor_meno") or None,
            "assigned_on": date_part(hw.get("datefrom")) or date_part(hw.get("datecreated")),
            "due_date": date_part(hw.get("dateto")),
            "done": False,
        }
        if hw.get("time"):
            entry["time"] = hw["time"]
        if hw.get("nextlesson"):
            entry["due_next_lesson"] = True

        item = by_ineid.get(hw_id.removeprefix("subid:"))
        if item is not None:
            entry["timeline_id"] = str(item.get("timelineid"))
            props = user_props.get(str(item.get("timelineid"))) or {}
            if isinstance(props, dict) and props.get("doneMaxCas"):
                entry["done"] = True
            data = loads(item.get("data"))
            if files := attachments(data, base_url):
                entry["attachments"] = files
            if isinstance(data, dict) and not entry["details"]:
                entry["details"] = (data.get("popis") or "").strip() or None
        out.append(entry)

    out.sort(key=lambda h: (h["due_date"] or "9999", h["subject"] or ""))
    return out


# -- school events -------------------------------------------------------------------


def parse_event(item: dict[str, Any], directory: Directory) -> dict[str, Any] | None:
    """A calendar event (trip, exam, holiday, ...) from an ``event`` timeline item."""
    data = loads(item.get("data"))
    if not isinstance(data, dict):
        return None
    start = date_part(data.get("date") or data.get("datefrom") or item.get("cas_udalosti"))
    if start is None:
        return None
    et = data.get("et") if isinstance(data.get("et"), dict) else {}
    code = data.get("typ")
    event = {
        "id": str(item.get("timelineid")),
        "title": (data.get("name") or item.get("text") or "").strip(),
        "type": directory.event_type(code) or et.get("name") or type_name(code),
        "type_code": code,
        "start_date": start,
        "end_date": date_part(data.get("dateto")) or start,
        "all_day": bool(data.get("allday")),
        "details": (data.get("details") or "").strip() or None,
        "subject": directory.subject_name(data.get("subjectid") or data.get("predmetid")),
        "teachers": _clean(directory.teacher(t) for t in data.get("teacherids") or []),
        "classes": _clean(directory.class_name(c) for c in data.get("classids") or []),
        "organiser": item.get("vlastnik_meno") or None,
    }
    if data.get("time"):
        event["time"] = data["time"]
    if event["type_code"] in EXAM_TYPES:
        event["kind"] = "exam"
    return event


# -- absences ---------------------------------------------------------------------


def parse_absence(item: dict[str, Any], directory: Directory, tz: tzinfo) -> dict[str, Any] | None:
    data = loads(item.get("data"))
    data = data if isinstance(data, dict) else {}
    code = item.get("typ")
    if code == "student_absent":
        periods = {
            key[2:]: value for key, value in sorted(data.items()) if key.startswith("p_")
        }
        return {
            "kind": "absence",
            "date": date_part(data.get("cas_udalosti") or item.get("cas_udalosti")),
            "periods": periods,
            "excused": bool(data.get("isExcused")),
            "recorded_by": item.get("vlastnik_meno") or None,
            "recorded_at": local_timestamp(item.get("cas_pridania"), tz),
            "text": item.get("text") or None,
        }
    if code in ("ospravedlnenka", "ospravedlnenka_reminder"):
        return {
            "kind": "excuse" if code == "ospravedlnenka" else "excuse_reminder",
            "date": date_part(data.get("datefrom") or item.get("cas_udalosti")),
            "end_date": date_part(data.get("dateto")),
            "reason": directory.absence_type(data.get("studentabsent_typeid")),
            "note": (data.get("note") or "").strip() or None,
            "recorded_by": item.get("vlastnik_meno") or None,
            "recorded_at": local_timestamp(item.get("cas_pridania"), tz),
            "text": item.get("text") or None,
        }
    return None


# -- meals -------------------------------------------------------------------------


def parse_meals(day: dict[str, Any] | None, add_info: dict[str, Any] | None) -> list[dict[str, Any]]:
    """One day of the canteen menu (``novyListok[<date>]``)."""
    legend = _allergen_legend(add_info)
    meals = []
    for slot, meal in sorted((day or {}).items()):
        if not isinstance(meal, dict) or meal.get("isCooking") is False:
            continue
        names = meal.get("nazvyMenu") if isinstance(meal.get("nazvyMenu"), dict) else {}
        menus = []
        raw_menus = meal.get("menus") if isinstance(meal.get("menus"), dict) else {}
        for number, menu in sorted(raw_menus.items(), key=lambda kv: _int(kv[0])):
            label = names.get(number) or {}
            menus.append({
                "menu": menu.get("skratkaMenu") or label.get("skratka") or number,
                "label": menu.get("nazovMenu") or label.get("nazov"),
                "items": [_meal_row(r, legend) for r in menu.get("rows") or [] if r],
            })
        if not menus and meal.get("rows"):
            menus.append({"menu": "A", "label": None,
                          "items": [_meal_row(r, legend) for r in meal["rows"] if r]})

        record = meal.get("evidencia") if isinstance(meal.get("evidencia"), dict) else {}
        state = record.get("stav")
        choosable = _choosable_menus(meal)
        meals.append({
            "meal": MEAL_SLOTS.get(str(slot), f"meal_{slot}"),
            "slot": str(slot),
            "served_from": meal.get("vydaj_od") or None,
            "served_to": meal.get("vydaj_do") or None,
            "menus": menus,
            "ordered": state == "V",
            "ordered_menu": record.get("obj") if state == "V" else None,
            "order_state": state,
            "can_choose": bool(meal.get("isChoosable")) and bool(choosable),
            "choosable_menus": [_menu_letter(n, names) for n in choosable],
            "order_by": meal.get("prihlas_do") or None,
            "cancel_by": meal.get("odhlas_do") or None,
            "change_by": meal.get("zmen_do") or None,
        })
    return meals


def meal_credit(add_info: dict[str, Any] | None) -> float | None:
    info = add_info or {}
    for value in (info.get("kredit"), (info.get("strRow") or {}).get("kredit")):
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def boarder_id(add_info: dict[str, Any] | None) -> str | None:
    info = add_info or {}
    return info.get("stravnikid") or (info.get("strRow") or {}).get("stravnikid")


def _choosable_menus(meal: dict[str, Any]) -> list[str]:
    menus = meal.get("choosableMenus")
    if isinstance(menus, dict) and menus:
        return sorted((k for k, v in menus.items() if v), key=_int)
    # Some canteens only list choices per serving type.
    for serving in (meal.get("typVydaj") or {}).values():
        if isinstance(serving, dict) and isinstance(serving.get("choosableMenus"), dict):
            return sorted((k for k, v in serving["choosableMenus"].items() if v), key=_int)
    return []


def _menu_letter(number: str, names: dict[str, Any]) -> str:
    label = names.get(number) or {}
    if label.get("skratka"):
        return label["skratka"]
    n = _int(number)
    return "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[n - 1] if 1 <= n <= 26 else number


def _meal_row(row: dict[str, Any], legend: dict[str, str]) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": " ".join((row.get("nazov") or "").split())}
    codes = [c.strip() for c in (row.get("alergenyStr") or "").split(",") if c.strip()]
    if codes:
        entry["allergens"] = [legend.get(c, c) for c in codes]
    if row.get("hmotnostiStr"):
        entry["portion"] = row["hmotnostiStr"]
    return entry


def _allergen_legend(add_info: dict[str, Any] | None) -> dict[str, str]:
    raw = (add_info or {}).get("alergenyIDS")
    if not isinstance(raw, dict):
        return {}
    legend = {}
    for row in raw.values():
        if isinstance(row, dict) and row.get("ozn") and row.get("nazov"):
            legend[str(row["ozn"])] = f"{row['ozn']} {row['nazov']}"
    return legend


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# -- grades ------------------------------------------------------------------------


def grade_to_dict(grade: Any, directory: Directory, tz: tzinfo) -> dict[str, Any]:
    """An ``edupage_api`` ``EduGrade`` as a dict."""
    value = grade.grade_n
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    entry: dict[str, Any] = {
        # The assessment (a column in the mark book); several marks can share one.
        "assessment_id": str(grade.event_id),
        "date": grade.date.replace(tzinfo=tz).isoformat() if grade.date else None,
        "subject": directory.subject_name(grade.subject_id) or grade.subject_name,
        "subject_short": (directory.subject(grade.subject_id) or {}).get("short") or grade.subject_name,
        "title": grade.title,
        "grade": value,
        "comment": grade.comment or None,
        "teacher": getattr(grade.teacher, "name", None),
    }
    if grade.max_points is not None:
        entry["max_points"] = grade.max_points
    if grade.percent is not None and grade.percent != float("inf"):
        entry["percent"] = grade.percent
    if grade.importance is not None:
        entry["weight"] = grade.importance
    if grade.class_grade_avg is not None:
        entry["class_average"] = grade.class_grade_avg
    if grade.verbal:
        entry["verbal"] = True
    return entry


def summarise_grades(grades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-subject count and weighted average of the numeric marks (1 is best)."""
    subjects: dict[str, dict[str, Any]] = {}
    for g in grades:
        s = subjects.setdefault(g["subject"] or "?", {"subject": g["subject"], "count": 0,
                                                      "_sum": 0.0, "_weight": 0.0, "percent": []})
        s["count"] += 1
        if "max_points" in g:
            if "percent" in g:
                s["percent"].append(g["percent"])
            continue
        if isinstance(g["grade"], (int, float)):
            w = g.get("weight") or 1.0
            s["_sum"] += g["grade"] * w
            s["_weight"] += w

    out = []
    for s in subjects.values():
        row = {"subject": s["subject"], "count": s["count"]}
        if s["_weight"]:
            row["average"] = round(s["_sum"] / s["_weight"], 2)
        if s["percent"]:
            row["average_percent"] = round(sum(s["percent"]) / len(s["percent"]), 1)
        out.append(row)
    out.sort(key=lambda r: r["subject"] or "")
    return out
