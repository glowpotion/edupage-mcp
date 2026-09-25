from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from conftest import TZ, dbi

from edupage_mcp.parsing import (
    Directory,
    belongs_to_student,
    fold,
    grade_to_dict,
    group_by_date,
    is_visible,
    parse_absence,
    parse_event,
    parse_homeworks,
    parse_meals,
    parse_timeline_item,
    parse_timetable,
    summarise_grades,
    thread_messages,
    type_name,
)

BASE = "https://school-a.edupage.org"
D = Directory(dbi())


def test_fold_ignores_case_and_accents():
    assert fold("  Nováková ") == "novakova"
    assert fold("ŠTEFAN") == fold("stefan")


def test_type_names_come_from_the_library_enum():
    assert type_name("sprava") == "message"
    assert type_name("znamka") == "grade"
    assert type_name("made-up") == "made-up"
    assert type_name(None) is None


# -- timetable -------------------------------------------------------------------


def test_timetable_resolves_ids_and_keeps_substitutions():
    items = [
        {"type": "card", "date": "2026-09-25", "uniperiod": "2", "starttime": "08:55",
         "endtime": "09:40", "subjectid": "202", "teacherids": ["-102"],
         "classroomids": ["301"], "classids": ["401"], "groupnames": [""], "changed": True},
        {"type": "card", "date": "2026-09-25", "uniperiod": "2", "starttime": "08:55",
         "endtime": "09:40", "subjectid": "201", "teacherids": ["101"],
         "classroomids": [], "classids": ["401"], "groupnames": ["1. sk"], "removed": True},
        {"type": "card", "date": "2026-09-24", "uniperiod": "1", "starttime": "08:00",
         "endtime": "09:40", "subjectid": "201", "teacherids": ["101"], "classids": ["401"],
         "groupnames": [], "durationperiods": 2, "eventids": ["2026-09-24:ABC123"]},
    ]
    lessons = parse_timetable(items, D)

    assert [l["date"] for l in lessons] == ["2026-09-24", "2026-09-25", "2026-09-25"]
    first = lessons[0]
    assert first["subject"] == "Matematika" and first["subject_short"] == "MAT"
    assert first["teachers"] == ["Anna Nováková"]
    assert first["periods"] == 2
    assert first["homework_ids"] == ["subid:ABC123"]

    replacement, original = lessons[1], lessons[2]
    assert replacement["changed"] and not replacement["cancelled"]
    assert replacement["teachers"] == ["Peter Horváth"] and replacement["classrooms"] == ["4A"]
    assert original["cancelled"] and original["groups"] == ["1. sk"]

    days = group_by_date(lessons)
    assert [(d["date"], d["weekday"], len(d["lessons"])) for d in days] == [
        ("2026-09-24", "Thursday", 1),
        ("2026-09-25", "Friday", 2),
    ]
    assert "date" not in days[0]["lessons"][0]


# -- timeline ----------------------------------------------------------------------


def _item(**kw):
    base = {"timelineid": "1", "typ": "sprava", "timestamp": "2026-09-25 13:04:00",
            "cas_pridania": "2026-09-25 10:00:00", "vlastnik_meno": "Anna Nováková",
            "user_meno": "Rodičia IV.A", "user": "Rodicko401", "text": "Hello",
            "data": "{}", "removed": "0", "pocet_reakcii": "0", "reakcia_na": None}
    base.update(kw)
    return base


def test_timeline_item_prefers_full_message_content_and_lists_attachments():
    item = _item(
        text="Dôležitá správa",
        pocet_reakcii="2",
        data=json.dumps({
            "messageContent": "  Full text of the message  ",
            "receipt": "1",
            "attachements": {"/elearning/abc": "trip.pdf", "https://cdn.example/x": "x.png"},
        }),
    )
    entry = parse_timeline_item(item, {"1": {"doneMaxCas": "2026-09-25 14:00:00", "starred": "1"}},
                                TZ, BASE)

    assert entry["text"] == "Full text of the message"
    assert entry["type"] == "message" and entry["type_code"] == "sprava"
    assert entry["timestamp"] == "2026-09-25T13:04:00+02:00"
    assert entry["attachments"] == [
        {"name": "trip.pdf", "url": BASE + "/elearning/abc"},
        {"name": "x.png", "url": "https://cdn.example/x"},
    ]
    assert entry["confirmation_requested"] is True
    assert entry["reply_count"] == 2
    assert entry["done_at"].startswith("2026-09-25T14:00")
    assert entry["starred"] is True


def test_timeline_item_falls_back_to_the_data_name():
    entry = parse_timeline_item(_item(typ="homework", text="", data='{"nazov": "Read p. 10"}'),
                                {}, TZ, BASE)
    assert entry["text"] == "Read p. 10"


def test_helper_and_system_items_are_hidden():
    assert not is_visible(_item(typ="h_homework"), include_system=True)
    assert not is_visible(_item(removed="1"), include_system=True)
    assert not is_visible(_item(typ="genotif"), include_system=False)
    assert is_visible(_item(typ="genotif"), include_system=True)
    assert is_visible(_item(typ="znamka"), include_system=False)


def test_items_for_a_sibling_are_filtered_out():
    groups = {
        "501": ["*", "Student501", "Trieda401", "Plan7"],
        "502": ["*", "Student502", "Trieda402", "Plan8"],
    }
    assert belongs_to_student(_item(user="Trieda401"), "501", groups)
    assert not belongs_to_student(_item(user="Trieda402"), "501", groups)
    assert not belongs_to_student(_item(user="StudRodic502"), "501", groups)
    assert not belongs_to_student(_item(user="RStud-900@502"), "501", groups)
    assert belongs_to_student(_item(user="*"), "501", groups)
    assert belongs_to_student(_item(user="Rodic-900"), "501", groups)
    # With one child nothing is filtered.
    assert belongs_to_student(_item(user="Trieda402"), "501", {"501": groups["501"]})


def test_replies_nest_under_their_message():
    entries = [
        {"id": "1", "timestamp": "2026-09-20T10:00:00+02:00", "text": "root"},
        {"id": "2", "timestamp": "2026-09-21T10:00:00+02:00", "text": "reply b", "reply_to": "1"},
        {"id": "3", "timestamp": "2026-09-20T12:00:00+02:00", "text": "reply a", "reply_to": "1"},
        {"id": "4", "timestamp": "2026-09-22T10:00:00+02:00", "text": "orphan", "reply_to": "99"},
    ]
    threads = thread_messages(entries)
    assert [t["id"] for t in threads] == ["4", "1"]
    assert [r["text"] for r in threads[1]["replies"]] == ["reply a", "reply b"]
    assert "replies" not in threads[0]


# -- homework ------------------------------------------------------------------------


def test_homework_joins_done_state_and_attachments_from_the_timeline():
    homeworks = [
        {"homeworkid": "subid:AAA", "typ": "hw", "name": "Exercise 3 ", "details": None,
         "dateto": "2026-09-30", "datefrom": "2026-09-25", "predmet_meno": "Matematika",
         "kurz_meno": "IV.A · Matematika", "ucitel_meno": "Anna Nováková", "nextlesson": True},
        {"homeworkid": "subid:BBB", "typ": "sexam", "name": "Quiz", "details": "",
         "dateto": "2026-09-29", "datefrom": "2026-09-24", "subjectid": "202",
         "time": "08:00 - 08:45"},
    ]
    timeline = [
        {"timelineid": "77", "typ": "homework", "ineid": "AAA",
         "data": json.dumps({"popis": "pages 10-12",
                             "attachements": {"/elearning/f": "sheet.pdf"}})},
    ]
    out = parse_homeworks(homeworks, timeline, {"77": {"doneMaxCas": "2026-09-26 18:00:00"}}, D, BASE)

    quiz, exercise = out  # sorted by due date
    assert quiz["kind"] == "exam" and quiz["type"] == "Small exam"
    assert quiz["subject"] == "Informatika" and quiz["time"] == "08:00 - 08:45"
    assert quiz["done"] is False and quiz["details"] is None

    assert exercise["kind"] == "homework" and exercise["type"] == "Homework"
    assert exercise["title"] == "Exercise 3"
    assert exercise["done"] is True and exercise["timeline_id"] == "77"
    assert exercise["details"] == "pages 10-12"
    assert exercise["attachments"] == [{"name": "sheet.pdf", "url": BASE + "/elearning/f"}]
    assert exercise["due_next_lesson"] is True


# -- events and absences ---------------------------------------------------------------


def test_event_from_a_timeline_item():
    item = _item(typ="event", timelineid="9", data=json.dumps({
        "typ": "trip", "name": "Zoo", "date": "2026-10-02", "dateto": "2026-10-03",
        "allday": True, "teacherids": ["101"], "classids": ["401"], "time": "08:00",
    }))
    event = parse_event(item, D)
    assert event["title"] == "Zoo" and event["type"] == "School trip"
    assert (event["start_date"], event["end_date"]) == ("2026-10-02", "2026-10-03")
    assert event["teachers"] == ["Anna Nováková"] and event["classes"] == ["IV.A"]
    assert event["all_day"] is True
    assert parse_event(_item(typ="event", data="not json"), D) is None


def test_absence_and_excuse():
    absent = parse_absence(_item(typ="student_absent", data=json.dumps(
        {"p_2": "A", "p_1": "A", "cas_udalosti": "2026-09-09", "isExcused": False})), D, TZ)
    assert absent["kind"] == "absence" and absent["date"] == "2026-09-09"
    assert absent["periods"] == {"1": "A", "2": "A"} and absent["excused"] is False

    excuse = parse_absence(_item(typ="ospravedlnenka", data=json.dumps(
        {"datefrom": "2026-09-09", "dateto": "2026-09-10", "studentabsent_typeid": "-2",
         "note": " flu "})), D, TZ)
    assert excuse["kind"] == "excuse" and excuse["end_date"] == "2026-09-10"
    assert excuse["reason"] == "Excused (parent)" and excuse["note"] == "flu"

    assert parse_absence(_item(typ="sprava"), D, TZ) is None


# -- meals --------------------------------------------------------------------------


def test_meals_with_several_menus_and_an_order():
    day = {
        "0": {"isCooking": False},
        "2": {
            "vydaj_od": "11:30", "vydaj_do": "14:00", "isChoosable": True,
            "choosableMenus": {"1": True, "2": True},
            "nazvyMenu": {"1": {"nazov": "Menu A", "skratka": "A"},
                          "2": {"nazov": "Menu B", "skratka": "B"}},
            "menus": {
                "2": {"nazovMenu": "Menu B", "skratkaMenu": "B",
                      "rows": [{"nazov": "Pasta", "alergenyStr": "1, 7"}]},
                "1": {"nazovMenu": "Menu A", "skratkaMenu": "A",
                      "rows": [{"nazov": "  Soup   of the day ", "hmotnostiStr": "250"}]},
            },
            "evidencia": {"stav": "V", "obj": "B"},
            "prihlas_do": "2026-09-24 13:45", "odhlas_do": "2026-09-25 08:00", "zmen_do": None,
        },
    }
    add_info = {"alergenyIDS": {"1": {"ozn": "1", "nazov": "Gluten"}}, "kredit": "12.5"}
    (lunch,) = parse_meals(day, add_info)

    assert lunch["meal"] == "lunch" and lunch["slot"] == "2"
    assert [m["menu"] for m in lunch["menus"]] == ["A", "B"]
    assert lunch["menus"][0]["items"] == [{"name": "Soup of the day", "portion": "250"}]
    assert lunch["menus"][1]["items"][0]["allergens"] == ["1 Gluten", "7"]
    assert lunch["ordered"] and lunch["ordered_menu"] == "B"
    assert lunch["can_choose"] and lunch["choosable_menus"] == ["A", "B"]
    assert lunch["cancel_by"] == "2026-09-25 08:00"


def test_meal_choices_listed_per_serving_type():
    day = {"0": {"isChoosable": True, "choosableMenus": [],
                 "typVydaj": {"7": {"choosableMenus": {"1": True}}},
                 "rows": [{"nazov": "Roll"}], "evidencia": {"stav": None}}}
    (breakfast,) = parse_meals(day, None)
    assert breakfast["meal"] == "breakfast"
    assert breakfast["choosable_menus"] == ["A"]
    assert breakfast["menus"] == [{"menu": "A", "label": None, "items": [{"name": "Roll"}]}]
    assert breakfast["ordered"] is False


# -- grades --------------------------------------------------------------------------


def _grade(value, subject_id=201, weight=1.0, max_points=None, percent=None):
    return SimpleNamespace(
        event_id=1, title="Test", grade_n=value, comment="", date=datetime(2026, 9, 24, 13, 0),
        subject_id=subject_id, subject_name="MAT", teacher=SimpleNamespace(name="Anna Nováková"),
        max_points=max_points, importance=weight, verbal=False, percent=percent,
        class_grade_avg=1.5,
    )


def test_grades_convert_and_summarise():
    grades = [
        grade_to_dict(_grade(1.0, weight=1.0), D, TZ),
        grade_to_dict(_grade(3.0, weight=0.5), D, TZ),
        grade_to_dict(_grade(18.0, subject_id=202, max_points=20.0, percent=90.0), D, TZ),
    ]
    assert grades[0]["grade"] == 1 and grades[0]["subject"] == "Matematika"
    assert grades[0]["date"] == "2026-09-24T13:00:00+02:00"
    assert grades[0]["comment"] is None and grades[0]["class_average"] == 1.5

    summary = {s["subject"]: s for s in summarise_grades(grades)}
    assert summary["Matematika"] == {"subject": "Matematika", "count": 2, "average": 1.67}
    assert summary["Informatika"] == {"subject": "Informatika", "count": 1, "average_percent": 90.0}
