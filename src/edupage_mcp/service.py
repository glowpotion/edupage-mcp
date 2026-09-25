"""What each MCP tool does, as plain synchronous methods returning dicts."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .client import EdupageClient, EdupageToolError, Student
from .config import Config
from .parsing import (
    MESSAGE_TYPES,
    Directory,
    belongs_to_student,
    bell_schedule,
    boarder_id,
    fold,
    grade_to_dict,
    group_by_date,
    is_visible,
    meal_credit,
    parse_absence,
    parse_event,
    parse_homeworks,
    parse_meals,
    parse_timeline_item,
    parse_timetable,
    summarise_grades,
    thread_messages,
)

MAX_TIMETABLE_DAYS = 31
MAX_RANGE_DAYS = 400
MAX_LIMIT = 200
# edupage_api's substitution actions; a (from, to) tuple means a moved lesson.
CHANGE_ACTIONS = {"add": "added", "change": "changed", "remove": "cancelled"}


class EdupageService:
    def __init__(self, config: Config, client: EdupageClient | None = None):
        self.config = config
        self.client = client or EdupageClient(config)

    # -- dates ----------------------------------------------------------------

    def today(self) -> date:
        return datetime.now(self.config.timezone).date()

    def parse_date(self, value: str | None, field: str, default: date) -> date:
        if value is None or not str(value).strip():
            return default
        raw = str(value).strip().lower()
        relative = {"today": 0, "tomorrow": 1, "yesterday": -1}
        if raw in relative:
            return self.today() + timedelta(days=relative[raw])
        try:
            return date.fromisoformat(raw[:10])
        except ValueError as exc:
            raise EdupageToolError(
                f"{field}={value!r} is not a date. Use YYYY-MM-DD, 'today', 'tomorrow' "
                "or 'yesterday'."
            ) from exc

    def _range(
        self, start: str | None, end: str | None, default_start: date, default_days: int,
        max_days: int = MAX_RANGE_DAYS,
    ) -> tuple[date, date]:
        d0 = self.parse_date(start, "start_date", default_start)
        d1 = self.parse_date(end, "end_date", d0 + timedelta(days=default_days - 1))
        if d1 < d0:
            raise EdupageToolError("end_date is before start_date.")
        if (d1 - d0).days + 1 > max_days:
            raise EdupageToolError(f"The range can be at most {max_days} days.")
        return d0, d1

    def school_year_start(self) -> date:
        t = self.today()
        return date(t.year if t.month >= 9 else t.year - 1, 9, 1)

    # -- account ----------------------------------------------------------------

    def get_account(self) -> dict[str, Any]:
        account = self.client.account()
        return {"status": "success", "read_only": self.config.read_only, **account}

    def complete_login(self, school: str | None, code: str | None) -> dict[str, Any]:
        return self.client.complete_login(school, code)

    # -- timetable ------------------------------------------------------------------

    def get_timetable(self, student: str | None, day: str | None, days: int) -> dict[str, Any]:
        s = self.client.resolve_student(student)
        if not 1 <= days <= MAX_TIMETABLE_DAYS:
            raise EdupageToolError(f"`days` must be between 1 and {MAX_TIMETABLE_DAYS}.")
        start = self.parse_date(day, "date", self.today())
        end = start + timedelta(days=days - 1)
        lessons = parse_timetable(self.client.timetable(s, start, end), self.client.directory(s.school))
        return {
            "status": "success",
            "student": s.as_dict(),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "days": group_by_date(lessons),
            **({"note": "No lessons in this range (weekend or holiday?)."} if not lessons else {}),
        }

    def _siblings(self, s: Student) -> list[Student]:
        return [x for x in self.client.students() if x.school == s.school and x.id != s.id]

    # -- timeline-based ------------------------------------------------------------------

    def _timeline(self, s: Student, since: date) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        data = self.client.timeline(s.school, since)
        child_groups = data.get("childGroups") or {}
        items = [
            i for i in data.get("timelineItems") or []
            if belongs_to_student(i, s.id, child_groups)
        ]
        return data, items

    def get_homework(
        self, student: str | None, start: str | None, end: str | None, kind: str,
        include_done: bool,
    ) -> dict[str, Any]:
        s = self.client.resolve_student(student)
        d0, d1 = self._range(start, end, self.today(), 14)
        if kind not in ("all", "homework", "exam"):
            raise EdupageToolError("`kind` must be 'all', 'homework' or 'exam'.")
        data, items = self._timeline(s, d0 - timedelta(days=45))
        directory = Directory(data.get("dbi") or self.client.directory(s.school).dbi)
        hws = parse_homeworks(
            _for_student(data.get("homeworks") or [], s, self._siblings(s)),
            items,
            data.get("timelineUserProps") or {},
            directory,
            f"https://{s.school}.edupage.org",
        )
        selected = [
            h for h in hws
            if h["due_date"] and d0.isoformat() <= h["due_date"] <= d1.isoformat()
            and (kind == "all" or h["kind"] == kind)
            and (include_done or not h["done"])
        ]
        return {
            "status": "success",
            "student": s.as_dict(),
            "start_date": d0.isoformat(),
            "end_date": d1.isoformat(),
            "count": len(selected),
            "homework": selected,
        }

    def get_notifications(
        self, student: str | None, since: str | None, types: list[str] | None,
        search: str | None, limit: int, include_system: bool,
    ) -> dict[str, Any]:
        s = self.client.resolve_student(student)
        start = self.parse_date(since, "since", self.today() - timedelta(days=7))
        limit = _limit(limit)
        data, items = self._timeline(s, start)
        wanted = {t.strip().lower() for t in types or [] if t.strip()}
        entries = []
        for item in items:
            if not is_visible(item, include_system or bool(wanted)):
                continue
            entry = parse_timeline_item(
                item, data.get("timelineUserProps") or {}, self.config.timezone,
                f"https://{s.school}.edupage.org",
            )
            if (entry["timestamp"] or "")[:10] < start.isoformat():
                continue
            if wanted and not wanted & {entry["type"], entry["type_code"]}:
                continue
            if search and not _matches(search, entry):
                continue
            entries.append(entry)
        entries.sort(key=lambda e: e["timestamp"] or "", reverse=True)
        return {
            "status": "success",
            "student": s.as_dict(),
            "since": start.isoformat(),
            "count": min(len(entries), limit),
            "total": len(entries),
            "notifications": entries[:limit],
        }

    def get_messages(
        self, student: str | None, since: str | None, search: str | None, limit: int,
    ) -> dict[str, Any]:
        s = self.client.resolve_student(student)
        start = self.parse_date(since, "since", self.today() - timedelta(days=14))
        limit = _limit(limit)
        data, items = self._timeline(s, start)
        props = data.get("timelineUserProps") or {}
        base = f"https://{s.school}.edupage.org"
        entries = [
            parse_timeline_item(i, props, self.config.timezone, base)
            for i in items
            if i.get("typ") in MESSAGE_TYPES and i.get("removed") != "1"
        ]
        entries = [e for e in entries if (e["timestamp"] or "")[:10] >= start.isoformat()]
        threads = thread_messages(entries)
        if search:
            threads = [
                t for t in threads
                if _matches(search, t) or any(_matches(search, r) for r in t.get("replies", []))
            ]
        return {
            "status": "success",
            "student": s.as_dict(),
            "since": start.isoformat(),
            "count": min(len(threads), limit),
            "total": len(threads),
            "messages": threads[:limit],
        }

    def get_events(self, student: str | None, start: str | None, end: str | None) -> dict[str, Any]:
        s = self.client.resolve_student(student)
        d0, d1 = self._range(start, end, self.today(), 30)
        data, items = self._timeline(s, min(d0, self.today()) - timedelta(days=120))
        directory = Directory(data.get("dbi") or self.client.directory(s.school).dbi)
        events: dict[tuple, dict[str, Any]] = {}
        for item in items:
            if item.get("typ") != "event" or item.get("removed") == "1":
                continue
            event = parse_event(item, directory)
            if event is None or event["end_date"] < d0.isoformat() or event["start_date"] > d1.isoformat():
                continue
            # An edited event is posted again; keep the newest copy.
            key = (event["title"], event["start_date"], event["type_code"])
            if key not in events or int(event["id"]) > int(events[key]["id"]):
                events[key] = event
        selected = sorted(events.values(), key=lambda e: (e["start_date"], e["title"]))
        return {
            "status": "success",
            "student": s.as_dict(),
            "start_date": d0.isoformat(),
            "end_date": d1.isoformat(),
            "count": len(selected),
            "events": selected,
        }

    def get_absences(self, student: str | None, since: str | None) -> dict[str, Any]:
        s = self.client.resolve_student(student)
        start = self.parse_date(since, "since", self.school_year_start())
        data, items = self._timeline(s, start)
        directory = Directory(data.get("dbi") or self.client.directory(s.school).dbi)
        records = [
            r for i in items
            if i.get("removed") != "1" and (r := parse_absence(i, directory, self.config.timezone))
            and (r["date"] or "") >= start.isoformat()
        ]
        records.sort(key=lambda r: r["date"] or "", reverse=True)
        absences = [r for r in records if r["kind"] == "absence"]
        return {
            "status": "success",
            "student": s.as_dict(),
            "since": start.isoformat(),
            "days_absent": len({r["date"] for r in absences}),
            "unexcused_days": len({r["date"] for r in absences if not r["excused"]}),
            "records": records,
            "note": "Built from the absence and excuse notices on the timeline; the "
                    "attendance page in EduPage is authoritative.",
        }

    # -- grades --------------------------------------------------------------------------

    def get_grades(
        self, student: str | None, subject: str | None, school_year: int | None,
        term: str | None, since: str | None,
    ) -> dict[str, Any]:
        s = self.client.resolve_student(student)
        raw, directory = self.client.grades(s, school_year, term)
        grades = [grade_to_dict(g, directory, self.config.timezone) for g in raw]
        if subject:
            q = fold(subject)
            exact = [g for g in grades if q in (fold(g["subject"] or ""), fold(g["subject_short"] or ""))]
            grades = exact or [g for g in grades if q in fold(g["subject"] or "")]
        if since:
            start = self.parse_date(since, "since", self.today())
            grades = [g for g in grades if (g["date"] or "")[:10] >= start.isoformat()]
        grades.sort(key=lambda g: g["date"] or "", reverse=True)
        return {
            "status": "success",
            "student": s.as_dict(),
            "count": len(grades),
            "summary": summarise_grades(grades),
            "grades": grades,
            "note": "Marks run 1 (best) to 5. `average` is weighted by each mark's weight; "
                    "points-based marks are averaged separately as `average_percent`.",
        }

    # -- school ------------------------------------------------------------------------------

    def get_substitutions(
        self, target: str | None, day: str | None, all_classes: bool,
    ) -> dict[str, Any]:
        d = self.parse_date(day, "date", self.today())
        student = None
        if target and target.strip().lower().removesuffix(".edupage.org") in self.client.schools():
            school = target.strip().lower().removesuffix(".edupage.org")
        else:
            student = self.client.resolve_student(target)
            school = student.school
        raw = self.client.substitutions(school, d)
        changes = [
            {
                "class": c.change_class,
                "period": c.lesson_n if not isinstance(c.lesson_n, tuple) else f"{c.lesson_n[0]}-{c.lesson_n[1]}",
                "action": CHANGE_ACTIONS.get(getattr(c.action, "value", None), "changed"),
                "description": c.title,
            }
            for c in raw["changes"]
        ]
        if student is not None and not all_classes and student.class_name:
            changes = [c for c in changes if fold(c["class"]) == fold(student.class_name)]
        result = {
            "status": "success",
            "school": school,
            "date": d.isoformat(),
            "class": None if all_classes or student is None else student.class_name,
            "changes": changes,
            "missing_teachers": [getattr(t, "name", str(t)) for t in raw["missing_teachers"]],
        }
        if "missing_teachers_error" in raw:
            result["warnings"] = [f"Could not read the missing teachers: {raw['missing_teachers_error']}"]
        return result

    def get_school_info(self, target: str | None) -> dict[str, Any]:
        school = self.client.resolve_school(target)
        info = self.client.run(school, lambda e: e.data)
        directory = Directory(info.get("dbi"))
        students = [s for s in self.client.students() if s.school == school]
        classes = []
        for s in students:
            row = directory.class_row(s.class_id) or {}
            classes.append({
                "student": s.name,
                "class": s.class_name,
                "class_teachers": [
                    n for n in (directory.teacher(row.get(k)) for k in ("teacherid", "teacher2id", "teacher3id")) if n
                ],
                "home_classroom": directory.classroom(row.get("classroomid")),
                "classmates": len([
                    r for r in directory.rows("students") if r.get("classid") == s.class_id
                ]),
            })
        return {
            "status": "success",
            "school": school,
            "url": f"https://{school}.edupage.org",
            "school_email": info.get("school_email") or None,
            "bell_schedule": bell_schedule((info.get("dbi") or {}).get("periods") or info.get("zvonenia")),
            "classes": classes,
            "counts": {
                g: len(directory.rows(g)) for g in ("teachers", "classes", "subjects", "classrooms")
            },
        }

    def list_teachers(self, target: str | None, query: str | None) -> dict[str, Any]:
        school = self.client.resolve_school(target)
        directory = self.client.directory(school)
        q = fold(query or "")
        teachers = []
        for row in directory.rows("teachers"):
            name = " ".join(p for p in (row.get("firstname"), row.get("lastname")) if p)
            full = " ".join(p for p in (row.get("nameprefix"), name, row.get("namesuffix")) if p).strip()
            if row.get("isOut") or (q and q not in fold(full) and q != fold(row.get("short") or "")):
                continue
            teachers.append({
                "name": name,
                "full_name": full,
                "short": row.get("short") or None,
                "recipient_id": f"Ucitel{row.get('id')}",
                "classroom": directory.classroom(row.get("classroomid")),
            })
        teachers.sort(key=lambda t: fold(t["name"].split(" ")[-1]))
        return {"status": "success", "school": school, "count": len(teachers), "teachers": teachers}

    # -- meals ------------------------------------------------------------------------------------

    def get_meals(self, student: str | None, day: str | None, days: int) -> dict[str, Any]:
        s = self.client.resolve_student(student)
        if not 1 <= days <= 14:
            raise EdupageToolError("`days` must be between 1 and 14.")
        start = self.parse_date(day, "date", self.today())
        end = start + timedelta(days=days - 1)
        out_days: list[dict[str, Any]] = []
        add_info, seen = None, set()
        cursor, covered = start, start - timedelta(days=1)
        # Each page is one week, Monday to Friday; weekends never appear.
        for _ in range(days // 5 + 3):
            if covered >= end:
                break
            data = self.client.meal_data(s, cursor)
            add_info = data.get("addInfo") or add_info
            page_days = sorted(k for k in data if k[:4].isdigit())
            if not page_days:
                break  # nothing published from here on
            for key in page_days:
                if start.isoformat() <= key <= end.isoformat() and key not in seen:
                    seen.add(key)
                    out_days.append({"date": key, "weekday": date.fromisoformat(key).strftime("%A"),
                                     "meals": parse_meals(data.get(key), add_info)})
            last = date.fromisoformat(page_days[-1])
            cursor = max(cursor, last + timedelta(days=1))
            if cursor.weekday() >= 5:  # skip the weekend
                cursor += timedelta(days=7 - cursor.weekday())
            covered = cursor - timedelta(days=1)
        out_days.sort(key=lambda d: d["date"])
        return {
            "status": "success",
            "student": s.as_dict(),
            "credit": meal_credit(add_info),
            "days": [d for d in out_days if d["meals"]] or [],
            **({"note": "No canteen menu published for these dates."} if not any(d["meals"] for d in out_days) else {}),
        }

    def order_meal(self, student: str | None, day: str, meal: str, menu: str) -> dict[str, Any]:
        self._writable()
        s = self.client.resolve_student(student)
        d = self.parse_date(day, "date", self.today())
        data = self.client.meal_data(s, d)
        meals = parse_meals(data.get(d.isoformat()), data.get("addInfo"))
        target = next((m for m in meals if m["meal"] == meal.strip().lower() or m["slot"] == meal.strip()), None)
        if target is None:
            raise EdupageToolError(
                f"No {meal!r} on {d.isoformat()}. Meals that day: "
                + (", ".join(m["meal"] for m in meals) or "none") + "."
            )
        boarder = boarder_id(data.get("addInfo"))
        if not boarder:
            raise EdupageToolError(f"{s.name} is not registered with the canteen.")
        choice = menu.strip().upper()
        if choice in ("CANCEL", "NONE", "OFF", "X", "AX"):
            code = "AX"
        elif choice in target["choosable_menus"]:
            code = choice
        else:
            raise EdupageToolError(
                f"Menu {menu!r} cannot be chosen for {target['meal']} on {d.isoformat()}. "
                f"Choosable: {', '.join(target['choosable_menus']) or 'none'}; or 'cancel'."
            )
        self.client.order_meal(s, d, target["slot"], code, boarder)
        after = parse_meals(self.client.meal_data(s, d).get(d.isoformat()), data.get("addInfo"))
        updated = next((m for m in after if m["slot"] == target["slot"]), None)
        return {
            "status": "success",
            "student": s.as_dict(),
            "date": d.isoformat(),
            "meal": target["meal"],
            "requested": "cancel" if code == "AX" else code,
            "now": {k: updated[k] for k in ("ordered", "ordered_menu", "order_state")} if updated else None,
        }

    # -- messages --------------------------------------------------------------------------------------

    def send_message(self, target: str | None, recipients: list[str], text: str) -> dict[str, Any]:
        self._writable()
        if not text or not text.strip():
            raise EdupageToolError("The message text is empty.")
        if not recipients:
            raise EdupageToolError("Give at least one recipient (a teacher's name or recipient_id).")
        school = self.client.resolve_school(target)
        teachers = self.list_teachers(school, None)["teachers"]
        ids, names = [], []
        for r in recipients:
            raw = r.strip()
            if raw.startswith(("Ucitel", "Rodic", "Student")) and raw[len(raw.rstrip("-0123456789")):]:
                ids.append(raw)
                names.append(raw)
                continue
            q = fold(raw)
            exact = [t for t in teachers if q in (fold(t["name"]), fold(t["full_name"]), fold(t["short"] or ""))]
            matches = exact or [t for t in teachers if q in fold(t["full_name"])]
            if len(matches) != 1:
                options = ", ".join(t["name"] for t in matches[:10])
                raise EdupageToolError(
                    f"Recipient {r!r} matches {len(matches)} teachers at {school}"
                    + (f" ({options})" if options else "")
                    + ". Use list_teachers to find the exact name."
                )
            ids.append(matches[0]["recipient_id"])
            names.append(matches[0]["name"])
        timeline_id = self.client.send_message(school, ids, text.strip())
        return {"status": "success", "school": school, "sent_to": names, "message_id": str(timeline_id)}

    def _writable(self) -> None:
        if self.config.read_only:
            raise EdupageToolError("This server is read-only (EDUPAGE_READ_ONLY is set).")

    # -- briefing ------------------------------------------------------------------------------------------

    def get_briefing(self, student: str | None, days_ahead: int) -> dict[str, Any]:
        if not 1 <= days_ahead <= 14:
            raise EdupageToolError("`days_ahead` must be between 1 and 14.")
        targets = [self.client.resolve_student(student)] if student else self.client.students()
        today = self.today()
        out = [self._brief(s, today, days_ahead) for s in targets]
        return {"status": "success", "date": today.isoformat(), "days_ahead": days_ahead, "students": out}

    def _brief(self, s: Student, today: date, days_ahead: int) -> dict[str, Any]:
        end = (today + timedelta(days=days_ahead)).isoformat()
        section: dict[str, Any] = {"student": s.as_dict()}
        warnings = []

        def attempt(name: str, fn):
            try:
                return fn()
            except EdupageToolError as exc:
                warnings.append(f"{name}: {exc}")
            except Exception as exc:  # noqa: BLE001 - one broken section should not sink the rest
                warnings.append(f"{name}: {type(exc).__name__}: {exc}")
            return None

        tt = attempt("timetable", lambda: self.get_timetable(s.id, today.isoformat(), 1))
        if tt and tt["days"]:
            section["today"] = [
                {k: l[k] for k in ("period", "start_time", "end_time", "subject", "classrooms",
                                   "changed", "cancelled") if k in l}
                for l in tt["days"][0]["lessons"]
            ]
        else:
            section["today"] = []
        hw = attempt("homework", lambda: self.get_homework(s.id, today.isoformat(), end, "all", False))
        if hw:
            section["homework_due"] = [h for h in hw["homework"] if h["kind"] == "homework"]
            section["exams"] = [h for h in hw["homework"] if h["kind"] != "homework"]
        msgs = attempt("messages", lambda: self.get_messages(
            s.id, (today - timedelta(days=2)).isoformat(), None, 20))
        if msgs:
            section["recent_messages"] = [
                {k: m.get(k) for k in ("id", "timestamp", "author", "recipient", "text", "attachments",
                                       "confirmation_requested") if m.get(k) is not None}
                for m in msgs["messages"]
            ]
        notes = attempt("notifications", lambda: self.get_notifications(
            s.id, (today - timedelta(days=2)).isoformat(), None, None, 50, False))
        if notes:
            section["other_notices"] = [
                {k: n.get(k) for k in ("timestamp", "type", "author", "text") if n.get(k)}
                for n in notes["notifications"]
                if n["type_code"] not in MESSAGE_TYPES and n["type_code"] != "homework"
            ]
        grades = attempt("grades", lambda: self.get_grades(
            s.id, None, None, None, (today - timedelta(days=7)).isoformat()))
        if grades:
            section["new_grades"] = [
                {k: g.get(k) for k in ("date", "subject", "title", "grade", "max_points", "comment") if g.get(k) is not None}
                for g in grades["grades"]
            ]
        meals = attempt("meals", lambda: self.get_meals(s.id, today.isoformat(), 1))
        if meals and meals["days"]:
            section["meals_today"] = [
                {"meal": m["meal"], "ordered": m["ordered"], "ordered_menu": m["ordered_menu"],
                 "menus": [{"menu": x["menu"], "items": [i["name"] for i in x["items"]]} for x in m["menus"]]}
                for m in meals["days"][0]["meals"]
            ]
        if warnings:
            section["warnings"] = warnings
        return section


def _for_student(
    homeworks: list[dict[str, Any]], s: Student, siblings: list[Student]
) -> list[dict[str, Any]]:
    """Drop homework set only for a sibling's class (a parent's feed mixes children).

    Only applied when there are siblings at the same school: a student can be
    in several class groups, so homework is never filtered on class alone.
    """
    theirs = {str(x.class_id) for x in siblings if x.class_id} - {str(s.class_id)}
    if not theirs:
        return homeworks
    return [
        h for h in homeworks
        if not {str(c) for c in h.get("classids") or []} <= theirs
        or not h.get("classids")
    ]


def _matches(search: str, entry: dict[str, Any]) -> bool:
    q = fold(search)
    return any(q in fold(str(entry.get(k) or "")) for k in ("text", "author", "recipient", "type"))


def _limit(limit: int) -> int:
    if not 1 <= limit <= MAX_LIMIT:
        raise EdupageToolError(f"`limit` must be between 1 and {MAX_LIMIT}.")
    return limit

