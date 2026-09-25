"""MCP server for EduPage (edupage.org): timetables, homework, grades, messages and more."""

from __future__ import annotations

import os
from typing import Any, Callable

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import __version__
from .client import EdupageToolError
from .config import TRUTHY, Config, ConfigError
from .service import EdupageService

INSTRUCTIONS = """\
Read a family's school life from EduPage (edupage.org), the school system used
across Slovakia, Czechia and elsewhere: timetables, homework and exams, grades,
messages from teachers, notices, absences, school events, substitutions and
the canteen menu.

One EduPage login can cover several children at several schools. Call
`get_account` first to see who is on it. Most tools take a `student`
argument: a first name, full name, student ID or school subdomain. It can be
left out only when the account has a single student. `get_briefing` gives a
one-call overview of today and the next few days for every student.

Dates are YYYY-MM-DD, or 'today', 'tomorrow', 'yesterday', in the school's
timezone. Timestamps come back as ISO 8601 with an offset.

EduPage content is written by the school, usually in the local language
(Slovak, Czech, ...). Quote names and subjects as they are; translate or
summarise the rest for the user if that helps.

`send_message` and `order_meal` change things in EduPage. Only call them when
the user has asked for that specific change, and confirm the recipient and
text, or the day and menu, with them first.

Every response has `status`: `success`, or `error` with an `error` message
saying what to fix. Pass any `warnings` or `note` on to the user.

If a tool says EduPage wants a second factor, ask the user to approve the
login in the EduPage app (or read out the emailed code) and call
`complete_login`.
"""

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE_TOOLS = ("send_message", "order_meal")

mcp = MCPServer(
    name="edupage",
    title="EduPage",
    version=__version__,
    instructions=INSTRUCTIONS,
)

_service: EdupageService | None = None


def _get_service() -> EdupageService:
    global _service
    if _service is None:
        _service = EdupageService(Config.from_env())
    return _service


async def _run(fn: Callable[[EdupageService], dict[str, Any]]) -> dict[str, Any]:
    """Run a blocking EduPage call off the event loop, mapping errors to results."""
    try:
        service = _get_service()
        return await anyio.to_thread.run_sync(lambda: fn(service))
    except (EdupageToolError, ConfigError) as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}. EduPage may have changed its pages, or "
                     "be unavailable; try again, and report it if it keeps happening.",
        }


StudentArg = str | None


@mcp.tool(
    title="Get account overview",
    description=(
        "Who is on this EduPage login: the account type (parent or student), every "
        "school it reaches (subdomain, school year, email) and every student with "
        "their class and school. Call this first to learn the names to pass as "
        "`student` to the other tools."
    ),
    annotations=READ,
)
async def get_account() -> dict[str, Any]:
    return await _run(lambda s: s.get_account())


@mcp.tool(
    title="Get daily briefing",
    description=(
        "One-call overview for every student (or just `student`): today's lessons "
        "with changes and cancellations, homework and exams due in the next "
        "`days_ahead` days (default 3), messages and notices from the last two "
        "days, grades from the last week, and today's canteen menu. Sections that "
        "fail are listed under `warnings` instead of failing the whole call."
    ),
    annotations=READ,
)
async def get_briefing(student: StudentArg = None, days_ahead: int = 3) -> dict[str, Any]:
    return await _run(lambda s: s.get_briefing(student, days_ahead))


@mcp.tool(
    title="Get timetable",
    description=(
        "A student's lessons for `date` (default today) and the following `days` "
        "days (1-31, default 1), grouped by day. Each lesson has its period, start "
        "and end time, subject, teachers, classroom and group. Substitutions show "
        "the original lesson with `cancelled: true` next to its replacement with "
        "`changed: true`. `homework_ids` link a lesson to entries from "
        "`get_homework`. Weekends and holidays have no entries."
    ),
    annotations=READ,
)
async def get_timetable(
    student: StudentArg = None, date: str | None = None, days: int = 1
) -> dict[str, Any]:
    return await _run(lambda s: s.get_timetable(student, date, days))


@mcp.tool(
    title="Get homework and exams",
    description=(
        "Homework and announced exams (tests, quizzes, oral exams) due between "
        "`start_date` (default today) and `end_date` (default two weeks later), "
        "sorted by due date. `kind` filters to 'homework' or 'exam' (default "
        "'all'). Items marked done in EduPage are left out unless "
        "`include_done` is true. Each item has the subject, teacher, title, "
        "details, due date and any attachments."
    ),
    annotations=READ,
)
async def get_homework(
    student: StudentArg = None,
    start_date: str | None = None,
    end_date: str | None = None,
    kind: str = "all",
    include_done: bool = False,
) -> dict[str, Any]:
    return await _run(lambda s: s.get_homework(student, start_date, end_date, kind, include_done))


@mcp.tool(
    title="Get messages",
    description=(
        "Messages from teachers and the school (and chats) since `since` (default "
        "14 days ago), newest first, with replies nested under the message they "
        "answer. Each has the author, who it was addressed to (a class, parents of "
        "a class, the whole school, or you), the full text, attachments, and "
        "`confirmation_requested` when the sender asked for a read receipt. "
        "`search` filters by text, author or recipient (accent-insensitive)."
    ),
    annotations=READ,
)
async def get_messages(
    student: StudentArg = None,
    since: str | None = None,
    search: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return await _run(lambda s: s.get_messages(student, since, search, limit))


@mcp.tool(
    title="Get notifications",
    description=(
        "Everything on the student's EduPage timeline since `since` (default 7 days "
        "ago), newest first: messages, homework, grades, events, absences, "
        "payments, sign-up forms, substitutions and more. Filter with `types`, "
        "using the returned `type` or `type_code` values, e.g. ['grade', "
        "'payments_published', 'enrollment']. EduPage's own tips and bookkeeping "
        "entries are hidden unless `include_system` is true. `search` matches text, "
        "author or recipient."
    ),
    annotations=READ,
)
async def get_notifications(
    student: StudentArg = None,
    since: str | None = None,
    types: list[str] | None = None,
    search: str | None = None,
    limit: int = 50,
    include_system: bool = False,
) -> dict[str, Any]:
    return await _run(
        lambda s: s.get_notifications(student, since, types, search, limit, include_system)
    )


@mcp.tool(
    title="Get grades",
    description=(
        "A student's marks for the current term, newest first, with a per-subject "
        "summary (count and weighted average). Marks run 1 (best) to 5; points and "
        "percentage marks include `max_points` and `percent`. Filter with "
        "`subject` (name or abbreviation) and `since`. For an earlier term pass "
        "`school_year` (the starting year, e.g. 2025 for 2025/26) and `term` (1 or 2)."
    ),
    annotations=READ,
)
async def get_grades(
    student: StudentArg = None,
    subject: str | None = None,
    school_year: int | None = None,
    term: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    return await _run(lambda s: s.get_grades(student, subject, school_year, term, since))


@mcp.tool(
    title="Get absences",
    description=(
        "Absences recorded for a student since `since` (default the start of the "
        "school year): each absence with the affected periods and whether it is "
        "excused, plus excuse notes and reminders, with totals of days absent and "
        "days not yet excused."
    ),
    annotations=READ,
)
async def get_absences(student: StudentArg = None, since: str | None = None) -> dict[str, Any]:
    return await _run(lambda s: s.get_absences(student, since))


@mcp.tool(
    title="Get school events",
    description=(
        "School calendar events for a student between `start_date` (default today) "
        "and `end_date` (default 30 days later): exams, trips, excursions, "
        "school events, parents' evenings, holidays and days off, as announced on "
        "the timeline, with the subject, teachers and classes involved."
    ),
    annotations=READ,
)
async def get_events(
    student: StudentArg = None, start_date: str | None = None, end_date: str | None = None
) -> dict[str, Any]:
    return await _run(lambda s: s.get_events(student, start_date, end_date))


@mcp.tool(
    title="Get substitutions",
    description=(
        "The school's substitution plan for `date` (default today): lessons "
        "cancelled, moved or taught by someone else, and which teachers are away. "
        "Pass a student to see only their class, or a school subdomain (or "
        "`all_classes: true`) for the whole school."
    ),
    annotations=READ,
)
async def get_substitutions(
    student: StudentArg = None, date: str | None = None, all_classes: bool = False
) -> dict[str, Any]:
    return await _run(lambda s: s.get_substitutions(student, date, all_classes))


@mcp.tool(
    title="Get canteen menu",
    description=(
        "The school canteen menu for `date` (default today) and the following "
        "`days` days (1-14), per meal (breakfast, snack, lunch, ...): each menu "
        "option with its dishes, allergens and portion sizes, whether a meal is "
        "ordered and which menu, the order/cancel deadlines, and the remaining "
        "meal credit."
    ),
    annotations=READ,
)
async def get_meals(
    student: StudentArg = None, date: str | None = None, days: int = 1
) -> dict[str, Any]:
    return await _run(lambda s: s.get_meals(student, date, days))


@mcp.tool(
    title="Get school info",
    description=(
        "Facts about a school (by subdomain or a student's name): the bell "
        "schedule with each period's start and end, the school's email, each "
        "student's class with its class teachers and home classroom, and how many "
        "teachers, classes, subjects and rooms it has."
    ),
    annotations=READ,
)
async def get_school_info(school: str | None = None) -> dict[str, Any]:
    return await _run(lambda s: s.get_school_info(school))


@mcp.tool(
    title="List teachers",
    description=(
        "Teachers at a school (by subdomain or a student's name), optionally "
        "filtered by `query` (part of a name, or the abbreviation used in the "
        "timetable). Each has a `recipient_id` for `send_message`."
    ),
    annotations=READ,
)
async def list_teachers(school: str | None = None, query: str | None = None) -> dict[str, Any]:
    return await _run(lambda s: s.list_teachers(school, query))


@mcp.tool(
    title="Send message",
    description=(
        "Send a new EduPage message from this account to one or more teachers at a "
        "school (by subdomain or a student's name). `recipients` are teacher names "
        "or `recipient_id`s from `list_teachers`; a name must match exactly one "
        "teacher. The message cannot be unsent: confirm the recipients and text "
        "with the user before calling."
    ),
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
)
async def send_message(recipients: list[str], text: str, school: str | None = None) -> dict[str, Any]:
    return await _run(lambda s: s.send_message(school, recipients, text))


@mcp.tool(
    title="Order or cancel a meal",
    description=(
        "Choose a menu for a student's canteen meal on `date`, or cancel it. "
        "`meal` is 'lunch' (default), 'breakfast', 'snack', ... as returned by "
        "`get_meals`; `menu` is one of that meal's `choosable_menus` (e.g. 'A', "
        "'B') or 'cancel'. Only works before the canteen's deadline. Confirm the "
        "day and choice with the user first."
    ),
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
    ),
)
async def order_meal(
    date: str, menu: str, student: StudentArg = None, meal: str = "lunch"
) -> dict[str, Any]:
    return await _run(lambda s: s.order_meal(student, date, meal, menu))


@mcp.tool(
    title="Complete two-factor login",
    description=(
        "Finish an EduPage login that asked for a second factor. After the user "
        "approves the login in the EduPage app, call this with no `code`; if "
        "EduPage emailed a code instead, pass it as `code`. `school` is only "
        "needed if several schools are waiting."
    ),
)
async def complete_login(code: str | None = None, school: str | None = None) -> dict[str, Any]:
    return await _run(lambda s: s.complete_login(school, code))


def apply_read_only() -> None:
    """Hide the write tools when EDUPAGE_READ_ONLY is set."""
    if os.environ.get("EDUPAGE_READ_ONLY", "").strip().lower() in TRUTHY:
        for name in WRITE_TOOLS:
            try:
                mcp.remove_tool(name)
            except Exception:  # noqa: BLE001 - already removed
                pass


apply_read_only()


def main() -> None:
    """Run the server over stdio or HTTP, or verify the login with ``--check``."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="edupage-mcp", description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="log in, print the schools and students the account reaches, and exit",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=os.environ.get("MCP_TRANSPORT", "stdio").strip() or "stdio",
        help="stdio for a local client (default), http to serve over the network",
    )
    parser.add_argument("--host", help="HTTP bind address (default MCP_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, help="HTTP port (default MCP_PORT or 8765)")
    parser.add_argument("--version", action="version", version=f"edupage-mcp {__version__}")
    args = parser.parse_args()

    if args.check:
        raise SystemExit(_check())
    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    from .http_app import HttpConfig, serve

    try:
        Config.from_env()  # fail at startup, not on the first tool call
        http = HttpConfig.from_env(host=args.host, port=args.port)
    except ConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    serve(mcp, http)


def _check() -> int:
    """Log in and print what the account reaches, so setup problems surface early."""
    import sys

    from .client import TwoFactorRequired

    try:
        service = _get_service()
        try:
            account = service.client.account()
        except TwoFactorRequired as exc:
            if not sys.stdin.isatty():
                raise
            print(exc, file=sys.stderr)
            code = input("Approve in the EduPage app and press Enter, or type the emailed code: ")
            service.complete_login(None, code.strip() or None)
            account = service.client.account()
    except (EdupageToolError, ConfigError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    config = service.config
    print(f"OK: logged in as {config.username} ({account['account_type']} account)")
    print(f"Timezone: {config.timezone}" + ("   [read-only]" if config.read_only else ""))
    print(f"{len(account['schools'])} school(s):")
    for school in account["schools"]:
        print(f"  - {school['school']}  {school['url']}  {school['school_year'] or ''}".rstrip())
    print(f"{len(account['students'])} student(s):")
    for student in account["students"]:
        print(f"  - {student['name']}  class {student['class'] or '?'}  at {student['school']}")
    return 0


if __name__ == "__main__":
    main()
