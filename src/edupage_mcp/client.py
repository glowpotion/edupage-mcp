"""EduPage sessions, school discovery and the requests behind each tool.

One EduPage account can reach several schools (a parent with children at
different schools has one login that works on each ``<school>.edupage.org``),
but every school is a separate session. :class:`EdupageClient` keeps one
``edupage_api.Edupage`` per school, logs in lazily, refreshes the session when
it gets old, and retries a failed read once with a fresh login.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, TypeVar

from edupage_api import Edupage
from edupage_api.compression import RequestData
from edupage_api.exceptions import (
    BadCredentialsException,
    CaptchaException,
    SecondFactorFailedException,
)
from edupage_api.exceptions import FailedToParseGradeDataError
from edupage_api.grades import Term
from edupage_api.login import Login, TwoFactorLogin
from edupage_api.substitution import Substitution
from edupage_api.utils import RequestUtil

from .config import Config
from .parsing import Directory, fold

T = TypeVar("T")

TIMELINE_CACHE_SECONDS = 60


class EdupageToolError(RuntimeError):
    """A problem the caller can act on; the message says what to do."""


class TwoFactorRequired(EdupageToolError):
    pass


@dataclass
class Student:
    id: str
    name: str
    first_name: str
    class_id: str | None
    class_name: str | None
    school: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "class": self.class_name,
            "school": self.school,
            "school_url": f"https://{self.school}.edupage.org",
        }


@dataclass
class School:
    subdomain: str
    edupage: Edupage | None = None
    logged_in_at: float = 0.0
    pending_2fa: TwoFactorLogin | None = None
    selected_child: str | None = None
    timeline: tuple[str, float, dict[str, Any]] | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def base_url(self) -> str:
        return f"https://{self.subdomain}.edupage.org"


class EdupageClient:
    def __init__(self, config: Config, factory: Callable[..., Edupage] = Edupage):
        self.config = config
        self._factory = factory
        self._schools: dict[str, School] | None = None
        self._lock = threading.RLock()

    # -- sessions ------------------------------------------------------------

    def schools(self) -> dict[str, School]:
        """Every school the account reaches, discovering them on first use."""
        with self._lock:
            if self._schools is None:
                if self.config.subdomains:
                    self._schools = {s: School(s) for s in self.config.subdomains}
                else:
                    self._schools = self._discover()
            return self._schools

    def _discover(self) -> dict[str, School]:
        edupage = self._new_edupage()
        try:
            pending = edupage.login_auto(self.config.username, self.config.password)
        except Exception as exc:  # noqa: BLE001
            raise self._login_error(exc, "portal") from exc
        first = School(edupage.subdomain)
        schools = {first.subdomain: first}
        # Kept even if a second factor is needed, so complete_login can find it.
        self._schools = schools
        if pending is not None:
            first.edupage, first.pending_2fa = edupage, pending
            raise self._two_factor_message(first)
        self._logged_in(first, edupage)
        if _account_type(edupage) == "parent":
            try:
                for sub in edupage.get_subdomains():
                    schools.setdefault(sub, School(sub))
            except Exception:  # noqa: BLE001 - one school is still usable
                pass
        return schools

    def school(self, subdomain: str) -> School:
        schools = self.schools()
        if subdomain not in schools:
            raise EdupageToolError(
                f"Unknown school {subdomain!r}. This account reaches: {', '.join(schools)}."
            )
        return schools[subdomain]

    def _new_edupage(self) -> Edupage:
        return self._factory(request_timeout=self.config.request_timeout)

    def _login(self, school: School) -> Edupage:
        if school.pending_2fa is not None:
            raise self._two_factor_message(school)
        if (edupage := self._restore_session(school)) is not None:
            return edupage
        edupage = self._new_edupage()
        try:
            pending = edupage.login(self.config.username, self.config.password, school.subdomain)
        except Exception as exc:  # noqa: BLE001
            raise self._login_error(exc, school.subdomain) from exc
        if pending is not None:
            school.edupage, school.pending_2fa = edupage, pending
            raise self._two_factor_message(school)
        self._logged_in(school, edupage)
        return edupage

    def _logged_in(self, school: School, edupage: Edupage) -> None:
        school.edupage = edupage
        school.logged_in_at = time.monotonic()
        school.pending_2fa = None
        school.selected_child = None
        school.timeline = None
        self._save_session(school)

    def _ensure(self, school: School, *, force: bool = False) -> Edupage:
        if school.edupage is None or school.pending_2fa is not None:
            return self._login(school)
        age = time.monotonic() - school.logged_in_at
        if force or age > self.config.session_ttl_minutes * 60:
            # Reloading with the existing session keeps a two-factor login
            # alive; only fall back to the password if the session has died.
            if not self._reload(school):
                school.edupage = None
                return self._login(school)
        return school.edupage

    def _reload(self, school: School) -> bool:
        edupage = school.edupage
        sid = _session_id(edupage, school.subdomain) if edupage else None
        if not sid:
            return False
        try:
            Login(edupage).reload_data(school.subdomain, sid, self.config.username)
        except Exception:  # noqa: BLE001
            return False
        if not getattr(edupage, "is_logged_in", False) or not edupage.data:
            return False
        self._logged_in(school, edupage)
        return True

    def run(self, subdomain: str, fn: Callable[[Edupage], T], *, retry: bool = True) -> T:
        """Call ``fn`` with a logged-in session, re-logging in once if it fails.

        Writes pass ``retry=False`` so a request that reached EduPage but
        failed to parse is never sent twice.
        """
        school = self.school(subdomain)
        with school.lock:
            edupage = self._ensure(school)
            try:
                return fn(edupage)
            except EdupageToolError:
                raise
            except Exception:
                if not retry:
                    raise
                edupage = self._ensure(school, force=True)
                return fn(edupage)

    # -- two-factor login ---------------------------------------------------------

    def complete_login(self, subdomain: str | None, code: str | None) -> dict[str, Any]:
        with self._lock:
            schools = self._schools or {}
            pending = [s for s in schools.values() if s.pending_2fa is not None]
        if subdomain:
            pending = [s for s in pending if s.subdomain == subdomain]
        if not pending:
            return {"status": "success", "message": "No login is waiting for a second factor."}

        school = pending[0]
        with school.lock:
            two_factor = school.pending_2fa
            try:
                if code:
                    two_factor.finish_with_code(code.strip())
                elif two_factor.is_confirmed():
                    two_factor.finish()
                else:
                    raise EdupageToolError(
                        f"The login to {school.subdomain} has not been approved yet. Approve "
                        "it in the EduPage app, or pass the code EduPage emailed as `code`."
                    )
            except SecondFactorFailedException as exc:
                school.pending_2fa = None
                school.edupage = None
                raise EdupageToolError(
                    "EduPage rejected the second factor (wrong or expired code). Call any "
                    "tool to start a new login."
                ) from exc
            self._logged_in(school, school.edupage)

        if self._schools is not None and len(self._schools) == 1 and not self.config.subdomains:
            # Discovery stopped at the first school; finish it now.
            edupage = school.edupage
            if _account_type(edupage) == "parent":
                for sub in edupage.get_subdomains():
                    self._schools.setdefault(sub, School(sub))
        return {"status": "success", "school": school.subdomain, "message": "Logged in."}

    def _two_factor_message(self, school: School) -> TwoFactorRequired:
        return TwoFactorRequired(
            f"EduPage wants a second factor to log in to {school.subdomain}. Approve the "
            "login in the EduPage mobile app and then call `complete_login`, or call "
            "`complete_login` with the `code` EduPage emailed you."
        )

    @staticmethod
    def _login_error(exc: Exception, where: str) -> EdupageToolError:
        if isinstance(exc, CaptchaException):
            return EdupageToolError(
                "EduPage asked for a captcha, usually after several failed logins. Log in "
                "once in a browser at https://portal.edupage.org, then try again."
            )
        if isinstance(exc, BadCredentialsException):
            return EdupageToolError(
                f"EduPage rejected the username or password (logging in to {where}). Check "
                "EDUPAGE_USERNAME and EDUPAGE_PASSWORD, and set EDUPAGE_SUBDOMAINS if the "
                "account belongs to a specific school."
            )
        return EdupageToolError(f"Could not log in to {where}: {type(exc).__name__}: {exc}")

    # -- session cache ---------------------------------------------------------------

    def _restore_session(self, school: School) -> Edupage | None:
        path = self.config.session_file
        if path is None or not path.exists():
            return None
        try:
            saved = json.loads(path.read_text()).get(school.subdomain)
            if not saved or saved.get("username") != self.config.username:
                return None
            edupage = self._new_edupage()
            Login(edupage).reload_data(school.subdomain, saved["sid"], self.config.username)
        except Exception:  # noqa: BLE001 - expired or unreadable; log in normally
            return None
        if not getattr(edupage, "is_logged_in", False):
            return None
        self._logged_in(school, edupage)
        return edupage

    def _save_session(self, school: School) -> None:
        path = self.config.session_file
        sid = _session_id(school.edupage, school.subdomain) if school.edupage else None
        if path is None or not sid:
            return
        try:
            saved = json.loads(path.read_text()) if path.exists() else {}
            saved[school.subdomain] = {"sid": sid, "username": self.config.username}
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(saved, fh)
            os.replace(tmp, path)
        except (OSError, ValueError):
            pass  # the cache is an optimisation; never fail a login over it

    # -- account and students ---------------------------------------------------------

    def account(self) -> dict[str, Any]:
        schools = []
        for sub in self.schools():
            info = self.run(sub, lambda e: _school_info(e, sub))
            schools.append(info)
        return {
            "username": self.config.username,
            "account_type": schools[0]["account_type"] if schools else None,
            "schools": schools,
            "students": [s.as_dict() for s in self.students()],
        }

    def students(self) -> list[Student]:
        out: list[Student] = []
        for sub in self.schools():
            out.extend(self.run(sub, lambda e: _students(e, sub)))
        return out

    def resolve_student(self, query: str | None) -> Student:
        """Find a student by first name, full name, ID or school subdomain."""
        students = self.students()
        if not students:
            raise EdupageToolError(
                "This account has no students attached (it may be a teacher account)."
            )
        if not query or not query.strip():
            if len(students) == 1:
                return students[0]
            raise EdupageToolError(
                "This account has several students; pass `student` as one of: "
                + ", ".join(f"{s.name} ({s.school})" for s in students)
                + "."
            )
        q = fold(query)
        for test in (
            lambda s: s.id == query.strip(),
            lambda s: fold(s.name) == q,
            lambda s: fold(s.first_name) == q,
            lambda s: s.school == q,
            lambda s: q in fold(s.name),
        ):
            matches = [s for s in students if test(s)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                break
        raise EdupageToolError(
            f"No single student matches {query!r}. Students on this account: "
            + ", ".join(f"{s.name} ({s.school}, id {s.id})" for s in students)
            + "."
        )

    def resolve_school(self, query: str | None) -> str:
        """A subdomain from a subdomain, or from anything naming a student."""
        schools = self.schools()
        if not query:
            if len(schools) == 1:
                return next(iter(schools))
            raise EdupageToolError(
                "This account reaches several schools; pass `school` as a subdomain ("
                + ", ".join(schools)
                + ") or a student's name."
            )
        q = query.strip().lower().removesuffix(".edupage.org")
        if q in schools:
            return q
        return self.resolve_student(query).school

    def directory(self, subdomain: str) -> Directory:
        return self.run(subdomain, lambda e: Directory(e.data.get("dbi")))

    def select_child(self, edupage: Edupage, student: Student) -> None:
        """Point a parent's session at one child (grades and meals follow it)."""
        school = self.school(student.school)
        if _account_type(edupage) != "parent" or school.selected_child == student.id:
            return
        edupage.switch_to_child(int(student.id))
        school.selected_child = student.id

    # -- data --------------------------------------------------------------------------

    def timetable(self, student: Student, start: date, end: date) -> list[dict[str, Any]]:
        def call(edupage: Edupage) -> list[dict[str, Any]]:
            body = {
                "__args": [
                    None,
                    {
                        "year": _school_year(edupage, start),
                        "datefrom": start.isoformat(),
                        "dateto": end.isoformat(),
                        "table": "students",
                        "id": student.id,
                        "showColors": True,
                        "showIgroupsInClasses": True,
                        "showOrig": True,
                        "log_module": "CurrentTTView",
                    },
                ],
                "__gsh": edupage.gsec_hash,
            }
            url = f"https://{student.school}.edupage.org/timetable/server/currenttt.js?__func=curentttGetData"
            response = edupage.session.post(url, json=body).json().get("r") or {}
            if response.get("error"):
                raise EdupageToolError(f"EduPage refused the timetable: {response['error']}")
            if "ttitems" not in response:
                raise RuntimeError("timetable response had no ttitems")
            return response["ttitems"] or []

        return self.run(student.school, call)

    def timeline(self, subdomain: str, since: date) -> dict[str, Any]:
        """The raw timeline (messages, notices, homework feed) from ``since`` onwards."""
        school = self.school(subdomain)
        key = since.isoformat()
        cached = school.timeline
        if cached and cached[0] <= key and time.monotonic() - cached[1] < TIMELINE_CACHE_SECONDS:
            return cached[2]

        def call(edupage: Edupage) -> dict[str, Any]:
            response = edupage.session.post(
                f"https://{subdomain}.edupage.org/timeline/",
                params=[("module", "todo"), ("filterTab", ""), ("akcia", "getData"),
                        ("filterTab", "messages")],
                data=RequestUtil.encode_form_data({"datefrom": key}),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            data = response.json()
            if "timelineItems" not in data:
                raise RuntimeError("timeline response had no timelineItems")
            return data

        data = self.run(subdomain, call)
        school.timeline = (key, time.monotonic(), data)
        return data

    def grades(self, student: Student, year: int | None, term: str | None) -> tuple[list, list, Directory]:
        """Numeric marks, written (text) evaluations, and the school's directory."""
        def call(edupage: Edupage) -> tuple[list, list, Directory]:
            self.select_child(edupage, student)
            if year is not None or term is not None:
                t = {"1": Term.FIRST, "first": Term.FIRST, "2": Term.SECOND,
                     "second": Term.SECOND}.get(str(term or "").lower())
                if t is None:
                    raise EdupageToolError("`term` must be 1 or 2 when `school_year` is given.")
                y = year if year is not None else edupage.get_school_year()
                grades = edupage.get_grades_for_term(y, t)
                fetch_text = lambda: edupage.get_text_grades_for_term(y, t)  # noqa: E731
            else:
                grades = edupage.get_grades()
                fetch_text = edupage.get_text_grades
            try:
                text_grades = fetch_text()
            except (TypeError, FailedToParseGradeDataError):
                # Schools without written evaluations have no `vsetkyVcelicky` block.
                text_grades = []
            return grades, text_grades, Directory(edupage.data.get("dbi"))

        return self.run(student.school, call)

    def meal_data(self, student: Student, day: date) -> dict[str, Any]:
        """The canteen's ``novyListok`` block (a week around ``day``)."""
        def call(edupage: Edupage) -> dict[str, Any]:
            self.select_child(edupage, student)
            url = f"https://{student.school}.edupage.org/menu/?date={day:%Y%m%d}"
            html = edupage.session.get(url).text
            if "edupageData: " not in html:
                raise EdupageToolError(
                    f"{student.school} has no canteen menu on EduPage (or it is not "
                    "visible to this account)."
                )
            blob = json.loads(html.split("edupageData: ", 1)[1].split(",\r\n", 1)[0])
            data = blob.get(student.school) or next(iter(blob.values()), {})
            return data.get("novyListok") or {}

        return self.run(student.school, call)

    def order_meal(self, student: Student, day: date, slot: str, choice: str, boarder: str) -> None:
        def call(edupage: Edupage) -> None:
            self.select_child(edupage, student)
            payload = {
                "stravnikid": boarder,
                "mysqlDate": day.isoformat(),
                "jids": {slot: choice},
                "view": "pc_listok",
                "pravo": "Student",
            }
            response = edupage.session.post(
                f"https://{student.school}.edupage.org/menu/",
                data={"akcia": "ulozJedlaStravnika", "jedlaStravnika": json.dumps(payload)},
            )
            try:
                error = response.json().get("error")
            except ValueError:
                error = "unexpected response"
            if error:
                raise EdupageToolError(
                    f"The canteen refused the change: {error}. The deadline may have passed."
                )

        self.run(student.school, call, retry=False)

    def substitutions(self, subdomain: str, day: date) -> dict[str, Any]:
        def call(edupage: Edupage) -> dict[str, Any]:
            out: dict[str, Any] = {"changes": [], "missing_teachers": []}
            try:
                out["changes"] = Substitution(edupage).get_timetable_changes(day) or []
            except IndexError:
                pass  # the page has no changes section that day
            try:
                out["missing_teachers"] = Substitution(edupage).get_missing_teachers(day) or []
            except (IndexError, ValueError):
                pass
            except Exception as exc:  # noqa: BLE001 - keep the changes if only this part failed
                out["missing_teachers_error"] = str(exc)
            return out

        return self.run(subdomain, call)

    def send_message(self, subdomain: str, recipients: list[str], text: str) -> int:
        def call(edupage: Edupage) -> int:
            data = RequestData.encode_request_body({
                "selectedUser": ";".join(recipients),
                "text": text,
                "attachements": "{}",
                "receipt": "0",
                "typ": "sprava",
            })
            response = edupage.session.post(
                f"https://{subdomain}.edupage.org/timeline/?=&akcia=createItem&eqav=1&maxEqav=7",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            decoded = RequestData.decode_response(response.text)
            if decoded == "0":
                raise EdupageToolError("EduPage refused to send the message.")
            changes = (json.loads(decoded) or {}).get("changes") or []
            if not changes:
                raise EdupageToolError(
                    "EduPage accepted the request but reported no new message; check the "
                    "EduPage app before trying again."
                )
            return int(changes[0].get("timelineid"))

        result = self.run(subdomain, call, retry=False)
        school = self.school(subdomain)
        school.timeline = None
        return result


# -- helpers on a logged-in Edupage ---------------------------------------------------


def _session_id(edupage: Edupage, subdomain: str) -> str | None:
    cookies = edupage.session.cookies
    return cookies.get_dict(f"{subdomain}.edupage.org").get("PHPSESSID") or cookies.get(
        "PHPSESSID"
    )


def _account_type(edupage: Edupage) -> str:
    user_id = str((edupage.data or {}).get("userid") or "")
    for prefix, kind in (("Rodic", "parent"), ("Student", "student"), ("Ucitel", "teacher")):
        if user_id.startswith(prefix):
            return kind
    return "other"


def _school_year(edupage: Edupage, day: date) -> int:
    try:
        return int(edupage.get_school_year())
    except Exception:  # noqa: BLE001
        # Slovak/Czech school years start in September.
        return day.year if day.month >= 9 else day.year - 1


def _students(edupage: Edupage, subdomain: str) -> list[Student]:
    data = edupage.data or {}
    directory = Directory(data.get("dbi"))
    kind = _account_type(edupage)
    if kind == "parent":
        ids = [str(i) for i in data.get("parentStudentids") or []]
        if not ids and data.get("parentChild"):
            ids = [str(data["parentChild"])]
    elif kind == "student":
        ids = [str(data.get("userid"))[len("Student"):]]
    else:
        ids = []

    students = []
    for sid in ids:
        row = (data.get("dbi", {}).get("students") or {}).get(sid) or {}
        first = row.get("firstname") or ""
        name = directory.student(sid) or (data.get("userrow") or {}).get("p_meno") or sid
        class_id = row.get("classid") or None
        students.append(Student(
            id=sid,
            name=name,
            first_name=first or name.split(" ")[0],
            class_id=class_id,
            class_name=directory.class_name(class_id),
            school=subdomain,
        ))
    return students


def _school_info(edupage: Edupage, subdomain: str) -> dict[str, Any]:
    data = edupage.data or {}
    try:
        year = int(edupage.get_school_year())
    except Exception:  # noqa: BLE001
        year = None
    return {
        "school": subdomain,
        "url": f"https://{subdomain}.edupage.org",
        "account_type": _account_type(edupage),
        "school_year": f"{year}/{year + 1}" if year else None,
        "school_email": data.get("school_email") or None,
    }

