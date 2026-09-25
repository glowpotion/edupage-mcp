from __future__ import annotations

import dataclasses
import json

import pytest
from conftest import FakeEdupage, login_data

from edupage_mcp.client import EdupageClient, EdupageToolError, TwoFactorRequired
from edupage_api.exceptions import BadCredentialsException, CaptchaException


def test_schools_are_discovered_from_a_parent_account(client):
    FakeEdupage.accounts = {"school-a": login_data(), "school-b": login_data(children=("502",))}
    assert list(client.schools()) == ["school-a", "school-b"]
    # The first school reuses the discovery login; the second logs in lazily.
    assert len(FakeEdupage.instances) == 1


def test_configured_subdomains_skip_discovery(config):
    FakeEdupage.accounts = {"school-a": login_data(), "school-b": login_data(children=("502",))}
    client = EdupageClient(dataclasses.replace(config, subdomains=("school-b",)), factory=FakeEdupage)
    assert list(client.schools()) == ["school-b"]
    assert [s.name for s in client.students()] == ["Marek Testový"]


def test_students_across_schools(client):
    FakeEdupage.accounts = {"school-a": login_data(), "school-b": login_data(children=("502",))}
    students = client.students()
    assert [(s.name, s.first_name, s.class_name, s.school) for s in students] == [
        ("Jana Testová", "Jana", "IV.A", "school-a"),
        ("Marek Testový", "Marek", "II.B", "school-b"),
    ]


def test_a_student_account_is_its_own_student(client):
    FakeEdupage.accounts = {"school-a": {**login_data(user="Student501"), "parentStudentids": []}}
    (student,) = client.students()
    assert (student.id, student.name) == ("501", "Jana Testová")


@pytest.mark.parametrize("query", ["jana", "JANA TESTOVÁ", "testova", "501", "school-a"])
def test_resolve_student(client, query):
    FakeEdupage.accounts = {"school-a": login_data(), "school-b": login_data(children=("502",))}
    assert client.resolve_student(query).id == "501"


def test_resolve_student_needs_a_name_when_there_are_several(client):
    FakeEdupage.accounts = {"school-a": login_data(), "school-b": login_data(children=("502",))}
    with pytest.raises(EdupageToolError, match="several students"):
        client.resolve_student(None)
    with pytest.raises(EdupageToolError, match="Jana Testová .school-a, id 501."):
        client.resolve_student("nobody")


def test_resolve_student_defaults_to_the_only_one(client):
    assert client.resolve_student(None).name == "Jana Testová"


def test_ambiguous_names_are_refused(client):
    FakeEdupage.accounts = {"school-a": login_data(children=("501", "502"))}
    with pytest.raises(EdupageToolError, match="No single student"):
        client.resolve_student("test")


def test_a_failed_read_is_retried_once_with_a_fresh_login(client):
    calls = []

    def flaky(edupage):
        calls.append(edupage)
        if len(calls) == 1:
            raise ValueError("session expired")
        return "ok"

    assert client.run("school-a", flaky) == "ok"
    assert len(calls) == 2
    assert len(FakeEdupage.instances) == 2  # reload failed on the fake, so a new login


def test_writes_are_never_retried(client):
    def failing(edupage):
        raise ValueError("boom")

    with pytest.raises(ValueError):
        client.run("school-a", failing, retry=False)
    assert len(FakeEdupage.instances) == 1


def test_select_child_switches_only_when_needed(client):
    FakeEdupage.accounts = {"school-a": login_data(children=("501", "503"))}
    jana, other = client.students()
    edupage = client.school("school-a").edupage
    client.select_child(edupage, jana)
    client.select_child(edupage, jana)
    client.select_child(edupage, other)
    assert edupage.switched == [501, 503]


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (BadCredentialsException(), "rejected the username or password"),
        (CaptchaException(), "captcha"),
        (RuntimeError("down"), "Could not log in"),
    ],
)
def test_login_failures_are_explained(client, exc, message):
    FakeEdupage.fail_login = exc
    with pytest.raises(EdupageToolError, match=message):
        client.schools()


class FakeTwoFactor:
    def __init__(self, confirmed=False):
        self.confirmed = confirmed
        self.finished_with = None

    def is_confirmed(self):
        return self.confirmed

    def finish(self):
        self.finished_with = "device"
        self._complete()

    def finish_with_code(self, code):
        self.finished_with = code
        self._complete()

    def _complete(self):
        edupage = FakeEdupage.instances[-1]
        edupage.data = FakeEdupage.accounts[edupage.subdomain]
        edupage.is_logged_in = True


def test_two_factor_login_with_a_code(client):
    FakeEdupage.two_factor = two_factor = FakeTwoFactor()
    with pytest.raises(TwoFactorRequired, match="complete_login"):
        client.students()
    # Still pending: calling again does not start a second login.
    with pytest.raises(TwoFactorRequired):
        client.students()
    FakeEdupage.two_factor = None

    assert client.complete_login(None, " 123456 ")["status"] == "success"
    assert two_factor.finished_with == "123456"
    assert client.students()[0].name == "Jana Testová"


def test_two_factor_login_approved_in_the_app(client):
    FakeEdupage.two_factor = two_factor = FakeTwoFactor(confirmed=False)
    with pytest.raises(TwoFactorRequired):
        client.students()
    with pytest.raises(EdupageToolError, match="not been approved yet"):
        client.complete_login(None, None)
    two_factor.confirmed = True
    client.complete_login("school-a", None)
    assert two_factor.finished_with == "device"


def test_sessions_are_saved_privately(config, tmp_path):
    path = tmp_path / "state" / "sessions.json"
    client = EdupageClient(dataclasses.replace(config, session_file=path), factory=FakeEdupage)
    client.schools()
    assert json.loads(path.read_text()) == {
        "school-a": {"sid": "sid-1", "username": "parent@example.com"}
    }
    assert path.stat().st_mode & 0o777 == 0o600


def test_unknown_school(client):
    with pytest.raises(EdupageToolError, match="reaches: school-a"):
        client.school("elsewhere")
