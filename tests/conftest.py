"""Fakes standing in for ``edupage_api.Edupage`` and EduPage's JSON.

All names and IDs here are invented.
"""

from __future__ import annotations

import json
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from edupage_mcp.client import EdupageClient
from edupage_mcp.config import Config
from edupage_mcp.service import EdupageService

TZ = ZoneInfo("Europe/Bratislava")


def dbi(**overrides: Any) -> dict[str, Any]:
    base = {
        "teachers": {
            "101": {"id": "101", "firstname": "Anna", "lastname": "Nováková", "short": "NA",
                    "classroomid": "301", "nameprefix": "Mgr.", "isOut": False},
            "-102": {"id": "-102", "firstname": "Peter", "lastname": "Horváth", "short": "HP",
                     "classroomid": "", "isOut": False},
            "103": {"id": "103", "firstname": "Eva", "lastname": "Nováková", "short": "NE",
                    "classroomid": "", "isOut": False},
            "104": {"id": "104", "firstname": "Old", "lastname": "Teacher", "short": "OT",
                    "isOut": True},
        },
        "subjects": {
            "201": {"id": "201", "name": "Matematika", "short": "MAT"},
            "202": {"id": "202", "name": "Informatika", "short": "INF"},
            "203": {"id": "203", "name": "Slovenský jazyk", "short": "SJL"},
        },
        "classrooms": {"301": {"id": "301", "name": "4.A", "short": "4A"}},
        "classes": {
            "401": {"id": "401", "name": "IV.A", "short": "IV.A", "teacherid": "101",
                    "teacher2id": "", "classroomid": "301"},
            "402": {"id": "402", "name": "II.B", "short": "II.B", "teacherid": "-102"},
        },
        "students": {
            "501": {"id": "501", "firstname": "Jana", "lastname": "Testová", "classid": "401"},
            "502": {"id": "502", "firstname": "Marek", "lastname": "Testový", "classid": "402"},
            "503": {"id": "503", "firstname": "Other", "lastname": "Kid", "classid": "401"},
        },
        "event_types": {"sexam": {"id": "sexam", "name": "Small exam"},
                        "trip": {"id": "trip", "name": "School trip"}},
        "studentabsent_types": {"-2": {"id": "-2", "name": "Excused (parent)"}},
        "periods": [
            {"id": "1", "short": "1", "starttime": "08:00", "endtime": "08:45"},
            {"id": "2", "short": "2", "starttime": "08:55", "endtime": "09:40"},
        ],
    }
    base.update(overrides)
    return base


def login_data(user: str = "Rodic-900", children: tuple[str, ...] = ("501",)) -> dict[str, Any]:
    return {
        "userid": user,
        "parentStudentids": [int(c) for c in children],
        "parentChild": int(children[0]) if children else None,
        "dbi": dbi(),
        "dp": {"year": 2026},
        "school_email": "office@school.example",
        "userrow": {"p_meno": "Parent"},
    }


class FakeCookies:
    def __init__(self, sid: str = "sid-1"):
        self.sid = sid

    def get_dict(self, domain: str) -> dict[str, str]:
        return {"PHPSESSID": self.sid}

    def get(self, name: str) -> str:
        return self.sid


class FakeResponse:
    def __init__(self, payload: Any = None, text: str | None = None):
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(self, routes: dict[str, Any]):
        self.routes = routes
        self.cookies = FakeCookies()
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _route(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        for fragment, handler in self.routes.items():
            if fragment in url:
                result = handler(url=url, **kwargs) if callable(handler) else handler
                return result if isinstance(result, FakeResponse) else FakeResponse(result)
        raise AssertionError(f"unexpected request: {method} {url}")

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._route("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._route("POST", url, **kwargs)


class FakeEdupage:
    """Just enough of ``edupage_api.Edupage`` for the client."""

    instances: list["FakeEdupage"] = []
    accounts: dict[str, dict[str, Any]] = {}
    routes: dict[str, Any] = {}
    subdomains: list[str] = []
    two_factor: Any = None
    fail_login: Exception | None = None

    def __init__(self, request_timeout: int = 5):
        self.data: dict[str, Any] | None = None
        self.is_logged_in = False
        self.subdomain: str | None = None
        self.gsec_hash = "gsh"
        self.session = FakeSession(type(self).routes)
        self.switched: list[int] = []
        self.logins = 0
        type(self).instances.append(self)

    def login(self, username: str, password: str, subdomain: str):
        if type(self).fail_login is not None:
            raise type(self).fail_login
        self.logins += 1
        self.subdomain = subdomain
        if type(self).two_factor is not None:
            return type(self).two_factor
        self.data = type(self).accounts[subdomain]
        self.is_logged_in = True
        return None

    def login_auto(self, username: str, password: str):
        return self.login(username, password, next(iter(type(self).accounts)))

    def get_subdomains(self) -> list[str]:
        return list(type(self).subdomains or type(self).accounts)

    def get_school_year(self) -> int:
        return self.data["dp"]["year"]

    def switch_to_child(self, child: int) -> None:
        self.switched.append(child)

    def get_grades(self):
        return type(self).grades

    grades: list[Any] = []


@pytest.fixture(autouse=True)
def reset_fake():
    FakeEdupage.instances = []
    FakeEdupage.accounts = {"school-a": login_data()}
    FakeEdupage.routes = {}
    FakeEdupage.subdomains = []
    FakeEdupage.two_factor = None
    FakeEdupage.fail_login = None
    FakeEdupage.grades = []
    yield


@pytest.fixture
def config() -> Config:
    return Config(
        username="parent@example.com",
        password="secret",
        subdomains=(),
        timezone=TZ,
    )


@pytest.fixture
def client(config: Config) -> EdupageClient:
    return EdupageClient(config, factory=FakeEdupage)


@pytest.fixture
def service(config: Config, client: EdupageClient) -> EdupageService:
    return EdupageService(config, client)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
