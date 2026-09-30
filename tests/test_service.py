from __future__ import annotations

import dataclasses
import json
from datetime import date, timedelta

import pytest
from conftest import FakeEdupage, FakeResponse, dbi, login_data

from edupage_mcp.client import EdupageClient, EdupageToolError
from edupage_mcp.service import EdupageService


@pytest.fixture
def today(service, monkeypatch):
    fixed = date(2026, 9, 25)
    monkeypatch.setattr(service, "today", lambda: fixed)
    return fixed


def _message(tid, ts, text, **kw):
    item = {"timelineid": tid, "typ": "sprava", "timestamp": ts, "cas_pridania": ts,
            "vlastnik_meno": "Anna Nováková", "user_meno": "Rodičia IV.A", "user": "Rodicko401",
            "text": text, "data": "{}", "removed": "0", "reakcia_na": None, "pocet_reakcii": "0"}
    item.update(kw)
    return item


def _timeline(**overrides):
    payload = {
        "timelineItems": [
            _message("10", "2026-09-24 09:00:00", "Trip on Friday"),
            _message("11", "2026-09-24 12:00:00", "Thanks!", reakcia_na="10",
                     vlastnik_meno="A Parent"),
            _message("12", "2026-09-01 09:00:00", "Welcome back"),
            _message("13", "2026-09-23 08:00:00", "Tip of the day", typ="genotif"),
            _message("14", "2026-09-23 10:00:00", "Známka - Matematika: 1", typ="znamka"),
            _message("15", "2026-09-22 10:00:00", "Hidden", typ="h_homework"),
            {"timelineid": "20", "typ": "homework", "ineid": "HW1", "timestamp": "2026-09-20 10:00:00",
             "data": "{}", "removed": "0", "user": "Plan1"},
            {"timelineid": "30", "typ": "event", "timestamp": "2026-09-20 10:00:00",
             "vlastnik_meno": "Office", "removed": "0",
             "data": json.dumps({"typ": "trip", "name": "Zoo", "date": "2026-10-02"})},
            {"timelineid": "31", "typ": "event", "timestamp": "2026-09-21 10:00:00", "removed": "0",
             "data": json.dumps({"typ": "trip", "name": "Zoo", "date": "2026-10-02",
                                 "details": "Bring lunch"})},
            {"timelineid": "40", "typ": "student_absent", "timestamp": "2026-09-09 08:00:00",
             "removed": "0", "data": json.dumps({"p_1": "A", "cas_udalosti": "2026-09-09",
                                                  "isExcused": False})},
        ],
        "homeworks": [
            {"homeworkid": "subid:HW1", "typ": "hw", "name": "Exercise", "dateto": "2026-09-28",
             "classids": ["401"]},
            {"homeworkid": "subid:HW2", "typ": "sexam", "name": "Quiz", "dateto": "2026-09-29",
             "classids": ["401"]},
            {"homeworkid": "subid:HW3", "typ": "hw", "name": "Old", "dateto": "2026-09-10"},
            {"homeworkid": "subid:HW4", "typ": "hw", "name": "Far", "dateto": "2026-12-01"},
        ],
        "timelineUserProps": {"20": {"doneMaxCas": "2026-09-26 18:00:00"}},
        "childGroups": {"501": ["Rodicko401"]},
        "dbi": dbi(),
    }
    payload.update(overrides)
    return payload


def test_dates_accept_words(service, today):
    assert service.parse_date("tomorrow", "date", today) == date(2026, 9, 26)
    assert service.parse_date("2026-10-01T08:00", "date", today) == date(2026, 10, 1)
    assert service.parse_date(None, "date", today) == today
    with pytest.raises(EdupageToolError, match="YYYY-MM-DD"):
        service.parse_date("next week", "date", today)


def test_ranges_are_checked(service, today):
    with pytest.raises(EdupageToolError, match="before start_date"):
        service.get_homework(None, "2026-10-01", "2026-09-01", "all", False)
    with pytest.raises(EdupageToolError, match="at most"):
        service.get_homework(None, "2026-01-01", "2027-06-01", "all", False)


def test_timetable_request(service, today):
    seen = {}

    def tt(json=None, **_):
        seen.update(json["__args"][1])
        return {"r": {"ttitems": [{"type": "card", "date": "2026-09-25", "uniperiod": "1",
                                   "starttime": "08:00", "endtime": "08:45", "subjectid": "201"}]}}

    FakeEdupage.routes = {"currenttt.js": tt}
    result = service.get_timetable(None, "today", 3)

    assert (seen["datefrom"], seen["dateto"], seen["id"], seen["table"]) == (
        "2026-09-25", "2026-09-27", "501", "students")
    assert result["days"][0]["lessons"][0]["subject"] == "Matematika"
    with pytest.raises(EdupageToolError, match="between 1 and 31"):
        service.get_timetable(None, None, 40)


def test_homework_window_kind_and_done(service, today):
    FakeEdupage.routes = {"/timeline/": _timeline()}

    result = service.get_homework(None, None, None, "all", False)
    assert [h["title"] for h in result["homework"]] == ["Quiz"]  # Exercise is done

    result = service.get_homework(None, None, None, "all", True)
    assert [(h["title"], h["done"]) for h in result["homework"]] == [("Exercise", True), ("Quiz", False)]

    result = service.get_homework(None, None, None, "exam", True)
    assert [h["title"] for h in result["homework"]] == ["Quiz"]
    with pytest.raises(EdupageToolError, match="kind"):
        service.get_homework(None, None, None, "chores", True)


def test_homework_for_a_siblings_class_is_dropped(config):
    FakeEdupage.accounts = {"school-a": login_data(children=("501", "502"))}
    FakeEdupage.routes = {"/timeline/": _timeline(homeworks=[
        {"homeworkid": "subid:A", "typ": "hw", "name": "Mine", "dateto": "2026-09-28", "classids": ["401"]},
        {"homeworkid": "subid:B", "typ": "hw", "name": "Sibling", "dateto": "2026-09-28", "classids": ["402"]},
        {"homeworkid": "subid:C", "typ": "hw", "name": "Shared", "dateto": "2026-09-28", "classids": []},
    ])}
    service = EdupageService(config, EdupageClient(config, factory=FakeEdupage))
    result = service.get_homework("Jana", "2026-09-25", "2026-09-30", "all", True)
    assert [h["title"] for h in result["homework"]] == ["Mine", "Shared"]


def test_messages_are_threaded_and_filtered(service, today):
    FakeEdupage.routes = {"/timeline/": _timeline()}

    result = service.get_messages(None, None, None, 50)
    assert result["since"] == "2026-09-11"
    (thread,) = result["messages"]  # "Welcome back" is older than 14 days
    assert thread["text"] == "Trip on Friday"
    assert [r["text"] for r in thread["replies"]] == ["Thanks!"]

    assert service.get_messages(None, None, "thanks", 50)["total"] == 1
    assert service.get_messages(None, None, "nothing", 50)["total"] == 0
    assert service.get_messages(None, "2026-09-01", None, 50)["total"] == 2
    with pytest.raises(EdupageToolError, match="limit"):
        service.get_messages(None, None, None, 0)


def test_notifications_hide_system_entries_unless_asked(service, today):
    FakeEdupage.routes = {"/timeline/": _timeline()}

    texts = [n["text"] for n in service.get_notifications(None, None, None, None, 50, False)["notifications"]]
    assert "Tip of the day" not in texts and "Hidden" not in texts
    assert texts[0] == "Thanks!"

    with_system = service.get_notifications(None, None, None, None, 50, True)["notifications"]
    assert "Tip of the day" in [n["text"] for n in with_system]

    grades = service.get_notifications(None, "2026-09-01", ["grade"], None, 50, False)
    assert [n["text"] for n in grades["notifications"]] == ["Známka - Matematika: 1"]

    limited = service.get_notifications(None, None, None, None, 1, False)
    assert limited["count"] == 1 and limited["total"] > 1


def test_events_keep_the_latest_version(service, today):
    FakeEdupage.routes = {"/timeline/": _timeline()}
    (event,) = service.get_events(None, None, None)["events"]
    assert event["title"] == "Zoo" and event["details"] == "Bring lunch"
    assert service.get_events(None, "2026-10-03", "2026-10-10")["events"] == []


def test_absences(service, today):
    FakeEdupage.routes = {"/timeline/": _timeline()}
    result = service.get_absences(None, None)
    assert result["since"] == "2026-09-01"
    assert (result["days_absent"], result["unexcused_days"]) == (1, 1)


def test_grades_subject_filter_prefers_exact_matches(service, today):
    from test_parsing import _grade

    FakeEdupage.grades = [_grade(1.0, subject_id=201), _grade(2.0, subject_id=202)]
    assert [g["subject"] for g in service.get_grades(None, "MAT", None, None, None)["grades"]] == ["Matematika"]
    assert [g["subject"] for g in service.get_grades(None, "mat", None, None, None)["grades"]] == ["Matematika"]
    assert service.get_grades(None, "info", None, None, None)["count"] == 1
    with pytest.raises(EdupageToolError, match="term"):
        service.get_grades(None, None, 2025, "3", None)


def test_grades_include_filtered_text_grades(service, today):
    from datetime import datetime
    from types import SimpleNamespace

    def text(grade_id, subject_id, day):
        return SimpleNamespace(grade_id=grade_id, comment="Great progress", grade_type="Hodnotenie",
                               date=datetime(2026, 9, day, 9, 0), subject_id=subject_id, subject_name="MAT")

    FakeEdupage.text_grades = [text(1, 201, 10), text(2, 202, 20)]
    result = service.get_grades(None, None, None, None, None)
    assert [t["id"] for t in result["text_grades"]] == ["2", "1"]
    assert result["text_grades"][0]["subject"] == "Informatika"
    assert result["text_grades"][1]["comment"] == "Great progress"
    assert [t["id"] for t in service.get_grades(None, "MAT", None, None, None)["text_grades"]] == ["1"]
    assert [t["id"] for t in service.get_grades(None, None, None, None, "2026-09-15")["text_grades"]] == ["2"]


# -- meals -----------------------------------------------------------------------------


def _menu_page(day: date, ordered: str | None = None):
    days = {}
    for i in range(5):
        d = (day - timedelta(days=day.weekday()) + timedelta(days=i)).isoformat()
        days[d] = {"2": {
            "isChoosable": True, "choosableMenus": {"1": True, "2": True},
            "nazvyMenu": {"1": {"skratka": "A"}, "2": {"skratka": "B"}},
            "menus": {"1": {"skratkaMenu": "A", "rows": [{"nazov": "Soup"}]},
                      "2": {"skratkaMenu": "B", "rows": [{"nazov": "Pasta"}]}},
            "evidencia": {"stav": "V" if ordered else None, "obj": ordered},
        }}
    blob = {"school-a": {"novyListok": {**days, "addInfo": {"stravnikid": "77", "kredit": 5}}}}
    return FakeResponse(text=f"var x = {{edupageData: {json.dumps(blob)},\r\n}}")


def test_meals_span_several_pages(service, today):
    pages = []

    def menu(url, **_):
        day = date.fromisoformat(f"{url[-8:-4]}-{url[-4:-2]}-{url[-2:]}")
        pages.append(day)
        return _menu_page(day)

    FakeEdupage.routes = {"/menu/": menu}
    result = service.get_meals(None, "2026-09-24", 7)

    assert [d["date"] for d in result["days"]] == [
        "2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30"]
    assert pages == [date(2026, 9, 24), date(2026, 9, 28)]  # one page per week
    assert result["credit"] == 5.0


def test_order_meal(service, today):
    posted = []

    def menu(url, **kwargs):
        if "data" in kwargs:
            posted.append(json.loads(kwargs["data"]["jedlaStravnika"]))
            return {"error": ""}
        return _menu_page(today, ordered="B" if posted else None)

    FakeEdupage.routes = {"/menu/": menu}

    result = service.order_meal(None, "2026-09-25", "lunch", "b")
    assert posted[-1] == {"stravnikid": "77", "mysqlDate": "2026-09-25", "jids": {"2": "B"},
                          "view": "pc_listok", "pravo": "Student"}
    assert result["now"] == {"ordered": True, "ordered_menu": "B", "order_state": "V"}

    service.order_meal(None, "2026-09-25", "lunch", "cancel")
    assert posted[-1]["jids"] == {"2": "AX"}

    with pytest.raises(EdupageToolError, match="Choosable: A, B"):
        service.order_meal(None, "2026-09-25", "lunch", "Z")
    with pytest.raises(EdupageToolError, match="Meals that day: lunch"):
        service.order_meal(None, "2026-09-25", "dinner", "A")


def test_a_refused_meal_change_is_reported(service, today):
    FakeEdupage.routes = {"/menu/": lambda url, **kw: {"error": "too late"} if "data" in kw else _menu_page(today)}
    with pytest.raises(EdupageToolError, match="too late"):
        service.order_meal(None, "2026-09-25", "lunch", "A")


# -- messages ---------------------------------------------------------------------------


def test_send_message_resolves_teachers(service):
    sent = []

    def create(data=None, **_):
        sent.append(data)
        return FakeResponse(text=json.dumps({"changes": [{"timelineid": "999"}]}))

    FakeEdupage.routes = {"akcia=createItem": create}

    result = service.send_message(None, ["anna novakova", "HP"], "Hello")
    assert result == {"status": "success", "school": "school-a",
                      "sent_to": ["Anna Nováková", "Peter Horváth"], "message_id": "999"}
    assert len(sent) == 1

    service.send_message("school-a", ["Ucitel101"], "Hi")
    with pytest.raises(EdupageToolError, match="matches 2 teachers"):
        service.send_message(None, ["Nováková"], "Hello")
    with pytest.raises(EdupageToolError, match="matches 0 teachers"):
        service.send_message(None, ["Old Teacher"], "Hello")  # left the school
    with pytest.raises(EdupageToolError, match="empty"):
        service.send_message(None, ["HP"], "  ")


def test_list_teachers(service):
    result = service.list_teachers(None, "novak")
    assert [(t["name"], t["recipient_id"]) for t in result["teachers"]] == [
        ("Anna Nováková", "Ucitel101"), ("Eva Nováková", "Ucitel103")]
    assert result["teachers"][0]["full_name"] == "Mgr. Anna Nováková"
    assert service.list_teachers(None, "hp")["teachers"][0]["name"] == "Peter Horváth"


def test_read_only_blocks_writes(config):
    service = EdupageService(dataclasses.replace(config, read_only=True),
                             EdupageClient(config, factory=FakeEdupage))
    with pytest.raises(EdupageToolError, match="read-only"):
        service.send_message(None, ["HP"], "Hello")
    with pytest.raises(EdupageToolError, match="read-only"):
        service.order_meal(None, "today", "lunch", "A")


def test_school_info(service):
    info = service.get_school_info(None)
    assert info["bell_schedule"][0] == {"period": "1", "start_time": "08:00", "end_time": "08:45"}
    assert info["classes"] == [{"student": "Jana Testová", "class": "IV.A",
                                "class_teachers": ["Anna Nováková"], "home_classroom": "4A",
                                "classmates": 2}]


def test_briefing_survives_a_failing_section(service, today):
    FakeEdupage.routes = {
        "/timeline/": _timeline(),
        "currenttt.js": {"r": {"ttitems": []}},
        "/menu/": FakeResponse(text="<html>no canteen</html>"),
    }
    (section,) = service.get_briefing(None, 7)["students"]
    assert [h["title"] for h in section["exams"]] == ["Quiz"]
    assert section["today"] == []
    assert any(w.startswith("meals:") for w in section["warnings"])
