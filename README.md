# edupage-mcp

An MCP server that lets an agent read a family's school life from
[EduPage](https://www.edupage.org), the school system used across Slovakia,
Czechia and beyond: timetables with substitutions, homework and exams, grades,
messages from teachers, notices, absences, school events and the canteen menu.
It can also send a message to a teacher and order or cancel school meals.

EduPage has no public API, so this builds on the community
[`edupage-api`](https://pypi.org/project/edupage-api/) library, which logs in
the way the website does. One login covers every child and every school on a
parent account; the schools are discovered automatically.

## Tools

| Tool | Input | Output |
| --- | --- | --- |
| `get_account` | — | Account type, every school (subdomain, school year, email) and every student with class and school |
| `get_briefing` | `student?`, `days_ahead?` (3) | Per student: today's lessons, homework and exams due soon, recent messages and notices, last week's grades, today's lunch |
| `get_timetable` | `student?`, `date?` (today), `days?` (1-31) | Lessons by day: period, times, subject, teachers, room, group, `changed` / `cancelled` |
| `get_homework` | `student?`, `start_date?`, `end_date?`, `kind?` (`all`/`homework`/`exam`), `include_done?` | Homework and announced exams by due date, with subject, teacher, details, attachments, done state |
| `get_messages` | `student?`, `since?` (14 days), `search?`, `limit?` | Messages and chats, newest first, replies nested, attachments, read-receipt requests |
| `get_notifications` | `student?`, `since?` (7 days), `types?`, `search?`, `limit?`, `include_system?` | The whole timeline: grades, homework, events, absences, payments, sign-up forms, substitutions... |
| `get_grades` | `student?`, `subject?`, `school_year?`, `term?`, `since?` | Marks with weight, points, class average, teacher; per-subject weighted averages; written evaluations as `text_grades` |
| `get_absences` | `student?`, `since?` (school year start) | Absences with periods and excused state, excuse notes, day totals |
| `get_events` | `student?`, `start_date?`, `end_date?` (30 days) | Trips, exams, school events, holidays and days off |
| `get_substitutions` | `student?` or school, `date?`, `all_classes?` | The substitution plan for the student's class (or the whole school) and missing teachers |
| `get_meals` | `student?`, `date?`, `days?` (1-14) | Canteen menus per meal with dishes, allergens and portions; what is ordered; deadlines; credit |
| `get_school_info` | `school?` | Bell schedule, school email, the student's class teachers and home room |
| `list_teachers` | `school?`, `query?` | Teachers with the `recipient_id` for `send_message` |
| `send_message` ✏️ | `recipients`, `text`, `school?` | Sends a new message to one or more teachers |
| `order_meal` ✏️ | `date`, `menu` (`A`, `B`, ... or `cancel`), `student?`, `meal?` (`lunch`) | Orders, changes or cancels a canteen meal before the deadline |
| `complete_login` | `code?`, `school?` | Finishes a login that EduPage wanted a second factor for |

✏️ changes something in EduPage. Set `EDUPAGE_READ_ONLY=true` to remove these
two tools entirely.

`student` is a first name, full name, student ID or school subdomain, matched
without regard to case or accents; it can be left out when the account has a
single student. Dates are `YYYY-MM-DD`, or `today`, `tomorrow`, `yesterday`.

## Setup

### 1. Install and configure

You need [uv](https://docs.astral.sh/uv/getting-started/installation/); it
fetches Python 3.12+ for you if necessary.

```bash
git clone https://github.com/frizzy/edupage-mcp.git
cd edupage-mcp
cp .env.example .env   # then fill it in
uv sync
```

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `EDUPAGE_USERNAME` | yes | — | The username you sign in to EduPage with (usually an email) |
| `EDUPAGE_PASSWORD` | yes | — | Your EduPage password |
| `EDUPAGE_SUBDOMAINS` | no | discovered | Comma-separated schools, e.g. `myschool,otherschool` for `myschool.edupage.org` |
| `EDUPAGE_TIMEZONE` | no | `Europe/Bratislava` | IANA timezone that "today" is judged in |
| `EDUPAGE_READ_ONLY` | no | `false` | `true` hides `send_message` and `order_meal` |
| `EDUPAGE_SESSION_FILE` | no | — | Keep sessions in this file (mode 600) so a two-factor login survives restarts |
| `EDUPAGE_SESSION_TTL_MINUTES` | no | `20` | How old a session may get before it is refreshed |
| `EDUPAGE_REQUEST_TIMEOUT` | no | `20` | Seconds to wait for EduPage |

Without `EDUPAGE_SUBDOMAINS`, the server logs in through
`portal.edupage.org` and then finds every school a parent account belongs to.
Set it if your account only works at one school's address, or to limit the
agent to some of your schools.

### 2. Verify the login

```bash
set -a && source .env && set +a
uv run edupage-mcp --check
```

This logs in, prints what the account reaches and exits, so problems surface
before you wire anything up:

```
OK: logged in as you@example.com (parent account)
Timezone: Europe/Bratislava
2 school(s):
  - myschool  https://myschool.edupage.org  2026/2027
  - otherschool  https://otherschool.edupage.org  2026/2027
2 student(s):
  - Jana Nováková  class IV.A  at myschool
  - Peter Novák  class I.A  at otherschool
```

If EduPage asks for a second factor, `--check` waits for you to approve the
login in the EduPage app (or to type the emailed code).

### 3. Register with the agent

By default the server speaks MCP over stdio, run on the same machine as the
client (to host it on another machine, see
[Running on a Raspberry Pi](#running-on-a-raspberry-pi)). Replace
`/path/to/edupage-mcp` with the directory you cloned into. For Claude Code:

```bash
claude mcp add edupage \
  --env EDUPAGE_USERNAME=you@example.com \
  --env EDUPAGE_PASSWORD=your-edupage-password \
  -- uv --directory /path/to/edupage-mcp run edupage-mcp
```

Or, as raw config for any host that takes the standard `mcpServers` shape:

```json
{
  "mcpServers": {
    "edupage": {
      "command": "uv",
      "args": ["--directory", "/path/to/edupage-mcp", "run", "edupage-mcp"],
      "env": {
        "EDUPAGE_USERNAME": "you@example.com",
        "EDUPAGE_PASSWORD": "your-edupage-password"
      }
    }
  }
}
```

Then ask things like *"What does Jana have tomorrow, and is any homework
due?"*, *"Summarise this week's messages from school in English"* or *"Did
anyone get a grade today?"*.

### Hermes Agent

[Hermes Agent](https://hermes-agent.nousresearch.com) reads MCP servers from
`mcp_servers` in `~/.hermes/config.yaml`. It gives a stdio server only the
environment variables listed under its `env`, so the credentials have to be
listed there. Keep the values themselves in `~/.hermes/.env`:

```bash
# ~/.hermes/.env
EDUPAGE_USERNAME=you@example.com
EDUPAGE_PASSWORD=your-edupage-password
```

Then add **one** of these to `~/.hermes/config.yaml`.

From a local checkout (if Hermes can't find `uv`, use the full path that
`command -v uv` prints):

```yaml
mcp_servers:
  edupage:
    command: "uv"
    args: ["--directory", "/path/to/edupage-mcp", "run", "edupage-mcp"]
    env:
      EDUPAGE_USERNAME: "${EDUPAGE_USERNAME}"
      EDUPAGE_PASSWORD: "${EDUPAGE_PASSWORD}"
```

With [Docker](#running-with-docker), and nothing to install:

```yaml
mcp_servers:
  edupage:
    command: "docker"
    args: ["run", "-i", "--rm", "-e", "EDUPAGE_USERNAME", "-e", "EDUPAGE_PASSWORD",
           "ghcr.io/frizzy/edupage-mcp:latest"]
    env:
      EDUPAGE_USERNAME: "${EDUPAGE_USERNAME}"
      EDUPAGE_PASSWORD: "${EDUPAGE_PASSWORD}"
```

Over HTTP, to an always-on server (the [Pi service](#running-on-a-raspberry-pi)
or [Docker Compose](#always-on-over-http)), with `EDUPAGE_MCP_TOKEN=<token>`
added to `~/.hermes/.env`:

```yaml
mcp_servers:
  edupage:
    url: "http://127.0.0.1:8766/mcp"
    headers:
      Authorization: "Bearer ${EDUPAGE_MCP_TOKEN}"
```

Check the connection with `hermes mcp test edupage`; it should list sixteen
tools (fourteen when read-only). Then run `/reload-mcp` in an open chat, or
start a new one. The tools show up as `mcp__edupage__get_briefing` and so on.
A daily cron job that calls `get_briefing` replaces a hand-written summary
script.

To give the agent read access only, set `EDUPAGE_READ_ONLY: "true"` under
`env` (or in the service's env file); the two write tools then disappear.

## Running with Docker

A multi-arch image (amd64 and arm64, so a Raspberry Pi works too) is published
as `ghcr.io/frizzy/edupage-mcp`. To build it yourself instead, run
`docker build -t edupage-mcp .` in a checkout and use that name below.

Check your credentials first, using a `.env` file filled in from
[`.env.example`](.env.example):

```bash
docker run --rm -it --env-file .env ghcr.io/frizzy/edupage-mcp --check
```

### As a stdio server

The agent starts a fresh container for each session and talks to it over
stdin/stdout, so `-i` is required. A bare `-e NAME` forwards that variable from
the agent's own environment, which keeps the password out of the command line.
For Claude Code:

```bash
claude mcp add edupage \
  --env EDUPAGE_USERNAME=you@example.com \
  --env EDUPAGE_PASSWORD=your-edupage-password \
  -- docker run -i --rm -e EDUPAGE_USERNAME -e EDUPAGE_PASSWORD ghcr.io/frizzy/edupage-mcp
```

### Always on, over HTTP

[`docker-compose.yml`](docker-compose.yml) runs the streamable-HTTP server with
a restart policy, a health check, a read-only filesystem and a small volume
for cached sessions:

```bash
cp .env.example .env                                  # fill in your credentials
echo "MCP_AUTH_TOKEN=$(openssl rand -hex 32)" >> .env
docker compose up -d
```

It listens on `http://127.0.0.1:8766/mcp` on this machine only. Connect with the
header `Authorization: Bearer <MCP_AUTH_TOKEN>`, just as for the
[Pi service](#running-on-a-raspberry-pi). To open it to your network, change
the port mapping to `"8766:8766"`; the security notes for the Pi apply. Use
`docker compose logs -f` for logs, and
`docker compose pull && docker compose up -d` to update.

## Running on a Raspberry Pi

The same server can run as an always-on service on a Pi (or any systemd Linux
box), speaking MCP's streamable-HTTP transport and guarded by a bearer token.
By default it listens on `127.0.0.1` only, for an agent harness running on the
Pi itself; `--lan` opens it to your home network or Tailscale instead. It uses
port 8766, so it can run alongside
[icloud-calendar-mcp](https://github.com/frizzy/icloud-calendar-mcp) on 8765.

**Requirements on the Pi:** 64-bit Raspberry Pi OS (Bookworm or later),
SSH access from your machine, `rsync`, and [uv](https://docs.astral.sh/uv/):

```bash
ssh pi@raspberrypi.local 'curl -LsSf https://astral.sh/uv/install.sh | sh'
```

**Deploy** from this directory on your machine:

```bash
deploy/deploy.sh pi@raspberrypi.local          # agent on the Pi itself
deploy/deploy.sh --lan pi@raspberrypi.local    # clients elsewhere on the network
```

That copies the project to `~/edupage-mcp` on the Pi (never `.git` or `.env`)
and runs `deploy/install.sh` there, which:

1. installs the dependencies into a local `.venv` with `uv sync --frozen`;
2. on the first run, creates `/etc/edupage-mcp.env` (root-only, mode 600) from
   your local `.env` — sent once and then deleted on the Pi — or by prompting
   if you have none, and generates a random `MCP_AUTH_TOKEN`;
3. checks the EduPage login with `--check`;
4. installs and starts a hardened `edupage-mcp` systemd service on port 8766,
   with sessions cached in `/var/lib/edupage-mcp`, and prints the URL and
   token to give your agent.

Run the same command again to deploy an update; the configuration is kept.
`--lan` only matters on the first install; to switch afterwards, change
`MCP_HOST` in the env file (`127.0.0.1` or `0.0.0.0`) and restart.

**Connect the agent.** Any MCP client that supports streamable HTTP needs two
things: the URL `http://127.0.0.1:8766/mcp` (or `http://raspberrypi.local:8766/mcp`
with `--lan`) and the header `Authorization: Bearer <token>`. For Claude Code:

```bash
claude mcp add --transport http edupage http://127.0.0.1:8766/mcp \
  --header "Authorization: Bearer <token>"
```

For clients that only launch local commands (such as Claude Desktop's
`mcpServers` config), bridge with
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote):

```json
{
  "mcpServers": {
    "edupage": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://raspberrypi.local:8766/mcp", "--allow-http",
               "--header", "Authorization:Bearer <token>"]
    }
  }
}
```

**Day to day, on the Pi:**

| Task | Command |
| --- | --- |
| Logs | `sudo journalctl -u edupage-mcp -f` |
| Status | `systemctl status edupage-mcp` |
| Change settings or rotate the token | `sudo nano /etc/edupage-mcp.env && sudo systemctl restart edupage-mcp` |
| Health check (no token needed) | `curl http://127.0.0.1:8766/healthz` |
| Uninstall | `sudo systemctl disable --now edupage-mcp && sudo rm /etc/systemd/system/edupage-mcp.service /etc/edupage-mcp.env && sudo rm -rf /var/lib/edupage-mcp && rm -rf ~/edupage-mcp` |

**Security notes.** The token guards your EduPage account: anyone holding it
can read your children's grades, messages and absences, and (unless
read-only) send messages as you. On the default localhost bind nothing off the
Pi can connect, and the token keeps other local processes out. With `--lan`,
traffic is plain HTTP, so keep it on a network you trust or on Tailscale
(which encrypts it); don't port-forward it to the internet. To listen only on
the tailnet, set `MCP_HOST` to the Pi's Tailscale IP. `MCP_ALLOWED_HOSTS`
optionally restricts accepted `Host` headers as a DNS-rebinding defence. The
server refuses to bind beyond loopback without a token of at least 32
characters.

You can also run HTTP mode by hand anywhere:

```bash
MCP_AUTH_TOKEN=$(openssl rand -hex 32) uv run edupage-mcp --transport http --host 0.0.0.0
```

## Two-factor login

If the account has two-factor authentication on, a login pauses until it is
confirmed. The tool that triggered it returns an error saying so; approve the
login in the EduPage app and have the agent call `complete_login`, or tell the
agent the code EduPage emailed and it passes it as `code`. `--check` does the
same interactively.

Sessions are refreshed in place rather than by logging in again, so a
confirmed login keeps working while the server runs. Set
`EDUPAGE_SESSION_FILE` (the Pi service and Docker Compose do) to keep it
across restarts too.

## How it works

- **Sessions.** Each school is a separate EduPage session. The server logs in
  on first use, refreshes a session older than `EDUPAGE_SESSION_TTL_MINUTES`,
  and retries a failed read once with a fresh session. Writes are never
  retried, so a message is never sent twice.
- **Several children.** A parent's timeline mixes every child at a school.
  Items addressed only to another child (their class, their courses) are
  filtered out, and the session is switched to the right child before
  reading grades or the canteen.
- **Where the data comes from.** Timetables come from EduPage's timetable
  service, one request for the whole range. Messages, notices, homework,
  events and absences come from the timeline, fetched fresh (and cached for a
  minute so `get_briefing` doesn't repeat itself). Grades and substitutions
  come from `edupage-api`; the canteen from the menu page.
- **Times.** EduPage stores school-local times without an offset; they come
  back as ISO 8601 in `EDUPAGE_TIMEZONE`.
- **Language.** EduPage content is whatever the school wrote, usually Slovak
  or Czech. Field names are in English; the agent can translate the rest.

Every tool returns `{"status": "error", "error": "..."}` rather than raising, so
the agent can read the problem and correct itself: missing credentials, a
rejected password or a captcha, an ambiguous student or teacher name (with the
choices listed), a date it cannot read, a meal past its deadline.

### Limitations

- `get_absences` is built from the absence and excuse notices on the
  timeline, not EduPage's attendance page, so it can miss entries a school
  records without a notice.
- `get_events` lists events that were announced on the timeline; EduPage's
  full calendar can hold more.
- Replying inside an existing message thread, excusing an absence, signing
  forms and uploading attachments are not supported yet.
- `send_message` and `order_meal` follow the requests the EduPage web app
  makes; canteens differ, so check the result in the app the first time.
- EduPage can change its pages at any time. Anything that stops parsing
  shows up as a tool error rather than wrong data.

## Development

```bash
uv run pytest
```

The suite uses a fake `Edupage` object and invented JSON shaped like
EduPage's, so nothing touches the network. It covers configuration, school
discovery, student and teacher name matching, sibling filtering, session retry
and two-factor login, the session cache, every parser (timetable, timeline,
homework, events, absences, meals, grades), the service logic behind each
tool, the MCP tool schemas and read-only mode, and the HTTP bearer-token
middleware.

## Layout

```
src/edupage_mcp/
  server.py       MCP tool definitions and CLI entry point
  service.py      What each tool does: filtering, windows, summaries
  client.py       EduPage sessions, school discovery, the raw requests
  parsing.py      EduPage JSON -> plain dicts
  http_app.py     Streamable-HTTP transport with bearer-token auth
  config.py       Environment configuration
Dockerfile        Container image (stdio by default)
docker-compose.yml  Always-on HTTP server in Docker
deploy/
  deploy.sh       Copy to a Pi over SSH and install/update there
  install.sh      Install or update the systemd service (runs on the Pi)
  edupage-mcp.service  systemd unit template
```

`EdupageService` is importable on its own if you want the EduPage logic
without the MCP layer.

## License

[MIT](LICENSE). Not affiliated with EduPage or asc Applied Software
Consultants.
