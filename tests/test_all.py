"""Test suite. Runs with plain `python tests/test_all.py` (no pytest needed).

Covers the parts where a bug would quietly corrupt the vault or the schedule:
frontmatter round-tripping, date extraction, LLM-output coercion, the planner's
working-hours arithmetic, and the .ics wire format.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import zlib
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sb import (  # noqa: E402
    ask as askmod,
    cards as cardsmod,
    extract,
    frontmatter,
    fsrs,
    generate,
    index as idxmod,
    parser,
    taxonomy,
    tutor,
    workflow,
)
from sb.calsync import events as calevents  # noqa: E402
from sb.calsync.ics import render  # noqa: E402
from sb.calsync import gtasks  # noqa: E402
from sb.config import Config, PlannerConfig  # noqa: E402
from sb.engine import Engine  # noqa: E402
from sb.models import (  # noqa: E402
    AreaSchedule,
    Bucket,
    Cadence,
    HabitMeta,
    Note,
    ProjectMeta,
    Step,
)
from sb.vault import Vault  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILED.append(name)


def section(title):
    print(f"\n{title}")


# --------------------------------------------------------------------------


def test_frontmatter():
    section("frontmatter")
    text = "---\ntitle: Hello\ntags:\n  - a\n---\n\nBody line\n"
    meta, body = frontmatter.parse(text)
    check("parses meta", meta == {"title": "Hello", "tags": ["a"]}, meta)
    check("parses body", body.strip() == "Body line", repr(body))

    round_tripped = frontmatter.parse(frontmatter.dump(meta, body))
    check("round trips", round_tripped[0] == meta and round_tripped[1].strip() == "Body line")

    check("no frontmatter is safe", frontmatter.parse("just text")[0] == {})
    check("unterminated fence is safe", frontmatter.parse("---\nbroken")[0] == {})


def test_dates():
    section("date extraction")
    today = dt.date(2026, 8, 22)  # a Saturday
    cases = [
        ("finish by 2026-09-01", dt.date(2026, 9, 1)),
        ("due tomorrow", dt.date(2026, 8, 23)),
        ("in 3 days", dt.date(2026, 8, 25)),
        ("in 2 weeks", dt.date(2026, 9, 5)),
        ("by monday", dt.date(2026, 8, 24)),
        # "next" skips a week past the coming occurrence — lj's call, and the
        # fix for both phrasings landing on the same day.
        ("next friday", dt.date(2026, 9, 4)),
        ("this friday", dt.date(2026, 8, 28)),
        ("sept 14 deadline", dt.date(2026, 9, 14)),
        ("read a book", None),
    ]
    for text, want in cases:
        got = extract.parse_deadline(text, today)
        check(f"{text!r} -> {want}", got == want, f"got {got}")

    check("past month rolls forward", extract.parse_deadline("jan 5", today) == dt.date(2027, 1, 5))
    check("duration hours", extract.parse_duration_minutes("about 3 hours") == 180)
    check("duration mixed", extract.parse_duration_minutes("1h 30m") == 90)
    check("duration absent", extract.parse_duration_minutes("some work") is None)
    check("learning detected", extract.is_learning("learn rust generics"))
    check("non-learning", not extract.is_learning("file my taxes"))
    check("level rises", extract.guess_level("advanced internals deep dive") > extract.guess_level("quick intro"))


def test_steps_and_prior():
    section("rule-based prior")
    text = "Learn Rust generics by next Friday\n- read ch.10\n- do exercises\n- write a demo"
    prior = extract.project_prior(text, dt.date(2026, 8, 22))
    check("steps from bullets", len(prior["steps"]) == 3, prior["steps"])
    check("deadline found", prior["deadline"] == dt.date(2026, 9, 4), prior["deadline"])
    check("learning flag", prior["learning"] is True)
    check("estimate scales with steps", prior["estimate_minutes"] >= 90, prior["estimate_minutes"])

    steps = extract.extract_steps("do the thing then test it then ship it")
    check("then-chains split", len(steps) == 3, steps)

    check("skills drop weekday noise", "Friday" not in prior["skills"], prior["skills"])

    titles = [
        ("Learn Rust generics by next Friday, about 4 hours", "Learn Rust generics"),
        ("Ship the invoice script by 2026-08-26", "Ship the invoice script"),
        ("Study for the systems design interview in 2 weeks", "Study for the systems design interview"),
        ("todo: file the tax return", "File the tax return"),
        ("Read DDIA", "Read DDIA"),
        ("by friday", "By friday"),  # over-trim guard keeps the original line
    ]
    for raw, want in titles:
        got = extract.derive_title(raw)
        check(f"title {raw[:32]!r}", got == want, f"got {got!r}")


def test_coercion():
    section("LLM output coercion")
    today = dt.date(2026, 8, 22)
    prior = extract.project_prior("learn x by next friday", today)

    bad = {"deadline": "next Friday", "level": "high", "estimate_minutes": "lots",
           "steps": ["a", {"text": "b", "minutes": "45"}], "skills": "rust"}
    meta = parser._merge(bad, prior, today)
    check("unparseable deadline falls back to prior", meta.deadline == prior["deadline"], meta.deadline)
    check("bad level falls back", 1 <= meta.level <= 5, meta.level)
    check("mixed step shapes", len(meta.steps) == 2, meta.steps)
    check("string skills wrapped", meta.skills == ["rust"], meta.skills)
    check("estimate from step sum", meta.estimate_minutes == sum(s.minutes for s in meta.steps))

    hallucinated = {"deadline": "1999-01-01", "steps": [{"text": "x", "minutes": 30}]}
    meta2 = parser._merge(hallucinated, prior, today)
    check("past date rejected", meta2.deadline == prior["deadline"], meta2.deadline)

    empty = parser._merge({"steps": []}, prior, today)
    check("no steps -> prior used", empty.deadline == prior["deadline"])

    from sb.llm._json import extract as jx
    check("fenced json", jx('```json\n{"a": 1}\n```')["a"] == 1)
    check("prose-wrapped json", jx('Sure! {"a": 2} hope that helps')["a"] == 2)
    check("trailing comma repaired", jx('{"a": 3,}')["a"] == 3)


def test_planner():
    section("planner")
    cfg = PlannerConfig(work_start="09:00", work_end="17:00", max_minutes_per_day=120,
                        workdays=[0, 1, 2, 3, 4], block_gap_minutes=0)
    note = Note(id="t1", title="T", bucket=Bucket.PROJECT,
                project=ProjectMeta(steps=[Step(id=f"s{i}", text=f"step {i}", minutes=60) for i in range(5)]))
    start = dt.datetime(2026, 8, 24, 10, 0).astimezone()  # Monday
    report = workflow.plan_project(note, cfg, start_from=start)
    slots = [s.scheduled for s in note.project.steps]

    check("all steps scheduled", report.scheduled == 5 and all(slots))
    check("starts at cursor", slots[0] == start, slots[0])
    check("respects daily cap", len({s.date() for s in slots}) == 3, [s.isoformat() for s in slots])
    check("inside working hours", all(cfg.start_time() <= s.time() <= cfg.end_time() for s in slots))
    check("skips weekends", all(s.weekday() in cfg.workdays for s in slots), [s.strftime('%a') for s in slots])
    check("monotonic", slots == sorted(slots))

    # weekend start rolls to Monday
    sat = dt.datetime(2026, 8, 22, 10, 0).astimezone()
    note2 = Note(id="t2", title="T2", bucket=Bucket.PROJECT,
                 project=ProjectMeta(steps=[Step(id="s1", text="x", minutes=30)]))
    workflow.plan_project(note2, cfg, start_from=sat)
    check("weekend start moves to Monday", note2.project.steps[0].scheduled.weekday() == 0)

    # idempotence: replanning without force leaves future slots alone
    before = list(slots)
    workflow.plan_project(note, cfg, start_from=start)
    check("replan is stable", [s.scheduled for s in note.project.steps] == before)

    # a second project must not double-book the first project's blocks
    other = Note(id="t3", title="T3", bucket=Bucket.PROJECT,
                 project=ProjectMeta(steps=[Step(id="s1", text="y", minutes=60)]))
    busy = [(s.scheduled, s.minutes) for s in note.project.steps]
    workflow.plan_project(other, cfg, start_from=start, busy=busy)
    got = other.project.steps[0].scheduled
    overlaps = any(b < got + dt.timedelta(minutes=60) and got < b + dt.timedelta(minutes=m)
                   for b, m in busy)
    check("no cross-project double-booking", not overlaps, got.isoformat())


def test_urgency_and_queue():
    section("urgency and queue")
    def mk(nid, days, steps_done=0, total=3):
        steps = [Step(id=f"s{i}", text=f"s{i}", minutes=60, done=i < steps_done) for i in range(total)]
        return Note(id=nid, title=nid, bucket=Bucket.PROJECT,
                    project=ProjectMeta(deadline=dt.date.today() + dt.timedelta(days=days), steps=steps))

    soon, later, overdue = mk("soon", 1), mk("later", 30), mk("overdue", -2)
    check("overdue is maximal", workflow.urgency(overdue) == 1.0)
    check("soon beats later", workflow.urgency(soon) > workflow.urgency(later))
    check("no deadline is low", workflow.urgency(Note(id="n", title="n", project=ProjectMeta())) < 0.3)

    q = workflow.next_actions([later, soon, overdue])
    check("queue ranked", [a.note_id for a in q] == ["overdue", "soon", "later"], [a.note_id for a in q])
    check("one step per project", len(q) == 3)
    done_note = mk("done", 5, steps_done=3, total=3)
    check("finished projects drop out", not workflow.next_actions([done_note]))


def test_ics():
    section("ics output")
    cfg = Config(vault=Path("/tmp/x"))
    far = dt.date.today() + dt.timedelta(days=40)
    note = Note(id="n1", title="Learn, Rust; generics", bucket=Bucket.PROJECT,
                project=ProjectMeta(deadline=far, level=4,
                                    steps=[Step(id="s1", text="Read ch.10", minutes=45,
                                                scheduled=dt.datetime(2026, 8, 25, 9, 0).astimezone())]))
    evs = calevents.events_for_note(note, cfg)
    check("deadline is not an event", [e.kind for e in evs] == ["block"], [e.kind for e in evs])
    tasks = calevents.tasks_for_note(note, cfg)
    check("deadline is a task", len(tasks) == 1 and tasks[0].due == far)

    out = render(evs, cfg, tasks)
    check("has calendar wrapper", out.startswith("BEGIN:VCALENDAR") and out.rstrip().endswith("END:VCALENDAR"))
    check("crlf line endings", "\r\n" in out and "\n\n" not in out)
    check("escapes commas/semicolons", "Learn\\, Rust\\; generics" in out)
    check("no all-day deadline event", "DTSTART;VALUE=DATE:" not in out)
    check("vtodo emitted", "BEGIN:VTODO" in out and "END:VTODO" in out)
    check("vtodo has due date", f"DUE;VALUE=DATE:{far:%Y%m%d}" in out)
    check("vtodo has status", "STATUS:NEEDS-ACTION" in out)
    check("vtodo has priority", "PRIORITY:" in out)
    check("vtodo uid stable", "UID:n1-due@secondbrain.local" in out)
    check("timed block", "DTSTART:20260825T090000" in out)
    check("event alarm present", "BEGIN:VALARM" in out and "TRIGGER:-PT10M" in out)
    check("task alarm hangs off due", "TRIGGER;RELATED=END:-PT1440M" in out)
    check("stable uid", "UID:n1-s1@secondbrain.local" in out)
    check("colour emitted", "COLOR:dodgerblue" in out, out[out.find("COLOR:"):][:40])
    check("category emitted", "X-SB-CATEGORY:study" in out)
    for line in out.split("\r\n"):
        if len(line.encode()) > 75 and not line.startswith(" "):
            check("line folding", False, line[:60])
            break
    else:
        check("line folding", True)

    done = Note(id="n2", title="x", bucket=Bucket.PROJECT,
                project=ProjectMeta(steps=[Step(id="s1", text="x", done=True,
                                                scheduled=dt.datetime.now().astimezone())]))
    check("completed steps excluded", not calevents.events_for_note(done, cfg))

    off = Config(vault=Path("/tmp/x"))
    off.calendar.task_sink = "none"
    check("task_sink none drops todos", "BEGIN:VTODO" not in render(evs, off, []))

    auto = Config(vault=Path("/tmp/x"))
    check("auto follows ics sink", auto.resolved_task_sink() == "ics")
    auto.calendar.sink = "both"
    check("auto follows both sink", auto.resolved_task_sink() == "both")
    auto.calendar.sink = "google"
    check("auto follows google sink", auto.resolved_task_sink() == "google")
    auto.calendar.task_sink = "ics"
    check("explicit task sink overrides auto", auto.resolved_task_sink() == "ics")


def test_taxonomy():
    section("keyword colours")
    cfg = Config(vault=Path("/tmp/x"))
    cases = {
        "Study for the calc quiz on Friday": "quiz",
        "Finish the orgo problem set": "hw",
        "Read chapter 10 of the textbook": "study",
        "Gym — leg day": "health",
        "Laundry and dishes": "chore",
        "Movie night with friends": "fun",
        "Pay the rent": "finance",
        "Renew my license": "admin",
        "Something entirely unremarkable": "general",
    }
    for text, expected in cases.items():
        got = taxonomy.detect(title=text, cfg=cfg)
        check(f"detect {expected}", got == expected, f"{text!r} -> {got}")

    check("tag beats body",
          taxonomy.detect(title="notes", body="gym workout run", tags=["hw"], cfg=cfg) == "hw")
    check("title beats body",
          taxonomy.detect(title="Quiz Friday", body="laundry dishes trash", cfg=cfg) == "quiz")
    check("hyphenated keyword", taxonomy.detect(title="p-set due", cfg=cfg) == "hw")
    check("no substring false positive",
          taxonomy.detect(title="Restudying is not a word", cfg=cfg) != "study")
    # Body only breaks ties the title left open — "study for the quiz" reads
    # as a quiz on its own, and as a study block once the steps say so.
    check("title tie broken by body",
          taxonomy.detect(title="Study for the quiz",
                          body="reread ch 4\npractice questions", cfg=cfg) == "study")

    # Regression sweep. A note body is *rendered by this system* — it carries
    # "· due 2026-08-28 ·", a "## Steps" heading, "No due date". Any of those
    # words in the keyword table means the system matches its own boilerplate
    # and paints the whole vault one colour. A neutral note must stay neutral.
    import tempfile as _tf
    with _tf.TemporaryDirectory() as _tmp:
        bcfg = Config(vault=Path(_tmp) / "v"); bcfg.llm.provider = "heuristic"
        be = Engine(bcfg)
        pid = be.capture("zzz qqq by next Friday, about 2 hours\n- alpha bravo\n- charlie delta",
                         "project")["note"]["id"]
        aid = be.capture("wibble wobble", "area")["note"]["id"]
        be.set_schedule(aid, time="06:30", duration_minutes=45, days=[0, 2, 4])
        for label, nid in (("project", pid), ("area", aid)):
            got = taxonomy.detect(title="zzz qqq", body=be.note(nid).body, cfg=cfg)
            check(f"rendered {label} body stays neutral", got == "general", got)

    note = Note(id="x", title="Gym session", category="fun")
    check("manual override wins", taxonomy.categorize(note, cfg) == "fun")
    note.category = "not-a-real-category"
    check("bogus override falls back to detection", taxonomy.categorize(note, cfg) == "health")

    check("emoji prefix applied", taxonomy.decorate("Quiz", "quiz", cfg).startswith("📝"))
    check("emoji prefix idempotent",
          taxonomy.decorate(taxonomy.decorate("Quiz", "quiz", cfg), "quiz", cfg).count("📝") == 1)
    check("general gets no emoji", taxonomy.decorate("Thing", "general", cfg) == "Thing")

    ext = Config(vault=Path("/tmp/x"))
    ext.calendar.categories = {"hw": {"keywords": ["orgo lab"]},
                               "church": {"emoji": "⛪", "keywords": ["church", "service"]}}
    check("config extends keywords", taxonomy.detect(title="orgo lab writeup", cfg=ext) == "hw")
    check("config adds a category", taxonomy.detect(title="church service", cfg=ext) == "church")
    check("colours are unique",
          len({c.google_color_id for c in taxonomy.table(cfg)}) == len(taxonomy.table(cfg)))


def test_areas_recur():
    section("areas recur, never fall due")
    cfg = Config(vault=Path("/tmp/x"))
    area = Note(id="a1", title="Gym", bucket=Bucket.AREA,
                habit=HabitMeta(cadence=Cadence.WEEKLY, target_count=3),
                schedule=AreaSchedule(time="07:30", duration_minutes=45))

    check("area has no task", calevents.tasks_for_note(area, cfg) == [])
    evs = calevents.events_for_note(area, cfg)
    check("area is one recurring event", len(evs) == 1 and evs[0].kind == "area")
    ev = evs[0]
    check("rrule spreads 3x/week", ev.rrule == "FREQ=WEEKLY;BYDAY=MO,WE,FR", ev.rrule)
    check("timed, not all-day", ev.all_day is False and ev.minutes == 45)
    check("starts at the set time", ev.start.strftime("%H:%M") == "07:30")
    check("health colour", ev.category == "health")

    area.schedule.days = [1, 3]
    check("explicit days pin the series",
          calevents.rrule_for(area.schedule, Cadence.WEEKLY, area.habit) == "FREQ=WEEKLY;BYDAY=TU,TH")

    area.schedule.until = dt.date.today() + dt.timedelta(days=30)
    check("until is floating local",
          calevents.rrule_for(area.schedule, Cadence.WEEKLY, area.habit).endswith("T235959"))

    daily = AreaSchedule(time="06:00")
    check("daily rrule", calevents.rrule_for(daily, Cadence.DAILY) == "FREQ=DAILY")
    monthly = AreaSchedule(time="09:00", monthday=12)
    check("monthly rrule",
          calevents.rrule_for(monthly, Cadence.MONTHLY) == "FREQ=MONTHLY;BYMONTHDAY=12")

    area.schedule.enabled = False
    check("paused area emits nothing", calevents.events_for_note(area, cfg) == [])

    area.schedule.enabled = True
    area.schedule.days = []
    area.schedule.until = None
    win_start = dt.datetime.now().astimezone()
    occ = calevents.occurrences(area, cfg, win_start, win_start + dt.timedelta(days=14))
    check("two weeks of a 3x habit is ~6 blocks", 5 <= len(occ) <= 7, len(occ))
    check("occurrences carry duration", all(m == 45 for _, m in occ))

    ics = render(calevents.events_for_note(area, cfg), cfg, [])
    check("rrule reaches the ics", "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR" in ics)
    check("area is opaque time", "TRANSP:OPAQUE" in ics)

    # the weekly "option to change"
    review = calevents.schedule_review_events([area], cfg)
    check("schedule review exists", len(review) == 1)
    check("schedule review recurs weekly", review[0].rrule.startswith("FREQ=WEEKLY;BYDAY=SU"))
    check("schedule review lists the area", "Gym" in review[0].description)
    check("no areas, no review", calevents.schedule_review_events([], cfg) == [])


def test_google_token_validation():
    section("google token validation (no network)")
    from sb.calsync import _google_auth as ga

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        creds = root / "credentials.json"
        token = root / "token.json"
        CID = "282723140113-current.apps.googleusercontent.com"
        creds.write_text(json.dumps({"installed": {"client_id": CID, "client_secret": "s"}}))

        check("missing token is a reason, not a crash",
              "no token yet" in (ga.usable_token(token, creds) or ""))

        def write(**over):
            base = {"token": "t", "refresh_token": "r", "client_id": CID,
                    "client_secret": "s", "scopes": list(ga.SCOPES)}
            base.update(over)
            token.write_text(json.dumps(base))

        write()
        check("a good token is usable", ga.usable_token(token, creds) is None)

        # lj's failure #1: token predates the tasks scope. The library stamps
        # the scopes you *ask for* onto the object, so creds.has_scopes() says
        # yes and the token is used anyway — this check reads the file instead.
        write(scopes=["https://www.googleapis.com/auth/calendar"])
        r = ga.usable_token(token, creds)
        check("calendar-only token rejected", r is not None and "tasks" in r, r)

        check("space-delimited scopes parse",
              ga.usable_token(token, creds) is not None)
        write(scopes=" ".join(ga.SCOPES))
        check("space-delimited scopes accepted", ga.usable_token(token, creds) is None)

        # lj's failure #2: the OAuth client was replaced. Refresh uses the id
        # inside token.json, so Google answers `deleted_client` forever.
        write(client_id="OLD-DELETED.apps.googleusercontent.com")
        r = ga.usable_token(token, creds)
        check("token from another OAuth client rejected",
              r is not None and "different OAuth client" in r, r)

        write(refresh_token="")
        check("token without refresh token rejected",
              "refresh token" in (ga.usable_token(token, creds) or ""))

        token.write_text("{not json")
        check("unreadable token rejected",
              "unreadable" in (ga.usable_token(token, creds) or ""))

        check("client_id read from installed block", ga.client_id_of(creds) == CID)
        creds.write_text(json.dumps({"web": {"client_id": "W"}}))
        check("client_id read from web block", ga.client_id_of(creds) == "W")
        creds.write_text("nonsense")
        check("unreadable credentials -> no id", ga.client_id_of(creds) is None)
        write()
        check("unknown current client does not reject a token",
              ga.usable_token(token, creds) is None)

    cfg = Config(vault=Path("/tmp/does-not-exist"))
    st = ga.status(cfg)
    check("status is offline-safe", st["ready"] is False and st["credentials"] is False)
    check("status explains itself", isinstance(st["reason"], str) and st["reason"])
    check("both scopes requested", len(ga.SCOPES) == 2 and any("tasks" in s for s in ga.SCOPES))


class FakeEvents:
    """Enough of the Google Calendar events resource to exercise the sweep."""

    def __init__(self, store):
        self.store = store

    def list(self, **kw):
        kind = kw["privateExtendedProperty"].split("=", 1)[1]
        items = [e for e in self.store.values()
                 if e["extendedProperties"]["private"].get("sb_kind") == kind]
        return _Exec({"items": items})

    def insert(self, calendarId, body):
        self.store[body["id"]] = body
        return _Exec(body)

    def update(self, calendarId, eventId, body):
        body = dict(body, id=eventId)
        self.store[eventId] = body
        return _Exec(body)

    def delete(self, calendarId, eventId):
        self.store.pop(eventId, None)
        return _Exec({})


class _Exec:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeCalendarService:
    def __init__(self, store):
        self._events = FakeEvents(store)

    def events(self):
        return self._events


def test_google_sync_logic():
    section("google sync logic (fake service)")
    from sb.calsync.google import GoogleSink, _gid

    cfg = Config(vault=Path("/tmp/x"))
    cfg.calendar.sink = "google"

    # Two events left behind by the previous model: an all-day `deadline`
    # banner, and a `habit` check-in. Neither is something the vault still
    # wants, so the sweep must find and remove them.
    store = {
        "sbstale1": {"id": "sbstale1", "summary": "DUE: old thing",
                     "extendedProperties": {"private": {"sb_kind": "deadline"}}},
        "sbstale2": {"id": "sbstale2", "summary": "Habit check-in: Gym",
                     "extendedProperties": {"private": {"sb_kind": "habit"}}},
        "sbforeign": {"id": "sbforeign", "summary": "Dentist",
                      "extendedProperties": {"private": {}}},
    }
    note = Note(id="n1", title="Ship it", bucket=Bucket.PROJECT,
                project=ProjectMeta(deadline=dt.date.today() + dt.timedelta(days=5),
                                    steps=[Step(id="s1", text="write", minutes=45,
                                                scheduled=dt.datetime.now().astimezone()
                                                + dt.timedelta(days=1))]))
    area = Note(id="a1", title="Gym", bucket=Bucket.AREA,
                habit=HabitMeta(cadence=Cadence.WEEKLY, target_count=3),
                schedule=AreaSchedule(time="07:00", duration_minutes=45))

    sink = GoogleSink()
    sink._service = None  # documents that auth is bypassed here
    import sb.calsync.google as gmod
    real = gmod._service
    gmod._service = lambda c: FakeCalendarService(store)
    try:
        r1 = sink.sync([note, area], cfg)
        r2 = sink.sync([note, area], cfg)   # idempotence
    finally:
        gmod._service = real

    # three events: the project's work block, the area series, and the one
    # weekly schedule review that exists because an Area does.
    check("retired kinds swept away", r1["deleted"] == 2, r1)
    check("events created", r1["created"] == 3, r1)
    check("second sync updates, never duplicates",
          r2["created"] == 0 and r2["deleted"] == 0 and r2["updated"] == 3, r2)
    check("foreign event untouched", "sbforeign" in store)
    check("no stale banners left",
          not any(e["extendedProperties"]["private"].get("sb_kind") in ("deadline", "habit")
                  for e in store.values()))

    area_ev = store[_gid(f"a1-area@{calevents.DOMAIN}")]
    check("area pushed as a recurring series",
          area_ev.get("recurrence") == ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"], area_ev.get("recurrence"))
    check("recurring series carries a named zone", area_ev["start"].get("timeZone"))
    check("area coloured by category", area_ev.get("colorId") == "10", area_ev.get("colorId"))
    check("no deadline event pushed",
          not any(e["extendedProperties"]["private"].get("sb_kind") == "deadline"
                  for e in store.values()))


class FakeTasks:
    def __init__(self, store):
        self.store = store
        self.seq = 0

    def list(self, **kw):
        return _Exec({"items": list(self.store.values())})

    def insert(self, tasklist, body):
        self.seq += 1
        body = dict(body, id=f"gt{self.seq}")
        self.store[body["id"]] = body
        return _Exec(body)

    def patch(self, tasklist, task, body):
        self.store[task] = dict(self.store.get(task, {}), **body)
        return _Exec(self.store[task])

    def delete(self, tasklist, task):
        self.store.pop(task, None)
        return _Exec({})


class FakeTaskLists:
    def __init__(self, lists):
        self.lists = lists

    def list(self, **kw):
        return _Exec({"items": self.lists})

    def insert(self, body):
        made = {"id": "tl-new", "title": body["title"]}
        self.lists.append(made)
        return _Exec(made)


class FakeTasksService:
    def __init__(self, store, lists):
        self._tasks = FakeTasks(store)
        self._lists = FakeTaskLists(lists)

    def tasks(self):
        return self._tasks

    def tasklists(self):
        return self._lists


def test_gtasks_sync_logic():
    section("google tasks sync logic (fake service)")
    from sb.calsync.gtasks import GoogleTasksSink

    cfg = Config(vault=Path("/tmp/x"))
    cfg.calendar.task_sink = "google"

    store = {"handmade": {"id": "handmade", "title": "buy milk", "notes": "no marker"}}
    lists = [{"id": "tl1", "title": "My Tasks"}]

    due = dt.date.today() + dt.timedelta(days=5)
    note = Note(id="n1", title="Ship it", bucket=Bucket.PROJECT,
                project=ProjectMeta(deadline=due))
    area = Note(id="a1", title="Gym", bucket=Bucket.AREA, habit=HabitMeta())

    import sb.calsync.gtasks as tmod
    real = tmod._google_service
    tmod._google_service = lambda c, a, v: FakeTasksService(store, lists)
    try:
        r1 = GoogleTasksSink().sync([note, area], cfg)
        after_first = dict(store)             # snapshot before later syncs mutate it
        r2 = GoogleTasksSink().sync([note, area], cfg)
        note.project.deadline = None          # deadline removed in the vault
        r3 = GoogleTasksSink().sync([note, area], cfg)
    finally:
        tmod._google_service = real

    check("tasklist created by name", any(l["title"] == "Second Brain" for l in lists))
    check("one task per due project", r1["created"] == 1, r1)
    check("area produced no task", len(after_first) == 2, list(after_first))
    check("second sync patches, never duplicates",
          r2["created"] == 0 and r2["updated"] == 1, r2)
    check("hand-made task never touched", store["handmade"]["title"] == "buy milk")
    check("removing the deadline removes the task", r3["deleted"] == 1, r3)
    check("hand-made task survives the delete sweep", "handmade" in store)


def test_task_shape():
    section("task shape")
    cfg = Config(vault=Path("/tmp/x"))

    def task_for(days, level=3):
        n = Note(id="p", title="Ship it", bucket=Bucket.PROJECT,
                 project=ProjectMeta(deadline=dt.date.today() + dt.timedelta(days=days),
                                     level=level))
        return calevents.tasks_for_note(n, cfg)[0]

    check("imminent outranks distant", task_for(1).priority < task_for(30).priority)
    check("harder outranks easier", task_for(10, level=5).priority < task_for(10, level=1).priority)
    check("priority stays in range", all(1 <= task_for(d).priority <= 9 for d in (0, 1, 5, 60)))

    overdue = task_for(-3)
    check("overdue flagged", overdue.overdue is True)

    partial = Note(id="p2", title="Half done", bucket=Bucket.PROJECT,
                   project=ProjectMeta(deadline=dt.date.today() + dt.timedelta(days=5),
                                       steps=[Step(id="s1", text="a", done=True),
                                              Step(id="s2", text="b")]))
    t = calevents.tasks_for_note(partial, cfg)[0]
    check("percent from steps", t.percent == 50, t.percent)

    body = gtasks.to_google(t)
    check("google task carries due date", body["due"].startswith(t.due.isoformat()))
    check("google task carries marker", f"[sb:{t.uid}]" in body["notes"])
    check("marker round trips", gtasks.uid_of({"notes": body["notes"]}) == t.uid)
    check("unmarked task is not ours", gtasks.uid_of({"notes": "bought milk"}) is None)

    for bucket in (Bucket.AREA, Bucket.RESOURCE, Bucket.ARCHIVE, Bucket.INBOX):
        n = Note(id="z", title="z", bucket=bucket,
                 project=ProjectMeta(deadline=dt.date.today() + dt.timedelta(days=2)))
        check(f"{bucket.value} never gets a task", calevents.tasks_for_note(n, cfg) == [])


def test_vault_and_engine():
    section("vault + engine (end to end, no model)")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(vault=Path(tmp) / "vault")
        cfg.llm.provider = "heuristic"
        engine = Engine(cfg)

        for d in ["00-Inbox", "10-Areas", "20-Projects", "30-Resources", "40-Archive", "_system"]:
            check(f"folder {d}", (cfg.vault / d).is_dir())

        r = engine.capture(
            "Learn Rust generics by next Friday, about 4 hours\n"
            "- read the Book ch.10\n- do the exercises\n- write a small demo",
            "project",
        )
        note_id = r["note"]["id"]
        p = r["note"]["project"]
        check("degraded flagged", r["parser"]["degraded"] is True)
        check("steps parsed", len(p["steps"]) == 3, p["steps"])
        check("deadline parsed", p["deadline"] is not None)
        check("learning flagged", p["learning"] is True)
        check("srs initialised", r["note"]["srs"] is not None)
        check("steps scheduled", all(s["scheduled"] for s in p["steps"]))
        check("file on disk", Path(r["path"]).exists())
        check("filed under Projects", "20-Projects" in r["path"])

        reread = Vault(cfg.vault).get(note_id)[1]
        check("round trips through disk", reread.project.deadline.isoformat() == p["deadline"])
        check("body has checklist", "- [ ] " in reread.body)
        check("body keeps capture", "Learn Rust generics" in reread.body)

        check("ics written", cfg.ics_path.exists())
        ics = cfg.ics_path.read_text()
        check("ics has a due task, not an event", "BEGIN:VTODO" in ics)
        check("task names the project", "Learn Rust generics" in ics)
        check("no DUE: banner event", "SUMMARY:DUE\\:" not in ics)

        engine.toggle_step(note_id, "s1")
        after = engine.note(note_id)
        check("step toggled", after.project.steps[0].done is True)
        check("toggle logged", any(h.event == "step" for h in after.history))
        check("body re-rendered once", after.body.count("## Capture") == 1)

        engine.toggle_step(note_id, "s2")
        engine.toggle_step(note_id, "s3")
        check("learning project awaits graduation", engine.note(note_id).project.status.value == "active")

        engine.move(note_id, "resource")
        moved = engine.note(note_id)
        check("moved to resource", moved.bucket == Bucket.RESOURCE)
        check("graduation logged", any(h.event == "graduated" for h in moved.history))
        check("review scheduled", moved.review and moved.review.next is not None)
        check("file physically moved", "30-Resources" in str(Vault(cfg.vault).get(note_id)[0]))
        check("old file gone", not list((cfg.vault / "20-Projects").glob("*.md")))

        engine.move(note_id, "archive")
        engine.move(note_id, "resource")
        check("archive round trip", engine.note(note_id).bucket == Bucket.RESOURCE)

        a = engine.capture("Exercise regularly", "area")
        area_id = a["note"]["id"]
        check("area gets habit", a["note"]["habit"]["cadence"] == "weekly")
        check("area gets a schedule", a["note"]["schedule"]["duration_minutes"] == 30)
        check("area body says it recurs", "recurs on the calendar" in engine.note(area_id).body)

        ics2 = cfg.ics_path.read_text()
        check("area recurs in the ics", "RRULE:FREQ=WEEKLY" in ics2)
        check("weekly schedule review present", "Schedule review" in ics2)

        # lj writes into the check-in log by hand; re-rendering must not eat it
        # or stack a second heading on top of it.
        hand = engine.note(area_id)
        hand.body = hand.body.rstrip() + "\n- 2026-08-20 went, felt good\n"
        Vault(cfg.vault).save(hand)

        engine.set_schedule(area_id, time="06:45", duration_minutes=50, days=[0, 3])
        rerendered = engine.note(area_id).body
        check("check-in log survives re-render", "felt good" in rerendered)
        check("one check-in heading", rerendered.count("## Check-in log") == 1)
        check("one capture heading", rerendered.count("## Capture") == 1)
        engine.set_habit(area_id, "weekly", 3)
        again = engine.note(area_id).body
        check("still one heading after a second re-render",
              again.count("## Check-in log") == 1 and "felt good" in again)

        moved = engine.note(area_id)
        check("schedule time changed", moved.schedule.time == "06:45")
        check("schedule days pinned", moved.schedule.days == [0, 3])
        check("schedule change logged", any(h.event == "schedule" for h in moved.history))
        check("new rrule synced", "BYDAY=MO,TH" in cfg.ics_path.read_text())

        engine.set_schedule(area_id, enabled=False)
        check("paused area leaves the calendar", "BYDAY=MO,TH" not in cfg.ics_path.read_text())
        engine.set_schedule(area_id, enabled=True)

        engine.set_category(area_id, "fun")
        check("category pinned", engine.note(area_id).category == "fun")
        engine.set_category(area_id, None)
        check("category cleared", engine.note(area_id).category is None)
        try:
            engine.set_category(area_id, "nonsense")
            check("bogus category rejected", False)
        except ValueError:
            check("bogus category rejected", True)
        try:
            engine.set_schedule(note_id, time="08:00")
            check("only areas take a schedule", False)
        except ValueError:
            check("only areas take a schedule", True)
        res = engine.capture("Rust Book https://doc.rust-lang.org/book/", "resource")
        check("resource gets review cycle", res["note"]["review"]["cycle_days"] == 90)

        d = engine.dashboard()
        check("dashboard counts", d["counts"]["area"] == 1 and d["counts"]["resource"] == 2, d["counts"])
        check("no active projects left", d["projects"] == [])

        r2 = engine.capture("Ship the invoice script by tomorrow\n- write it\n- test it", "project")
        d2 = engine.dashboard()
        check("dashboard lists project", len(d2["projects"]) == 1)
        check("queue populated", len(d2["next_actions"]) == 1)
        check("upcoming populated", len(d2["upcoming"]) >= 1)
        check("non-learning stays project", r2["note"]["project"]["learning"] is False)
        check("dashboard lists tasks", len(d2["tasks"]) == 1, d2["tasks"])
        check("task carries a category", d2["tasks"][0]["category"] in
              {c["key"] for c in d2["categories"]})
        check("dashboard lists areas", len(d2["areas"]) == 1)
        check("area shows its rrule", d2["areas"][0]["rrule"].startswith("FREQ="))
        check("area has a next occurrence", d2["areas"][0]["next"] is not None)
        check("projects carry a category", "category" in d2["projects"][0])
        check("upcoming labels due, not deadline",
              all(i["kind"] in ("due", "block") for i in d2["upcoming"]))

        h = engine.health()
        check("health reports vault", h["vault_exists"] is True)


class FakeLLM:
    """Stands in for Ollama so the model path — the one actually used in
    production — is covered without a running server."""

    name = "fake"
    is_llm = True

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def available(self):
        return True

    def complete_text(self, prompt, system=None):
        return "text"

    def complete_json(self, prompt, system=None, schema_hint=None):
        self.calls += 1
        self.last_prompt = prompt
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_llm_path():
    section("llm path (mocked model)")
    today = dt.date(2026, 8, 22)
    good = {
        "title": "Rust generics and trait bounds",
        "deadline": "2026-08-28",
        "estimate_minutes": 240,
        "level": 4,
        "skills": ["Rust", "type systems"],
        "materials": ["The Rust Book ch.10"],
        "steps": [
            {"text": "Read the Book ch.10", "minutes": 60},
            {"text": "Work the exercises", "minutes": 90},
            {"text": "Write a generic container", "minutes": 90},
        ],
        "learning": True,
        "ideal_end": "Can write a generic container with trait bounds unaided.",
    }
    fake = FakeLLM(good)
    original = parser.resolve_provider
    parser.resolve_provider = lambda cfg, role='': fake
    try:
        cfg = Config()
        r = parser.parse_project("Learn Rust generics by next Friday", cfg, today)
        check("model used", r.degraded is False and fake.calls == 1)
        check("title taken", r.title == "Rust generics and trait bounds")
        check("deadline honoured", r.meta.deadline == dt.date(2026, 8, 28))
        check("steps kept", len(r.meta.steps) == 3)
        check("step ids assigned", [s.id for s in r.meta.steps] == ["s1", "s2", "s3"])
        check("ideal end kept", r.meta.ideal_end.startswith("Can write"))
        check("prompt carries today", "2026-08-22" in fake.last_prompt)
        check("prompt carries prior deadline", "2026-09-04" in fake.last_prompt)

        # the model falls over -> capture still succeeds, flagged degraded
        parser.resolve_provider = lambda cfg, role='': FakeLLM(RuntimeError("model exploded"))
        r2 = parser.parse_project("Learn Go by tomorrow", cfg, today)
        check("model failure degrades gracefully", r2.degraded is True)
        check("fallback still parsed a deadline", r2.meta.deadline == dt.date(2026, 8, 23))

        # re-parsing preserves completed work
        parser.resolve_provider = lambda cfg, role='': FakeLLM(good)
        note = Note.capture("Learn Rust generics by next Friday", Bucket.PROJECT)
        parser.apply_to_note(note, cfg, today)
        note.project.steps[0].done = True
        parser.apply_to_note(note, cfg, today)
        check("re-parse keeps completions", note.project.steps[0].done is True)
        check("re-parse retitles", note.title == "Rust generics and trait bounds")
    finally:
        parser.resolve_provider = original


def test_api():
    section("http api")
    try:
        from starlette.testclient import TestClient
    except ImportError:
        print("  skip (no starlette testclient)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(vault=Path(tmp) / "vault")
        cfg.llm.provider = "heuristic"
        from sb.api import build_app

        client = TestClient(build_app(cfg))
        check("index serves", client.get("/").status_code == 200)
        check("health", client.get("/api/health").json()["vault_exists"] is True)

        r = client.post("/api/capture", json={"text": "Learn Go by friday\n- tour of go", "bucket": "project"})
        check("capture 200", r.status_code == 200, r.text[:200])
        nid = r.json()["note"]["id"]

        d = client.get("/api/dashboard").json()
        check("dashboard has project", len(d["projects"]) == 1)
        t = client.post(f"/api/notes/{nid}/steps/s1/toggle")
        check("step toggle 200", t.status_code == 200 and t.json()["project"]["steps"][0]["done"] is True)

        ics = client.get("/calendar.ics")
        check("ics served", ics.status_code == 200 and ics.text.startswith("BEGIN:VCALENDAR"))
        check("empty capture rejected", client.post("/api/capture", json={"text": "", "bucket": "project"}).status_code == 400)
        check("unknown note 500/400", client.get("/api/notes/nope").status_code in (400, 500))
        check("notes list", isinstance(client.get("/api/notes?bucket=project").json(), list))

        cats = client.get("/api/categories").json()
        check("categories served", any(c["key"] == "quiz" for c in cats))
        check("categories carry colours", all(c["hex"].startswith("#") for c in cats))

        aid = client.post("/api/capture", json={"text": "Gym", "bucket": "area"}).json()["note"]["id"]
        s = client.post(f"/api/notes/{aid}/schedule",
                        json={"time": "07:15", "duration_minutes": 45, "days": [1, 3]})
        check("schedule endpoint 200", s.status_code == 200)
        check("schedule applied", s.json()["schedule"]["time"] == "07:15")
        p = client.post(f"/api/notes/{aid}/schedule", json={"enabled": False})
        check("pause applied", p.json()["schedule"]["enabled"] is False)
        c = client.post(f"/api/notes/{aid}/category", json={"category": "fun"})
        check("category endpoint 200", c.status_code == 200 and c.json()["category"] == "fun")
        check("bad category 400",
              client.post(f"/api/notes/{aid}/category", json={"category": "zzz"}).status_code == 400)
        check("project rejects a schedule",
              client.post(f"/api/notes/{nid}/schedule", json={"time": "07:15"}).status_code == 400)

        d2 = client.get("/api/dashboard").json()
        check("dashboard exposes areas", len(d2["areas"]) == 1)
        check("dashboard exposes categories", len(d2["categories"]) >= 10)


def test_concurrent_writes():
    section("concurrent writes")
    # Found by driving the dashboard in a real browser: two date-picker changes
    # on one note, milliseconds apart, both reached the engine's threadpool.
    # Both writers built the same fixed `.md.tmp` path, the first replace
    # consumed it, and the second raised FileNotFoundError *after* the write
    # had actually succeeded — a 500 for an operation that worked.
    import threading

    with tempfile.TemporaryDirectory() as tmp:
        vault = Vault(Path(tmp) / "vault")
        note = Note.capture("Race me", Bucket.PROJECT)
        note.project = ProjectMeta(deadline=dt.date(2026, 9, 1))
        path = vault.write(note)

        errors = []
        barrier = threading.Barrier(8)

        def writer(i):
            try:
                n = vault.read(path)
                n.project.deadline = dt.date(2026, 9, 1) + dt.timedelta(days=i)
                barrier.wait(timeout=5)
                vault.save(n)
            except Exception as exc:  # noqa: BLE001 - the thing under test
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10)

        check("eight concurrent writes all succeed", errors == [], errors[:2])
        # The winner is whoever replaced last, and that is fine. What must
        # never happen is a half-written or missing note.
        survivor = vault.read(path)
        check("the note survives intact", survivor.id == note.id)
        check("and holds one real deadline, not a blend",
              survivor.project.deadline in
              [dt.date(2026, 9, 1) + dt.timedelta(days=i) for i in range(8)],
              survivor.project.deadline)
        leftovers = list((Path(tmp) / "vault" / "20-Projects").glob("*.tmp"))
        check("no temp files left behind", leftovers == [], leftovers)

        # The deck store rewrites its file on every single answer, so two
        # reviews graded a moment apart is the normal case there, not an edge.
        store = cardsmod.DeckStore(Path(tmp) / "vault")
        deck = cardsmod.Deck(note_id="n1", subject="Race")
        for i in range(4):
            deck.add(front=f"q{i}?", back="a", status="active")
        store.save(deck)

        deck_errors = []
        deck_barrier = threading.Barrier(6)

        def deck_writer(i):
            try:
                d = store.get("n1")
                d.cards[i % len(d.cards)].reps = i + 1
                deck_barrier.wait(timeout=5)
                store.save(d)
            except Exception as exc:  # noqa: BLE001
                deck_errors.append(f"{type(exc).__name__}: {exc}")

        deck_threads = [threading.Thread(target=deck_writer, args=(i,)) for i in range(6)]
        for th in deck_threads:
            th.start()
        for th in deck_threads:
            th.join(timeout=10)
        check("six concurrent deck writes all succeed", deck_errors == [], deck_errors[:2])
        check("the deck survives intact", len(store.get("n1").cards) == 4)
        check("no deck temp files left behind",
              list((Path(tmp) / "vault" / "_decks").glob("*.tmp")) == [])


def engine_note_path(vault_root, note_id):
    """The file backing a note id — for tests that need to age a note on disk
    into a shape an older version of the schema would have written.

    Matched on the id *inside* the file, not on the filename: the filename
    carries only the first 15 characters of the id, which is a timestamp to
    the second, and a test that captures several projects in a row produces
    several files sharing that prefix.
    """
    for p in sorted((Path(vault_root) / "20-Projects").glob("*.md")):
        meta, _ = frontmatter.parse(p.read_text(encoding="utf-8"))
        if meta.get("id") == note_id:
            return p
    raise AssertionError(f"no file for {note_id}")


def test_resource_reviews():
    section("resource reviews")
    try:
        from starlette.testclient import TestClient
    except ImportError:
        print("  skip (no starlette testclient)")
        return

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(vault=Path(tmp) / "vault")
        cfg.llm.provider = "heuristic"
        cfg.calendar.sink = "ics"
        cfg.review.resource_cycle_days = 90
        from sb.api import build_app

        engine = Engine(cfg)
        client = TestClient(build_app(cfg))

        fresh = client.post("/api/capture", json={
            "text": "Rust book chapter 10 notes", "bucket": "resource"}).json()["note"]["id"]
        d = client.get("/api/dashboard").json()
        check("a fresh resource is not asked about", d["reviews"] == [], d["reviews"])
        check("the archive panel starts empty", d["archive"]["items"] == [])

        # Age two resources into the review window.
        def age(note_id, days_ago):
            note = engine.note(note_id)
            note.review.next = dt.date.today() - dt.timedelta(days=days_ago)
            engine.vault.save(note)

        stale = client.post("/api/capture", json={
            "text": "Old conference handout. Amdahl's law is the rule that a "
                    "speedup is capped by the fraction of work that stays "
                    "serial. Little's law is the relation between queue length, "
                    "arrival rate and waiting time in a stable system.",
            "bucket": "resource"}).json()["note"]["id"]
        age(fresh, 0)
        age(stale, 12)

        rows = client.get("/api/dashboard").json()["reviews"]
        check("both land in the queue", len(rows) == 2, rows)
        check("the most overdue is first", rows[0]["note_id"] == stale, rows[0]["title"])
        check("it reports how long it has been asking", rows[0]["overdue_days"] == 12)
        check("it says when the note was filed", rows[0]["filed"] is not None)

        # Keep: stamps the review and pushes a full cycle out.
        r = client.post(f"/api/notes/{fresh}/review", json={"action": "keep"}).json()
        check("keeping reports the next date",
              r["next"] == (dt.date.today() + dt.timedelta(days=90)).isoformat(), r)
        rows = client.get("/api/dashboard").json()["reviews"]
        check("a kept resource leaves the queue",
              fresh not in [x["note_id"] for x in rows])
        kept = engine.note(fresh)
        check("keeping stamps the review", kept.review.last == dt.date.today())
        check("keeping is recorded in the history",
              any(h.event == "reviewed" and "kept" in (h.detail or "") for h in kept.history))

        # Snooze: a short push, and explicitly not a decision.
        s = client.post(f"/api/notes/{stale}/review",
                        json={"action": "snooze", "days": 14}).json()
        check("snoozing pushes it out 14 days",
              s["next"] == (dt.date.today() + dt.timedelta(days=14)).isoformat(), s)
        check("snoozing empties the queue", client.get("/api/dashboard").json()["reviews"] == [])
        snoozed = engine.note(stale)
        check("snoozing is not recorded as keeping",
              snoozed.review.last is None, snoozed.review.last)

        # Archive: a move, never a delete, and everything survives it.
        client.post(f"/api/decks/{stale}/generate", json={})
        client.post(f"/api/decks/{stale}/approve", json={})
        cards_before = client.get(f"/api/decks/{stale}").json()["active"]
        check("the resource has flashcards to lose", cards_before >= 1)

        age(stale, 1)
        row = [x for x in client.get("/api/dashboard").json()["reviews"]
               if x["note_id"] == stale][0]
        check("the queue says how many flashcards would come along",
              row["cards"] == cards_before, row["cards"])

        client.post(f"/api/notes/{stale}/review", json={"action": "archive"})
        d = client.get("/api/dashboard").json()
        check("archiving moves it out of Resources", d["counts"]["archive"] == 1)
        check("and out of the review queue", d["reviews"] == [])
        check("the archive panel shows it",
              [a["note_id"] for a in d["archive"]["items"]] == [stale])
        check("the flashcards survive archiving",
              client.get(f"/api/decks/{stale}").json()["active"] == cards_before)
        archived = engine.note(stale)
        check("the audit trail says why, not just that",
              any(h.event == "reviewed" and "not needed" in (h.detail or "")
                  for h in archived.history))

        # Restore: bidirectional (§2), with the clock restarted so it does not
        # bounce straight back into the queue.
        r = client.post(f"/api/notes/{stale}/review", json={"action": "restore"}).json()
        d = client.get("/api/dashboard").json()
        check("restoring puts it back in Resources", d["counts"]["resource"] == 2)
        check("restoring empties the archive panel", d["archive"]["items"] == [])
        check("restoring does not immediately re-ask", d["reviews"] == [])
        check("restoring restarts the cycle",
              r["next"] == (dt.date.today() + dt.timedelta(days=90)).isoformat(), r)

        # Wrong bucket, wrong action.
        proj = client.post("/api/capture", json={
            "text": "Ship the thing by friday", "bucket": "project"}).json()["note"]["id"]
        check("a project is not reviewed",
              client.post(f"/api/notes/{proj}/review",
                          json={"action": "keep"}).status_code == 400)
        check("an unknown action is rejected",
              client.post(f"/api/notes/{fresh}/review",
                          json={"action": "burn"}).status_code == 400)
        check("only archived notes restore",
              client.post(f"/api/notes/{fresh}/review",
                          json={"action": "restore"}).status_code == 400)


def test_habit_checkin():
    section("habit check-in")
    try:
        from starlette.testclient import TestClient
    except ImportError:
        print("  skip (no starlette testclient)")
        return

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(vault=Path(tmp) / "vault")
        cfg.llm.provider = "heuristic"
        cfg.calendar.sink = "ics"
        from sb.api import build_app

        engine = Engine(cfg)
        client = TestClient(build_app(cfg))
        aid = client.post("/api/capture", json={"text": "Gym", "bucket": "area"}).json()["note"]["id"]

        area = client.get("/api/dashboard").json()["areas"][0]
        check("a new area starts at 3× weekly",
              area["target_count"] == 3 and area["cadence"] == "weekly")
        check("and is unpinned", area["pinned"] is False)
        check("so its days are derived, spread not clumped",
              area["days"] == [0, 2, 4], area["days"])

        # §8's actual question: continue as-is, or change the occurrence count.
        client.post(f"/api/notes/{aid}/habit", json={"cadence": "weekly", "target_count": 5})
        area = client.get("/api/dashboard").json()["areas"][0]
        check("raising the target re-spreads the days while unpinned",
              area["days"] == [0, 1, 2, 3, 4], area["days"])
        check("the target is stored", area["target_count"] == 5)
        check("the rrule follows", "BYDAY=MO,TU,WE,TH,FR" in area["rrule"], area["rrule"])

        # Pinning: ticking days by hand takes the target out of the driving seat.
        client.post(f"/api/notes/{aid}/schedule", json={"days": [1, 3]})
        area = client.get("/api/dashboard").json()["areas"][0]
        check("ticking days pins the series", area["pinned"] is True)
        client.post(f"/api/notes/{aid}/habit", json={"cadence": "weekly", "target_count": 2})
        area = client.get("/api/dashboard").json()["areas"][0]
        check("a pinned series ignores the target", area["days"] == [1, 3], area["days"])
        check("but still records the new target", area["target_count"] == 2)

        # Cadence.
        client.post(f"/api/notes/{aid}/habit", json={"cadence": "daily", "target_count": 2})
        area = client.get("/api/dashboard").json()["areas"][0]
        check("cadence changes", area["cadence"] == "daily")
        check("and the recurrence rule with it", area["rrule"] == "FREQ=DAILY", area["rrule"])

        note = engine.note(aid)
        check("every change is in the history",
              sum(1 for h in note.history if h.event == "habit") == 3)
        check("the note body says the current target",
              "Target 2× per daily" in note.body, note.body[:300])

        check("a bad cadence is a 400",
              client.post(f"/api/notes/{aid}/habit",
                          json={"cadence": "fortnightly", "target_count": 2}).status_code == 400)


class FakeEmbedder:
    """A deterministic stand-in for nomic-embed-text.

    Each text becomes a bag-of-words vector over a fixed vocabulary, so cosine
    similarity behaves the way a real embedder's does — texts sharing
    vocabulary score higher — without a model running. Enough to test the
    plumbing (normalisation, row alignment, ranking, incremental reuse), which
    is the part that can actually be wrong.
    """

    name = "fake-embed"
    is_llm = True
    VOCAB = [
        "amdahl", "speedup", "serial", "parallel", "cores", "little", "queue",
        "arrival", "wait", "deposit", "flat", "pounds", "march", "inventory",
        "krebs", "cycle", "mitochondria", "atp", "law", "system",
    ]

    def __init__(self, answer="The maximum speedup is 10x [1]."):
        self.answer = answer
        self.embed_calls = 0
        self.embedded_texts = []
        self.text_calls = 0
        self.last_prompt = ""

    def available(self):
        return True

    #: Words outside the vocabulary hash into their own slots, the way feature
    #: hashing works. Without this an unknown query would collapse to a
    #: degenerate vector and match the first dimension — which is a property of
    #: the stub, not of any real embedder, and would make the "an unrelated
    #: question matches nothing" test pass or fail for the wrong reason.
    NOISE_DIMS = 64

    def embed(self, texts):
        self.embed_calls += 1
        out = []
        for text in texts:
            self.embedded_texts.append(text)
            low = text.lower()
            vec = [float(low.count(word)) for word in self.VOCAB] + [0.0] * self.NOISE_DIMS
            for word in re.findall(r"[a-z0-9]{3,}", low):
                if word in self.VOCAB:
                    continue
                slot = len(self.VOCAB) + (zlib.crc32(word.encode()) % self.NOISE_DIMS)
                vec[slot] += 1.0
            if not any(vec):
                vec[len(self.VOCAB)] = 1.0
            out.append(vec)
        return out

    def complete_text(self, prompt, system=None):
        self.text_calls += 1
        self.last_prompt = prompt
        return self.answer

    def complete_json(self, prompt, system=None, schema_hint=None):
        return {}


def test_index_chunking():
    section("index chunking")
    note = Note.capture(
        "Performance laws\n\n"
        "## Amdahl\n\n"
        "Amdahl's law caps a speedup by the serial fraction of the work.\n\n"
        "## Little\n\n"
        "Little's law relates queue length to arrival rate times average wait.",
        Bucket.RESOURCE,
    )
    note.title = "Performance laws"
    chunks = idxmod.chunk_note(note)
    check("one chunk per section", len(chunks) == 2, [c["heading"] for c in chunks])
    check("headings are captured",
          [c["heading"] for c in chunks] == ["Amdahl", "Little"])
    check("the heading travels with the text",
          all(c["heading"] in c["text"] for c in chunks))
    check("so does the note title",
          all(c["text"].startswith("Performance laws") for c in chunks))
    check("chunks are ordered", [c["ord"] for c in chunks] == [0, 1])

    # A long unbroken passage is windowed, with overlap so a sentence split
    # across a boundary survives whole somewhere.
    long_note = Note.capture("x", Bucket.RESOURCE)
    long_note.title = "Long"
    long_note.body = ". ".join(f"Sentence number {i} about queues" for i in range(120)) + "."
    windows = idxmod.chunk_note(long_note)
    check("a long passage is windowed", len(windows) > 2, len(windows))
    check("windows respect the size cap",
          all(len(w["text"]) <= idxmod.CHUNK_CHARS + 120 for w in windows),
          max(len(w["text"]) for w in windows))
    joined = " ".join(w["text"] for w in windows)
    check("nothing is lost in the middle", "Sentence number 60" in joined)

    # A two-line capture is often exactly what you are looking for.
    tiny = Note.capture("Wifi password is hunter2", Bucket.RESOURCE)
    check("a tiny note is still indexed", len(idxmod.chunk_note(tiny)) == 1)
    check("and carries its text", "hunter2" in idxmod.chunk_note(tiny)[0]["text"])

    empty = Note.capture("Just a title", Bucket.RESOURCE)
    empty.body = ""
    check("an empty body falls back to the title",
          idxmod.chunk_note(empty)[0]["text"] == empty.title)

    a = Note.capture("same text", Bucket.RESOURCE)
    b = Note.capture("same text", Bucket.RESOURCE)
    check("fingerprints match on identical content",
          idxmod.fingerprint(a) == idxmod.fingerprint(b))
    b.body = "different"
    check("and differ on edits", idxmod.fingerprint(a) != idxmod.fingerprint(b))


def _rag_vault(tmp):
    cfg = Config(vault=Path(tmp) / "vault")
    cfg.llm.provider = "heuristic"
    cfg.calendar.sink = "ics"
    engine = Engine(cfg)
    ids = {}
    ids["perf"] = engine.capture(
        "Amdahl's law caps a speedup by the serial fraction of the work. "
        "If 10 percent stays serial the ceiling is ten times however many cores.\n\n"
        "## Little\n\nLittle's law relates queue length to arrival rate times wait.",
        "resource")["note"]["id"]
    ids["flat"] = engine.capture(
        "Deposit for the flat was 1400 pounds paid in March, held with the scheme. "
        "The inventory was signed the same week.", "resource")["note"]["id"]
    ids["old"] = engine.capture(
        "Lecture notes on the krebs cycle and how mitochondria make atp.",
        "resource")["note"]["id"]
    engine.move(ids["old"], "archive")
    return cfg, engine, ids


def test_index_build():
    section("index build")
    with tempfile.TemporaryDirectory() as tmp:
        cfg, engine, ids = _rag_vault(tmp)
        fake = FakeEmbedder()
        original = idxmod.resolve_provider
        idxmod.resolve_provider = lambda c, role='': fake
        try:
            stats = engine.reindex()
            check("every indexable note is read", stats["notes"] == 3, stats)
            check("chunks were written", stats["chunks"] >= 4, stats)
            check("everything was embedded", stats["embedded"] == stats["chunks"], stats)
            check("the index is semantic", stats["semantic"] is True, stats.get("warning"))

            status = engine.index_status()
            check("the manifest records the model",
                  status["model"] == cfg.llm.embed_model, status["model"])
            check("dimension is recorded",
                  status["dim"] == len(FakeEmbedder.VOCAB) + FakeEmbedder.NOISE_DIMS,
                  status["dim"])
            check("one vector per chunk", status["vectors"] == status["chunks"])
            check("archive is indexed too, just not searched by default",
                  set(status["buckets"]) == {"resource", "archive"}, status["buckets"])
            check("the index reports itself current", status["current"] is True, status)

            # Vectors are normalised at write time so search is a plain dot.
            _, vectors, _ = engine.index.load()
            check("vectors are unit length",
                  all(abs(sum(x * x for x in v) - 1.0) < 1e-5 for v in vectors))

            # -- incremental: editing one note re-embeds one note
            before = fake.embed_calls
            fake.embedded_texts.clear()
            again = engine.reindex()
            check("an unchanged vault re-embeds nothing", again["embedded"] == 0, again)
            check("and reuses everything", again["reused"] == again["chunks"], again)
            check("the model was not called at all", fake.embed_calls == before)

            note = engine.note(ids["flat"])
            note.body = note.body + "\n\nThe deposit was returned in full."
            engine.vault.save(note)
            fake.embedded_texts.clear()
            edited = engine.reindex()
            check("an edited note is re-embedded", edited["embedded"] >= 1, edited)
            check("and only that note",
                  all("deposit" in t.lower() for t in fake.embedded_texts),
                  fake.embedded_texts[:1])
            check("the rest is reused", edited["reused"] >= 2, edited)

            # -- a deleted note leaves nothing behind
            path, _ = engine.vault.get(ids["flat"])
            path.unlink()
            engine.index._cache = None
            pruned = engine.reindex()
            check("a deleted note's chunks are dropped", pruned["removed"] >= 1, pruned)
            chunks, _, _ = engine.index.load()
            check("and are really gone",
                  ids["flat"] not in {c["note_id"] for c in chunks})

            # -- force rebuilds from scratch
            fake.embedded_texts.clear()
            forced = engine.reindex(force=True)
            check("force re-embeds everything",
                  forced["embedded"] == forced["chunks"] and forced["reused"] == 0, forced)
        finally:
            idxmod.resolve_provider = original


def test_index_search():
    section("index search")
    with tempfile.TemporaryDirectory() as tmp:
        cfg, engine, ids = _rag_vault(tmp)
        fake = FakeEmbedder()
        original = idxmod.resolve_provider
        idxmod.resolve_provider = lambda c, role='': fake
        try:
            engine.reindex()
            idx = engine.index

            hits = idx.search("amdahl speedup serial cores")
            check("the right note comes first",
                  hits and hits[0]["note_id"] == ids["perf"], hits[:1])
            check("hits carry their score and note", hits[0]["score"] > 0 and hits[0]["note_id"])
            check("and are marked semantic", hits[0]["semantic"] is True)

            check("archive is excluded by default",
                  all(h["bucket"] != "archive" for h in idx.search("krebs cycle mitochondria")))
            widened = idx.search("krebs cycle mitochondria", include_archive=True)
            check("and included when asked",
                  any(h["bucket"] == "archive" for h in widened), widened[:1])

            # One long note must not be allowed to supply the whole answer.
            capped = idx.search("law queue arrival wait system", per_note=1)
            per_note = {}
            for h in capped:
                per_note[h["note_id"]] = per_note.get(h["note_id"], 0) + 1
            check("per-note cap holds", all(v <= 1 for v in per_note.values()), per_note)

            check("k is respected", len(idx.search("law", k=1)) <= 1)
            check("an unrelated question matches nothing",
                  idx.search("zebra pancake helicopter") == [])

            # A vector file that does not line up with the chunk file is a
            # broken index, not a half-usable one.
            idx.vectors_path.write_bytes(b"\x00" * 12)
            idx._cache = None
            chunks, vectors, _ = idx.load()
            check("a mismatched vector file is refused", vectors == [], len(vectors))
            degraded = idx.search("amdahl speedup serial")
            check("and search falls back to keywords rather than lying",
                  degraded and degraded[0]["semantic"] is False, degraded[:1])
        finally:
            idxmod.resolve_provider = original


def test_ask():
    section("ask")
    with tempfile.TemporaryDirectory() as tmp:
        cfg, engine, ids = _rag_vault(tmp)
        fake = FakeEmbedder(answer="Ten times, because 10% stays serial [1]. "
                                   "Little's law is unrelated [2].")
        oi, oa = idxmod.resolve_provider, askmod.resolve_provider
        idxmod.resolve_provider = lambda c, role='': fake
        askmod.resolve_provider = lambda c, role='': fake
        try:
            engine.reindex()
            # A question spanning both halves of the notes, so there really
            # are two sources for the answer to cite.
            r = engine.ask("amdahl serial speedup and little law queue arrival wait")
            check("the answer comes back", "Ten times" in r["answer"], r["answer"])
            check("it is grounded", r["grounded"] is True)
            check("sources are numbered from one",
                  [s["n"] for s in r["sources"]] == list(range(1, len(r["sources"]) + 1)))
            check("citations are parsed out of the prose", r["used"] == [1, 2], r["used"])
            check("a citation past the last source is dropped, not shown",
                  askmod._cited("see [1] and [9]", 2) == [1])
            check("uncited text yields no citations", askmod._cited("no markers", 3) == [])
            check("sources carry a category for the swatch",
                  all("category" in s for s in r["sources"]))
            check("excerpts are trimmed, not whole chunks",
                  all(len(s["excerpt"]) <= 321 for s in r["sources"]))
            check("the prompt hands the model numbered excerpts",
                  "[1]" in fake.last_prompt and "[2]" in fake.last_prompt)
            check("the prompt carries the question",
                  "little law queue arrival wait" in fake.last_prompt)
            check("more than one passage was retrieved", len(r["sources"]) >= 2,
                  len(r["sources"]))

            # The refusal path: no retrieval, no answer, no invention.
            empty = engine.ask("zebra pancake helicopter")
            check("nothing found means nothing answered",
                  empty["grounded"] is False and empty["sources"] == [])
            check("and it says archive was left out",
                  "archive" in empty["answer"].lower(), empty["answer"])
            check("the model is not even asked", fake.text_calls == 1, fake.text_calls)

            arc = engine.ask("krebs cycle mitochondria atp", include_archive=True)
            check("archive answers when invited", arc["grounded"] is True)
            check("and the flag comes back", arc["searched_archive"] is True)
            check("archived sources are labelled",
                  any(s["bucket"] == "archive" for s in arc["sources"]))
        finally:
            idxmod.resolve_provider, askmod.resolve_provider = oi, oa

        # No model at all: the excerpts are still the useful half.
        r = engine.ask("amdahl serial speedup")
        check("with no model, excerpts still come back", len(r["sources"]) >= 1)
        check("and it says why there is no prose",
              "Ollama" in r["note"] or "no model" in r["answer"].lower(), r["note"])


def test_ask_api():
    section("ask api")
    try:
        from starlette.testclient import TestClient
    except ImportError:
        print("  skip (no starlette testclient)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        cfg, engine, ids = _rag_vault(tmp)
        from sb.api import build_app

        client = TestClient(build_app(cfg))
        s = client.get("/api/index").json()
        check("a fresh vault has no index", s["built"] is False, s)

        r = client.post("/api/index/rebuild", json={}).json()
        check("rebuild works with no model, keyword-only",
              r["chunks"] >= 3 and r["semantic"] is False, r)
        # A degraded build is a success. Reporting it under `error` would make
        # the API client treat a working keyword index as a failed call — which
        # is exactly what it did before this was caught in the browser.
        check("a degraded build is a warning, not an error",
              "error" not in r and r.get("warning"), r)
        s = client.get("/api/index").json()
        check("status reports keyword mode", s["semantic"] is False)
        check("status counts what it indexed", s["notes"] == 3, s)

        a = client.post("/api/ask", json={"question": "deposit flat pounds march"}).json()
        check("asking returns sources", len(a["sources"]) >= 1, a)
        check("the right note is cited",
              a["sources"][0]["note_id"] == ids["flat"], a["sources"][0]["title"])

        check("an empty question is a 400",
              client.post("/api/ask", json={"question": "  "}).status_code == 400)

        # Asking before indexing builds the index rather than erroring.
        engine.index.chunks_path.unlink()
        engine.index._cache = None
        a2 = client.post("/api/ask", json={"question": "amdahl serial"}).json()
        check("asking builds the index if it is missing", len(a2["sources"]) >= 1, a2)

        h = client.get("/api/health").json()
        check("doctor reports the index", h["index"]["built"] is True, h.get("index"))

        # Editing a note in Obsidian while the app is closed must show as stale.
        note = engine.note(ids["perf"])
        note.body += "\n\nA further paragraph about queues."
        engine.vault.save(note)
        s = client.get("/api/index").json()
        check("an edited note shows as stale", s["stale"] == 1, s)
        check("and the index is not claimed current", s["current"] is False)
        client.post("/api/index/rebuild", json={})
        check("re-indexing clears it", client.get("/api/index").json()["current"] is True)


def test_model_lanes():
    section("model lanes")
    from sb import llm as llmmod
    from sb.config import ROLES

    # One model for everything is the default and stays that way.
    plain = Config().llm
    check("no study model means one lane",
          {r: plain.model_for(r) for r in ROLES} == {r: plain.model for r in ROLES})

    two = Config().llm
    two.model = "llama3.1:8b"
    two.study_model = "phi4"
    check("parsing stays on the fast model", two.model_for("parse") == "llama3.1:8b")
    check("card generation gets the good one", two.model_for("generate") == "phi4")
    check("so does marking", two.model_for("grade") == "phi4")
    check("and explaining", two.model_for("explain") == "phi4")
    check("and asking", two.model_for("ask") == "phi4")
    check("an unknown role falls to the fast lane", two.model_for("nonsense") == "llama3.1:8b")
    check("no role at all is the fast lane", two.model_for("") == "llama3.1:8b")

    # The two lanes take turns in VRAM, so they hold on for different lengths.
    check("the fast lane lets go quickly", two.keep_alive_for("parse") == two.keep_alive)
    check("the study lane holds through a session",
          two.keep_alive_for("ask") == two.study_keep_alive)
    check("and those are actually different", two.keep_alive != two.study_keep_alive)

    # Moving a role between lanes is a config edit, not a code change.
    two.study_roles = ["ask"]
    check("roles are configurable",
          two.model_for("ask") == "phi4" and two.model_for("generate") == "llama3.1:8b")

    # -- tag matching: `ollama pull phi4` reports back as `phi4:latest`
    check("bare tag matches :latest", llmmod.has_model(["phi4:latest"], "phi4"))
    check("explicit tag matches itself", llmmod.has_model(["phi4:latest"], "phi4:latest"))
    check("a different model does not match", not llmmod.has_model(["llama3.1:8b"], "phi4"))
    check("a different size does not match",
          not llmmod.has_model(["qwen3:14b"], "qwen3:32b"))
    check("nothing matches nothing", not llmmod.has_model([], "phi4"))
    check("an empty want never matches", not llmmod.has_model(["phi4"], ""))

    # -- fallback when the study model was never pulled
    two.study_model = "phi4"
    # back to the shipped split: everything but `parse`
    two.study_roles = ["generate", "grade", "explain", "ask"]
    real = llmmod.installed_models
    try:
        llmmod.installed_models = lambda cfg, force=False: ["llama3.1:8b", "phi4:latest"]
        model, why = llmmod.resolve_model(two, "ask")
        check("a pulled study model is used", model == "phi4" and why == "")

        llmmod.installed_models = lambda cfg, force=False: ["llama3.1:8b"]
        model, why = llmmod.resolve_model(two, "ask")
        check("an unpulled study model falls back", model == "llama3.1:8b")
        check("and says how to fix it", "ollama pull phi4" in why, why)
        check("the fast lane is unaffected",
              llmmod.resolve_model(two, "parse") == ("llama3.1:8b", ""))

        # Ollama being down is a different problem, and must not be reported
        # as a missing pull — the caller's availability check handles it.
        llmmod.installed_models = lambda cfg, force=False: []
        model, why = llmmod.resolve_model(two, "ask")
        check("a stopped server is not blamed on a missing model",
              model == "phi4" and why == "")

        llmmod.installed_models = lambda cfg, force=False: ["llama3.1:8b"]
        report = llmmod.lane_report(two)
        check("the report names both lanes",
              report["fast"]["model"] == "llama3.1:8b"
              and report["study"]["model"] == "phi4")
        check("and admits the study model is missing",
              report["study"]["pulled"] is False and report["study"]["in_use"] == "llama3.1:8b")
        check("and maps every role", set(report["roles"]) == set(ROLES))
    finally:
        llmmod.installed_models = real

    # -- the provider is really bound to the resolved model
    two.study_model = ""
    fast = llmmod.get_provider(two, "generate")
    check("one lane binds the fast model", fast.model == "llama3.1:8b", fast.model)
    two.study_model = "phi4"
    try:
        llmmod.installed_models = lambda cfg, force=False: ["llama3.1:8b", "phi4:latest"]
        good = llmmod.get_provider(two, "generate")
        check("two lanes bind the study model", good.model == "phi4", good.model)
        check("and the longer keep-alive with it",
              good.keep_alive == two.study_keep_alive, good.keep_alive)
        check("while parsing keeps the short one",
              llmmod.get_provider(two, "parse").keep_alive == two.keep_alive)
    finally:
        llmmod.installed_models = real


def test_lane_routing():
    section("lane routing")
    # Each call site must name its own role, or the whole split is decorative.
    from sb import ask as askmod2, generate as genmod, parser as parsermod, tutor as tutormod

    seen = []

    class Spy:
        name, is_llm, model = "spy", True, "spy"

        def available(self):
            return True

        def complete_text(self, prompt, system=None):
            return "text"

        def complete_json(self, prompt, system=None, schema_hint=None):
            return {"cards": []}

    def spy(module):
        def factory(cfg, role=""):
            seen.append((module, role))
            return Spy()
        return factory

    saved = {
        m: m.resolve_provider
        for m in (parsermod, genmod, tutormod, askmod2)
    }
    try:
        for m in saved:
            m.resolve_provider = spy(m.__name__.rsplit(".", 1)[-1])
        cfg = Config()
        parsermod.parse_project("learn x by friday", cfg, dt.date(2026, 8, 22))
        genmod.generate("Amdahl law is a rule about speedups. " * 6, cfg)
        tutormod.grade_recall("q?", "an answer", "my attempt", cfg)
        tutormod.explain(cardsmod.Card(id="c1", front="q?", back="a"), "body", "why?", cfg)
        with tempfile.TemporaryDirectory() as tmp:
            c2 = Config(vault=Path(tmp) / "v")
            idx = idxmod.Index(c2)
            idx.dir.mkdir(parents=True, exist_ok=True)
            idx.chunks_path.write_text(
                json.dumps({"id": "n#0", "note_id": "n", "title": "T", "bucket": "resource",
                            "heading": "", "ord": 0, "text": "amdahl law speedup serial",
                            "fingerprint": "x"}) + "\n", encoding="utf-8")
            askmod2.ask("amdahl law speedup", c2, index=idx)
    finally:
        for m, fn in saved.items():
            m.resolve_provider = fn

    roles = dict(seen)
    check("parsing asks for the parse role", roles.get("parser") == "parse", seen)
    check("generation asks for the generate role", roles.get("generate") == "generate", seen)
    check("asking asks for the ask role", roles.get("ask") == "ask", seen)
    check("marking and explaining both route through tutor",
          [r for m, r in seen if m == "tutor"] == ["grade", "explain"], seen)
    check("every role named is one the config knows",
          all(r in Config().llm.study_roles + ["parse"] for _, r in seen), seen)


def test_model_wire():
    section("model routing on the wire")
    # Config-level routing can be right while a call site still forgets to name
    # its role. This drives a fake Ollama over real HTTP and reads the `model`
    # field off each request — the only check that cannot be fooled.
    import http.server
    import socketserver
    import threading
    import time

    calls = []

    class FakeOllama(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/tags":
                self._json({"models": [{"name": "llama3.1:8b"}, {"name": "phi4:latest"},
                                       {"name": "nomic-embed-text:latest"}]})
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            calls.append((self.path, body.get("model"), body.get("keep_alive")))
            if self.path == "/api/embeddings":
                self._json({"embedding": [0.1] * 8})
            else:
                self._json({"message": {"content":
                                        '{"cards": [], "score": 0.9, "feedback": "ok"}'}})

    # Port 0 lets the OS pick a free one. A fixed port collides with a
    # previous run still in TIME_WAIT and silently skips the test, which is
    # the worst outcome: a check that never fails because it never runs.
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 0), FakeOllama)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)

    from sb import llm as llmmod

    try:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(vault=Path(tmp) / "v")
            cfg.llm.provider = "ollama"
            cfg.llm.ollama_url = f"http://127.0.0.1:{port}"
            cfg.llm.model = "llama3.1:8b"
            cfg.llm.study_model = "phi4"
            cfg.calendar.sink = "ics"
            llmmod._tags_cache.clear()

            engine = Engine(cfg)
            engine.capture("Learn Rust generics by friday\n- read ch10\n- exercises", "project")
            nid = engine.capture(
                "Amdahl law is a rule that caps a speedup by the serial fraction. "
                "Little law relates queue length to arrival rate times wait time.",
                "resource")["note"]["id"]
            engine.generate_cards(nid)
            engine.reindex()
            engine.ask("amdahl law serial fraction")

        chats = [(m, k) for path, m, k in calls if path == "/api/chat"]
        embeds = [(m, k) for path, m, k in calls if path == "/api/embeddings"]

        check("the capture parse went to the fast model",
              chats and chats[0][0] == "llama3.1:8b", chats[:1])
        check("and released it quickly", chats[0][1] == "5m", chats[0])
        check("card generation went to the study model",
              "phi4" in [m for m, _ in chats[1:]], chats)
        check("asking did too", chats[-1][0] == "phi4", chats[-1])
        check("the study lane holds VRAM through a session",
              all(k == "30m" for m, k in chats if m == "phi4"), chats)
        check("no chat call used a model nobody configured",
              set(m for m, _ in chats) <= {"llama3.1:8b", "phi4"}, chats)
        check("embeddings used the embedding model",
              embeds and all(m == "nomic-embed-text" for m, _ in embeds), embeds)
        check("and hold VRAM only briefly, so they do not evict a chat model",
              all(k == "2m" for _, k in embeds), embeds)
    finally:
        srv.shutdown()
        srv.server_close()
        llmmod._tags_cache.clear()


# ==========================================================================
# dates: confidence and approval
# ==========================================================================


def test_date_confidence():
    section("date confidence")
    today = dt.date(2026, 8, 22)  # a Saturday

    def g(text):
        return extract.parse_deadline_guess(text, today)

    # -- the three bugs ----------------------------------------------------

    # 1. "this Friday" and "next Friday" used to be the same day. A deadline
    #    landing a week early is exactly the kind of wrong that looks right.
    check("this friday is the coming one", g("this friday").date == dt.date(2026, 8, 28))
    check("next friday skips a week", g("next friday").date == dt.date(2026, 9, 4))
    check("bare friday is the coming one", g("friday").date == dt.date(2026, 8, 28))
    check("they are no longer the same day",
          g("this friday").date != g("next friday").date)
    check("the qualifier is read through a preposition",
          g("by next friday").date == dt.date(2026, 9, 4))

    # 2. "a week from today" returned today — the bare `today` matcher claimed
    #    the word out of the middle of the phrase.
    check("a week from today is a week away",
          g("a week from today").date == dt.date(2026, 8, 29))
    check("2 weeks from now works too",
          g("2 weeks from now").date == dt.date(2026, 9, 5))
    check("bare today still works", g("today").date == today)

    # 3. "in a month" was 30 days, not a calendar month.
    check("in a month is a calendar month", g("in a month").date == dt.date(2026, 9, 22))
    check("month arithmetic clamps short months",
          extract._add_months(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28))
    check("and does not overflow into March",
          extract._add_months(dt.date(2026, 1, 31), 1).month == 2)

    # -- the false-positive class ------------------------------------------
    check("'do 1/2 the chapter' is not 2 January", g("do 1/2 the chapter").date is None)
    check("'read pages 3/4' is not 4 March", g("read pages 3/4").date is None)
    check("'chapter 3-5' is not a date", g("read chapter 3-5").date is None)
    check("'1st chapter' is not the 1st", g("1st chapter").date is None)
    check("a preposition makes it a date", g("by 9/8").date == dt.date(2026, 9, 8))
    check("a four-digit year makes it a date", g("9/8/2026").date == dt.date(2026, 9, 8))
    check("being the whole capture makes it a date", g("8-25").date == dt.date(2026, 8, 25))

    # -- phrases that now work at all --------------------------------------
    for text, expected in [
        ("eow", dt.date(2026, 8, 28)),
        ("end of the month", dt.date(2026, 8, 31)),
        ("eom", dt.date(2026, 8, 31)),
        ("by eod", today),
        ("this weekend", dt.date(2026, 8, 22)),
        ("next weekend", dt.date(2026, 8, 29)),
        ("the 30th", dt.date(2026, 8, 30)),
        ("the 5th", dt.date(2026, 9, 5)),        # already past this month
        ("by the 15th", dt.date(2026, 9, 15)),
        ("the 30th of September", dt.date(2026, 9, 30)),
        ("day after tomorrow", dt.date(2026, 8, 24)),
    ]:
        check(f"{text!r} -> {expected}", g(text).date == expected, g(text).date)

    # -- an explicit prefix is read literally ------------------------------
    check("due: is explicit", g("due: 2026-12-01").kind == extract.EXPLICIT)
    check("deadline = friday is explicit, not a guess",
          g("deadline = friday").kind == extract.EXPLICIT and g("friday").kind == extract.EXACT)
    check("due: wins over other text in the line",
          g("ship the thing next friday, due: 2026-12-01").date == dt.date(2026, 12, 1))

    # -- the certain / ambiguous split -------------------------------------
    for text, kind in [
        ("sept 8", extract.EXPLICIT),
        ("2026-09-01", extract.EXPLICIT),
        ("by 9/8", extract.EXPLICIT),
        ("due: 2026-12-01", extract.EXPLICIT),
        ("tomorrow", extract.EXACT),
        ("by eod", extract.EXACT),
        ("in 2 weeks", extract.EXACT),
        ("a week from today", extract.EXACT),
        ("this friday", extract.EXACT),
        ("by the end of the week", extract.AMBIGUOUS),
        ("next friday", extract.AMBIGUOUS),
        ("in a month", extract.AMBIGUOUS),
        ("next week", extract.AMBIGUOUS),
        ("this weekend", extract.AMBIGUOUS),
    ]:
        check(f"{text!r} is {kind}", g(text).kind == kind, g(text).kind)

    check("explicit arrives confirmed", g("sept 8").confirmed is True)
    check("exact arrives confirmed", g("tomorrow").confirmed is True)
    check("ambiguous does not", g("next friday").confirmed is False)
    check("no date is not confirmed", g("learn rust").confirmed is False)

    # -- the phrase is quotable --------------------------------------------
    check("the phrase is kept", g("finish by the end of the week").phrase == "end of the week")
    check("the phrase keeps the original casing",
          g("Ship it by Next Friday").phrase == "Next Friday")
    check("no date, no phrase", g("learn rust").phrase == "")

    # -- the prior carries it through --------------------------------------
    prior = extract.project_prior("learn x by the end of the week", today)
    check("prior carries the kind", prior["deadline_kind"] == extract.AMBIGUOUS)
    check("prior carries the phrase", prior["deadline_phrase"] == "end of the week")
    check("prior carries confirmed", prior["deadline_confirmed"] is False)


def test_deadline_approval():
    section("deadline approval")
    today = dt.date(2026, 8, 22)
    cfg = Config()

    # A model that quietly rewrites a date the rules read correctly. This is
    # the whole reason for the `llm` kind.
    drift = {
        "title": "Ship the thing",
        "deadline": "2026-11-11",
        "estimate_minutes": 60,
        "level": 3,
        "steps": [{"text": "do it", "minutes": 60}],
    }
    original = parser.resolve_provider
    parser.resolve_provider = lambda c, role='': FakeLLM(drift)
    try:
        r = parser.parse_project("Ship the thing by sept 8", cfg, today)
        check("model drift is kept but flagged", r.meta.deadline == dt.date(2026, 11, 11))
        check("model drift is sourced to the model", r.meta.deadline_source == extract.LLM)
        check("model drift is never confirmed", r.meta.deadline_confirmed is False)

        # Agreement is not a downgrade.
        parser.resolve_provider = lambda c, role='': FakeLLM({**drift, "deadline": "2026-09-08"})
        r2 = parser.parse_project("Ship the thing by sept 8", cfg, today)
        check("agreement keeps the rules' confidence",
              r2.meta.deadline_source == extract.EXPLICIT and r2.meta.deadline_confirmed)

        # The model saying nothing leaves the rules in charge.
        parser.resolve_provider = lambda c, role='': FakeLLM({**drift, "deadline": None})
        r3 = parser.parse_project("Ship the thing by the end of the week", cfg, today)
        check("silence leaves the rules' guess", r3.meta.deadline_source == extract.AMBIGUOUS)
        check("and the phrase to quote", r3.meta.deadline_phrase == "end of the week")
    finally:
        parser.resolve_provider = original

    # -- the approval flow, through the HTTP layer -------------------------
    try:
        from starlette.testclient import TestClient
    except ImportError:
        print("  skip api half (no starlette testclient)")
        return

    with tempfile.TemporaryDirectory() as tmp:
        c = Config(vault=Path(tmp) / "vault")
        c.llm.provider = "heuristic"
        c.calendar.sink = "ics"
        from sb.api import build_app

        client = TestClient(build_app(c))

        vague = client.post("/api/capture", json={
            "text": "Finish the lab report by the end of the week", "bucket": "project"}).json()
        vid = vague["note"]["id"]
        certain = client.post("/api/capture", json={
            "text": "Renew the parking permit by 2026-12-01", "bucket": "project"}).json()
        cid = certain["note"]["id"]

        d = client.get("/api/dashboard").json()
        pending = {p["note_id"]: p for p in d["pending_dates"]}
        check("a vague date joins the queue", vid in pending)
        check("a named date does not", cid not in pending)
        check("the queue quotes the words it read",
              pending[vid]["phrase"] == "end of the week", pending[vid])
        check("and says why it is asking",
              "two ways" in pending[vid]["why"], pending[vid]["why"])

        # "Looks right" — keeps the date, settles it.
        was = pending[vid]["deadline"]
        client.post(f"/api/notes/{vid}/deadline", json={"confirm": True})
        d = client.get("/api/dashboard").json()
        check("confirming empties the queue", d["pending_dates"] == [])
        kept = [p for p in d["projects"] if p["id"] == vid][0]["project"]
        check("confirming keeps the date", kept["deadline"] == was)
        check("confirming does not fake a source",
              kept["deadline_source"] == extract.AMBIGUOUS and kept["deadline_confirmed"])

        # Correcting it — becomes manual, which is confirmed by definition.
        client.post(f"/api/notes/{cid}/deadline", json={"date": "2026-09-30"})
        proj = [p for p in client.get("/api/dashboard").json()["projects"]
                if p["id"] == cid][0]["project"]
        check("a picked date is stored", proj["deadline"] == "2026-09-30")
        check("a picked date is manual", proj["deadline_source"] == extract.MANUAL)
        check("a picked date is confirmed", proj["deadline_confirmed"] is True)
        check("the corrected date reaches the calendar",
              "DUE;VALUE=DATE:20260930" in client.get("/calendar.ics").text)

        # A manual date survives a re-parse: re-reading the note is not a
        # request to overrule a choice already made.
        client.post(f"/api/notes/{cid}/reparse")
        proj = [p for p in client.get("/api/dashboard").json()["projects"]
                if p["id"] == cid][0]["project"]
        check("re-parsing keeps a hand-picked date", proj["deadline"] == "2026-09-30")

        # Clearing it. No deadline is a legitimate answer.
        client.post(f"/api/notes/{cid}/deadline", json={"date": None})
        proj = [p for p in client.get("/api/dashboard").json()["projects"]
                if p["id"] == cid][0]["project"]
        check("clearing removes the date", proj.get("deadline") is None)
        check("clearing is not 'confirmed nothing'", proj["deadline_confirmed"] is False)
        check("a cleared project has no task",
              not [t for t in client.get("/api/dashboard").json()["tasks"]
                   if t["note_id"] == cid])

        # The picker beside the capture buttons beats the text, no questions.
        picked = client.post("/api/capture", json={
            "text": "Book the flights next friday", "bucket": "project",
            "due": "2026-10-05"}).json()
        pp = picked["note"]["project"]
        check("the capture picker wins over the text", pp["deadline"] == "2026-10-05")
        check("the capture picker needs no confirmation",
              pp["deadline_confirmed"] is True and pp["deadline_source"] == extract.MANUAL)
        check("and it stays out of the queue",
              picked["note"]["id"] not in
              [p["note_id"] for p in client.get("/api/dashboard").json()["pending_dates"]])

        # A project captured before dates carried provenance: nothing to quote,
        # and the "next Friday" semantics changed underneath it, so it gets one
        # look rather than a silent pass.
        legacy = client.post("/api/capture", json={
            "text": "Old project by sept 8", "bucket": "project"}).json()["note"]["id"]
        raw = engine_note_path(c.vault, legacy)
        import re as _re
        aged = _re.sub(r"^\s+deadline_(confirmed|source|phrase):.*$", "",
                       raw.read_text(encoding="utf-8"), flags=_re.M)
        raw.write_text(_re.sub(r"\n{3,}", "\n", aged), encoding="utf-8")
        row = [p for p in client.get("/api/dashboard").json()["pending_dates"]
               if p["note_id"] == legacy]
        check("a pre-provenance project is offered once", len(row) == 1, row)
        check("and says so honestly rather than quoting nothing",
              row and "before dates were checked" in row[0]["why"],
              row[0]["why"] if row else None)

        # Bad input, and the wrong bucket.
        check("garbage is a 400",
              client.post(f"/api/notes/{vid}/deadline",
                          json={"date": "not a date"}).status_code == 400)
        area = client.post("/api/capture", json={"text": "Gym", "bucket": "area"}).json()
        check("an area has no deadline to set",
              client.post(f"/api/notes/{area['note']['id']}/deadline",
                          json={"date": "2026-09-01"}).status_code == 400)


# ==========================================================================
# the tutor
# ==========================================================================


def test_fsrs():
    section("fsrs scheduler")
    # The curve's defining identity: at t == S, recall probability is 90%.
    check("R(S, S) == 0.9", abs(fsrs.retrievability(10, 10) - 0.9) < 1e-9)
    check("R decays", fsrs.retrievability(10, 40) < fsrs.retrievability(10, 10))
    check("R of an unseen card is 0", fsrs.retrievability(0, 5) == 0.0)
    check("interval inverts the curve", abs(fsrs.interval_for(10, 0.9) - 10) < 1e-9)
    check("higher retention -> shorter interval",
          fsrs.interval_for(10, 0.95) < fsrs.interval_for(10, 0.85))

    # A first answer: better grades mean more initial stability, less difficulty.
    firsts = [fsrs.review(fsrs.Memory(), g) for g in fsrs.GRADES]
    check("initial stability rises with grade",
          [round(s.memory.stability, 3) for s in firsts] ==
          sorted(round(s.memory.stability, 3) for s in firsts))
    check("initial difficulty falls with grade",
          firsts[0].memory.difficulty > firsts[3].memory.difficulty)
    check("Good on a new card is a few days", 2 <= firsts[2].interval_days <= 5,
          firsts[2].interval_days)
    check("Easy on a new card is a couple of weeks", 10 <= firsts[3].interval_days <= 25,
          firsts[3].interval_days)

    now = dt.datetime(2026, 8, 23, 9, 0).astimezone()
    mature = fsrs.Memory(stability=30.0, difficulty=5.0, reps=6, lapses=0)
    last = now - dt.timedelta(days=30)

    graded = {g: fsrs.review(mature, g, last_review=last, now=now) for g in fsrs.GRADES}
    check("Again shortens to a day", graded[1].interval_days == 1)
    check("Again lowers stability", graded[1].memory.stability < mature.stability)
    check("a lapse never raises stability", graded[1].memory.stability <= mature.stability)
    check("Good raises stability", graded[3].memory.stability > mature.stability)
    check("intervals are ordered Hard < Good < Easy",
          graded[2].interval_days < graded[3].interval_days < graded[4].interval_days,
          [graded[g].interval_days for g in (2, 3, 4)])
    check("Again counts a lapse", graded[1].memory.lapses == 1 and graded[3].memory.lapses == 0)
    check("every review counts a rep", all(g.memory.reps == 7 for g in graded.values()))
    check("Again asks to be re-shown", graded[1].again is True and graded[3].again is False)

    # Difficulty reverts toward the mean rather than ratcheting — the fix for
    # SM-2's ease hell.
    hard_card = fsrs.Memory(stability=10, difficulty=9.5, reps=3)
    after_good = fsrs.review(hard_card, 3, last_review=last, now=now).memory.difficulty
    check("difficulty reverts after a Good", after_good < 9.5, after_good)
    check("difficulty stays in range", 1.0 <= after_good <= 10.0)

    # A same-day retry is not evidence of a spaced success.
    same_day = fsrs.review(mature, 3, last_review=now - dt.timedelta(hours=2), now=now)
    spaced = fsrs.review(mature, 3, last_review=last, now=now)
    check("same-day review earns far less than a spaced one",
          same_day.memory.stability < spaced.memory.stability)

    # A card reviewed later than scheduled has decayed further, so the same
    # Good is worth more.
    late = fsrs.review(mature, 3, last_review=now - dt.timedelta(days=60), now=now)
    early = fsrs.review(mature, 3, last_review=now - dt.timedelta(days=10), now=now)
    check("the spacing effect is real", late.memory.stability > early.memory.stability)

    check("bad grades are rejected",
          _raises(lambda: fsrs.review(fsrs.Memory(), 7)))
    check("maximum interval is honoured",
          fsrs.review(fsrs.Memory(stability=9e5, difficulty=3, reps=9), 4,
                      last_review=last, now=now, maximum_interval=365).interval_days == 365)

    ivs = fsrs.preview_intervals(mature, last_review=last, now=now)
    check("preview covers four buttons", set(ivs) == {"again", "hard", "good", "easy"})
    check("humanize reads sensibly",
          (fsrs.humanize(1), fsrs.humanize(45), fsrs.humanize(400)) == ("1d", "1.5mo", "1.1y"))


def _raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


def test_deck_roundtrip():
    section("deck files")
    deck = cardsmod.Deck(note_id="n1", subject="Rust generics", category="study")
    deck.add(front="What is a trait bound?", back="A constraint on a type parameter.",
             source="Trait bounds constrain type parameters.", status="active")
    deck.add(front="A {{monomorphized}} generic compiles per concrete type.",
             back="Zero-cost abstraction.", status="draft")
    deck.add(front="Multi-line?", back="one\n\ntwo\n\n```py\nx = 1\n```", status="active")

    text = cardsmod.dump(deck)
    back = cardsmod.loads(text)
    check("ids survive", [c.id for c in back.cards] == ["c1", "c2", "c3"])
    check("status survives", [c.status for c in back.cards] == ["active", "draft", "active"])
    check("multi-paragraph answers survive", back.cards[2].back.count("\n\n") == 2)
    check("code fences survive", "```py" in back.cards[2].back)
    check("cloze detected", back.cards[1].kind == "cloze" and back.cards[0].kind == "basic")
    check("cloze question hides the answer",
          "monomorphized" not in back.cards[1].question())
    check("cloze answer shows it", "monomorphized" in back.cards[1].answer())
    check("subject and category survive",
          back.subject == "Rust generics" and back.category == "study")

    # The body is the list of cards that exist: type one in Obsidian and it is
    # a real card; delete one and its scheduling row goes with it.
    typed = text + "\n### Card c9\n\n**Q.** Hand written?\n\n**A.** Yes.\n"
    with_typed = cardsmod.loads(typed)
    check("hand-written card is picked up", len(with_typed.cards) == 4)
    check("hand-written card is active, not a draft",
          with_typed.card("c9").status == "active")

    trimmed = cardsmod.loads(cardsmod.dump(with_typed).replace(
        "### Card c2  ·  *awaiting review*", "### Card c2  ·  *awaiting review*\n<!--", 1))
    check("a deck with odd markup still loads", isinstance(trimmed, cardsmod.Deck))

    # Scheduling round-trips through the frontmatter.
    card = back.card("c1")
    card.apply(fsrs.review(card.memory, 3))
    saved = cardsmod.loads(cardsmod.dump(back)).card("c1")
    check("stability persists", abs(saved.stability - card.stability) < 1e-6)
    check("due date persists", saved.due == card.due)
    check("reps persist", saved.reps == 1)
    check("last review persists", saved.last_review is not None)

    with tempfile.TemporaryDirectory() as tmp:
        store = cardsmod.DeckStore(Path(tmp))
        store.save(back)
        check("store round-trips", len(store.get("n1").cards) == 3)
        check("store lists decks", [d.note_id for d in store.all()] == ["n1"])
        check("README written, not treated as a deck", (store.root / "README.md").exists())
        store.log_review({"at": "2026-08-23T10:00:00", "grade": 3})
        store.log_review({"at": "bad", "grade": 1})
        check("review log reads back", len(list(store.reviews())) == 2)
        (store.root / cardsmod.REVIEW_LOG).write_text(
            '{"at": "2026-08-23T10:00:00", "grade": 3}\nnot json\n', encoding="utf-8")
        check("a corrupt log line is skipped, not fatal", len(list(store.reviews())) == 1)


def test_card_generation():
    section("card generation")
    body = (
        "# Photosynthesis\n\n"
        "Photosynthesis is the process by which plants convert light energy into "
        "chemical energy stored as glucose. It happens in the chloroplasts.\n\n"
        "The light-dependent reactions occur in the thylakoid membrane and produce "
        "ATP and NADPH, which the Calvin cycle then consumes to fix carbon dioxide.\n\n"
        "## Steps\n\n"
        "- [ ] Read chapter 4 (45m)\n- [ ] Do the problem set (60m)\n\n"
        "## Skills\n\n#biology #cell-biology\n"
    )
    passages = generate.chunk(body)
    check("prose is chunked", len(passages) >= 1, passages)
    joined = " ".join(passages)
    check("our own Steps boilerplate is excluded", "problem set" not in joined)
    check("our own Skills boilerplate is excluded", "cell-biology" not in joined)
    check("real content is kept", "thylakoid" in joined)
    check("frontmatter is stripped",
          "id:" not in " ".join(generate.chunk("---\nid: x\ntitle: y\n---\n\n" + body)))

    cfg = Config()
    good = {"cards": [
        {"q": "Where do the light-dependent reactions occur?",
         "a": "The thylakoid membrane",
         "why": "The light-dependent reactions occur in the thylakoid membrane"},
        # rejected: the quote is not in the passage
        {"q": "What colour is chlorophyll?", "a": "Green", "why": "Chlorophyll is green."},
        # rejected: unanswerable outside its passage
        {"q": "What does the above describe?", "a": "Photosynthesis", "why": ""},
        # rejected: the answer restates the question
        {"q": "The Calvin cycle?", "a": "The Calvin cycle", "why": ""},
        # kept, but the missing "?" is added
        {"q": "The Calvin cycle consumes ATP and", "a": "NADPH", "why": ""},
    ]}
    original = generate.resolve_provider
    generate.resolve_provider = lambda c, role='': FakeLLM(good)
    try:
        r = generate.generate(passages[0] if len(passages) == 1 else body, cfg,
                              subject="Photosynthesis", max_cards=20)
        fronts = [c.front for c in r.cards]
        check("valid card kept", any("thylakoid membrane" in c.back for c in r.cards))
        check("fabricated quote is dropped, card kept",
              all(c.source == "" for c in r.cards if c.back == "Green")
              or not any(c.back == "Green" for c in r.cards))
        check("real quote is kept",
              any("thylakoid" in c.source for c in r.cards), [c.source for c in r.cards])
        check("'the above' question rejected",
              not any("above" in f.lower() for f in fronts), fronts)
        check("restated answer rejected",
              not any(f.strip("?").lower() == "the calvin cycle" for f in fronts), fronts)
        check("missing question mark added", all(f.rstrip().endswith("?") for f in fronts), fronts)
        check("something was rejected", r.rejected >= 2, r.rejected)
        check("everything lands as a draft", all(c.status == "draft" for c in r.cards))
        check("duplicates are not re-added",
              generate.generate(body, cfg, existing=r.cards, max_cards=20).rejected > 0)
    finally:
        generate.resolve_provider = original

    # No model: cloze cards from definition sentences, quoting the note verbatim.
    cfg.llm.provider = "heuristic"
    r2 = generate.generate(body, cfg, subject="Photosynthesis", max_cards=10)
    check("offline path still produces cards", len(r2.cards) >= 1, r2.note)
    check("offline path is flagged degraded", r2.degraded is True)
    check("offline cards are cloze", all(c.kind == "cloze" for c in r2.cards))
    check("offline cards quote the note",
          all(c.source in body.replace("\n", " ") or c.source in body for c in r2.cards))
    check("an empty note produces nothing, and says so",
          generate.generate("", cfg).cards == [] and generate.generate("", cfg).note)


def _deck_with(note_id, subject, n, due_offset=0, category="study"):
    deck = cardsmod.Deck(note_id=note_id, subject=subject, category=category)
    for i in range(n):
        card = deck.add(front=f"{subject} q{i}?", back=f"a{i}", status="active")
        card.stability = 10.0
        card.difficulty = 5.0
        card.reps = 2
        card.due = dt.date.today() + dt.timedelta(days=due_offset)
        card.last_review = dt.datetime.now().astimezone() - dt.timedelta(days=10)
    return deck


def test_session_mix():
    section("study session")
    cfg = Config()
    cfg.study.new_cards_per_day = 5
    cfg.study.session_size = 40

    rust = _deck_with("n-rust", "Rust", 12, due_offset=-1)
    bio = _deck_with("n-bio", "Biology", 3, due_offset=-5)
    spanish = _deck_with("n-es", "Spanish", 0)
    for i in range(8):  # unseen cards
        spanish.add(front=f"hola {i}?", back="hello", status="active")

    s = tutor.build_session([rust, bio, spanish], cfg)
    subjects = [q.deck.subject for q in s.queue]
    check("due cards are all picked up", s.due_available == 15, s.due_available)
    check("new cards are capped per day",
          sum(1 for q in s.queue if q.reason == "new") == 5,
          [q.reason for q in s.queue])
    check("every subject appears", set(subjects) == {"Rust", "Biology", "Spanish"}, subjects)

    # Interleaving: the three-card deck should not be finished in the first
    # three slots, and runs of one subject should be short.
    bio_positions = [i for i, x in enumerate(subjects) if x == "Biology"]
    check("a small deck is spread across the session",
          max(bio_positions) - min(bio_positions) > len(subjects) / 3,
          bio_positions)
    longest_run = max_run = 1
    for a, b in zip(subjects, subjects[1:]):
        max_run = max_run + 1 if a == b else 1
        longest_run = max(longest_run, max_run)
    check("subjects do not clump", longest_run <= 4, longest_run)

    # Picking subjects narrows the pool.
    only_bio = tutor.build_session([rust, bio, spanish], cfg, subjects=["n-bio"])
    check("subject filter applies",
          {q.deck.subject for q in only_bio.queue} == {"Biology"})

    # Most overdue first, in priority terms.
    check("the most overdue deck is represented early",
          "Biology" in subjects[: max(3, len(subjects) // 3)], subjects[:6])

    # Caps.
    small = tutor.build_session([rust, bio, spanish], cfg, limit=4)
    check("session size caps the queue", len(small.queue) == 4 and small.capped is True)
    maxed = tutor.build_session([rust, bio, spanish], cfg, reviewed_today=999)
    check("the daily review cap is respected",
          all(q.reason == "new" for q in maxed.queue), [q.reason for q in maxed.queue])
    check("nothing due reads as nothing due",
          "Nothing due" in tutor.build_session([], cfg).message)

    # Blocked practice is available for anyone who wants it.
    cfg.study.interleave = False
    blocked = [q.deck.subject for q in tutor.build_session([rust, bio, spanish], cfg).queue]
    check("interleave: false groups by deck", blocked[0] == blocked[1], blocked[:4])

    # Ordering is stable within a day, so a reload does not reshuffle.
    cfg.study.interleave = True
    again = [q.card.id + q.deck.note_id for q in tutor.build_session([rust, bio, spanish], cfg).queue]
    once = [q.card.id + q.deck.note_id for q in tutor.build_session([rust, bio, spanish], cfg).queue]
    check("the same day gives the same order", again == once)


def test_recall_grading():
    section("free-recall marking")
    cfg = Config()
    check("a perfect score is Easy", tutor.score_to_grade(1.0) == fsrs.EASY)
    check("a good score is Good", tutor.score_to_grade(0.8) == fsrs.GOOD)
    check("a partial score is Hard", tutor.score_to_grade(0.5) == fsrs.HARD)
    check("a poor score is Again", tutor.score_to_grade(0.2) == fsrs.AGAIN)

    original = tutor.resolve_provider
    tutor.resolve_provider = lambda c, role='': FakeLLM(
        {"score": 0.5, "missed": "the thylakoid membrane", "feedback": "Half of it."}
    )
    try:
        g = tutor.grade_recall("Where?", "The thylakoid membrane", "in the chloroplast", cfg)
        check("the model's score is used", g.score == 0.5 and g.grade == fsrs.HARD)
        check("feedback comes back", g.feedback == "Half of it.")
        check("what was missed comes back", "thylakoid" in g.missed)

        tutor.resolve_provider = lambda c, role='': FakeLLM({"score": 4.7})
        check("an out-of-range score is clamped",
              tutor.grade_recall("q", "a", "x", cfg).score == 1.0)

        tutor.resolve_provider = lambda c, role='': FakeLLM(RuntimeError("model down"))
        fallback = tutor.grade_recall("q", "the thylakoid membrane", "thylakoid membrane", cfg)
        check("a model failure falls back to the offline marker",
              fallback.graded_by == "rule")
        check("the offline marker recognises the right words", fallback.grade >= fsrs.HARD)
        check("the offline marker never awards Easy",
              tutor.grade_recall("q", "abc def", "abc def", cfg).grade != fsrs.EASY)
        check("an empty answer is Again",
              tutor.grade_recall("q", "a", "   ", cfg).grade == fsrs.AGAIN)
        check("nonsense is Again",
              tutor.grade_recall("q", "the thylakoid membrane inside chloroplasts",
                                 "zebra pancake", cfg).grade == fsrs.AGAIN)
    finally:
        tutor.resolve_provider = original


def test_progress_and_mastery():
    section("progress and mastery")
    cfg = Config()
    today = dt.date.today()

    per_day = {(today - dt.timedelta(days=i)).isoformat(): 3 for i in range(0, 5)}
    check("streak counts back from today", tutor._streak(per_day, today) == 5)
    del per_day[today.isoformat()]
    check("an unstudied today does not break the streak — the day is not over",
          tutor._streak(per_day, today) == 4)
    del per_day[(today - dt.timedelta(days=1)).isoformat()]
    check("an unstudied yesterday does", tutor._streak(per_day, today) == 0)
    check("longest streak spans gaps",
          tutor._longest_streak({
              "2026-01-01": 1, "2026-01-02": 1, "2026-01-03": 1, "2026-01-09": 1,
          }) == 3)

    # Mastery needs both enough spaced reps and enough predicted staying power.
    deck = cardsmod.Deck(note_id="n1", subject="Rust")
    for i in range(8):
        c = deck.add(front=f"q{i}?", back="a", status="active")
        c.reps, c.stability, c.difficulty = 6, 40.0, 5.0
        c.due = today + dt.timedelta(days=30)
        c.last_review = dt.datetime.now().astimezone()
    check("a well-drilled deck is mastered", tutor.is_ready_to_graduate(deck, cfg))

    deck.cards[0].reps = 1
    deck.cards[1].reps = 1
    check("mastery is a proportion, and 75% is not enough",
          not tutor.is_ready_to_graduate(deck, cfg),
          tutor.deck_progress(deck, cfg)["mastery"])

    fresh = cardsmod.Deck(note_id="n2", subject="Tiny")
    for i in range(3):
        c = fresh.add(front=f"q{i}?", back="a", status="active")
        c.reps, c.stability = 9, 200.0
        c.last_review = dt.datetime.now().astimezone()
    check("a three-card deck cannot graduate however well it goes",
          not tutor.is_ready_to_graduate(fresh, cfg))

    high_reps_low_stability = cardsmod.Deck(note_id="n3", subject="Crammed")
    for i in range(8):
        c = high_reps_low_stability.add(front=f"q{i}?", back="a", status="active")
        c.reps, c.stability = 12, 2.0  # answered a lot, remembered for two days
        c.last_review = dt.datetime.now().astimezone()
    check("cramming is not mastery",
          not tutor.is_ready_to_graduate(high_reps_low_stability, cfg))

    p = tutor.deck_progress(deck, cfg)
    check("progress counts drafts separately", p["drafts"] == 0 and p["active"] == 8)
    check("progress reports retention", 0.0 <= p["retention"] <= 1.0)


def test_study_api():
    section("study api (end to end)")
    try:
        from starlette.testclient import TestClient
    except ImportError:
        print("  skip (no starlette testclient)")
        return

    body = (
        "Mitochondria are the organelles that generate most of the cell's ATP. "
        "The citric acid cycle is a series of reactions that releases stored energy. "
        "Oxidative phosphorylation is the final stage of cellular respiration and "
        "produces the bulk of the ATP a cell uses."
    )

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(vault=Path(tmp) / "vault")
        cfg.llm.provider = "heuristic"  # offline path, no mocking needed
        cfg.calendar.sink = "ics"
        from sb.api import build_app

        client = TestClient(build_app(cfg))
        r = client.post("/api/capture", json={"text": body, "bucket": "resource"})
        nid = r.json()["note"]["id"]

        check("study page serves", client.get("/study").status_code == 200)
        over = client.get("/api/study/overview").json()
        check("a fresh vault has no decks", over["decks"] == [])
        check("notes without cards are offered", any(c["note_id"] == nid for c in over["candidates"]))

        gen = client.post(f"/api/decks/{nid}/generate", json={}).json()
        check("cards generated", gen["generated"] >= 2, gen)
        deck = gen["deck"]
        check("all generated cards are drafts",
              all(c["status"] == "draft" for c in deck["cards"]))
        check("drafts are not schedulable yet", deck["active"] == 0)

        session = client.post("/api/study/session", json={}).json()
        check("drafts do not reach a session", session["queue"] == [], session)

        client.post(f"/api/decks/{nid}/approve", json={})
        deck = client.get(f"/api/decks/{nid}").json()
        check("approval activates the drafts", deck["active"] == len(deck["cards"]))

        session = client.post("/api/study/session", json={}).json()
        check("approved cards reach a session", len(session["queue"]) >= 2, session)
        first = session["queue"][0]
        check("the queue withholds the answer",
              first["answer"] == "" and first["back"] == "" and first["front"] == "",
              first)
        check("the queue carries the question", bool(first["question"]))
        check("a cloze question keeps its blank hidden",
              "[ ... ]" in first["question"] or first["kind"] == "basic")

        full = client.get(f"/api/study/{nid}/{first['id']}/reveal").json()
        check("reveal returns the answer", bool(full["answer"]))
        check("reveal returns four intervals", set(full["intervals"]) ==
              {"again", "hard", "good", "easy"})

        ans = client.post(f"/api/study/{nid}/{first['id']}/answer",
                          json={"grade": 3, "seconds": 4.2}).json()
        check("answering schedules the card", ans["interval_days"] >= 1)
        check("answering sets a due date", ans["due"] is not None)
        check("answering does not ask for it again", ans["again"] is False)

        def reps_of(card_id):
            deck = client.get(f"/api/decks/{nid}").json()
            return [c for c in deck["cards"] if c["id"] == card_id][0]["reps"]

        before_mark = reps_of(first["id"])
        marked = client.post(f"/api/study/{nid}/{first['id']}/mark",
                             json={"typed": "no idea"}).json()
        check("marking works offline", marked["graded_by"] == "rule")
        check("marking shows the answer", bool(marked["answer"]))
        # Marking proposes; only answering disposes. A model that marks you
        # wrong should cost a click, not a card.
        check("marking does not schedule anything", reps_of(first["id"]) == before_mark)

        again = client.post(f"/api/study/{nid}/{first['id']}/answer",
                            json={"grade": 1, "mode": "recall", "typed": "no idea"}).json()
        check("Again asks for the card again", again["again"] is True)
        check("Again comes back tomorrow", again["interval_days"] == 1)

        stats = client.get("/api/study/stats").json()
        check("reviews are logged", stats["reviews_total"] == 2, stats["reviews_total"])
        check("today's count is right", stats["today"] == 2)
        check("the streak starts at one", stats["streak"] == 1)
        check("the forecast covers a fortnight", len(stats["forecast"]) == 14)
        check("the heatmap ends today",
              stats["heatmap"][-1]["date"] == dt.date.today().isoformat())

        # Hand-written cards, edits and deletion.
        client.post(f"/api/decks/{nid}/cards", json={"front": "Typed?", "back": "Yes"})
        deck = client.get(f"/api/decks/{nid}").json()
        typed_card = [c for c in deck["cards"] if c["front"] == "Typed?"][0]
        check("a typed card is active immediately", typed_card["status"] == "active")
        client.post(f"/api/decks/{nid}/cards/{typed_card['id']}",
                    json={"back": "Definitely"})
        deck = client.get(f"/api/decks/{nid}").json()
        check("editing keeps the id and changes the text",
              [c for c in deck["cards"] if c["id"] == typed_card["id"]][0]["back"] == "Definitely")
        client.post(f"/api/decks/{nid}/cards/{typed_card['id']}", json={"delete": True})
        check("deleting removes it",
              not [c for c in client.get(f"/api/decks/{nid}").json()["cards"]
                   if c["id"] == typed_card["id"]])

        check("a bad status is rejected",
              client.post(f"/api/decks/{nid}/cards/{first['id']}",
                          json={"status": "banana"}).status_code == 400)
        check("a card with no answer is rejected",
              client.post(f"/api/decks/{nid}/cards", json={"front": "x"}).status_code == 400)
        check("a deck that does not exist is a 400",
              client.get("/api/decks/nope").status_code in (400, 500))

        # The dashboard learns about it, and the calendar gains a study block.
        d = client.get("/api/dashboard").json()
        check("the dashboard reports the tutor", d["study"]["decks"] == 1)
        check("the dashboard counts today's reviews", d["study"]["reviewed_today"] == 2)
        ics = client.get("/calendar.ics").text
        check("a daily study block reaches the calendar",
              "study-session@" in ics and "FREQ=DAILY" in ics)

        # Graduating a Project carries its deck: decks are keyed by note id,
        # not by the folder the note happens to be sitting in.
        p = client.post("/api/capture",
                        json={"text": "Learn the Krebs cycle\n" + body, "bucket": "project"})
        pid = p.json()["note"]["id"]
        client.post(f"/api/decks/{pid}/generate", json={})
        client.post(f"/api/decks/{pid}/approve", json={})
        before = client.get(f"/api/decks/{pid}").json()["active"]
        client.post(f"/api/notes/{pid}/move", json={"bucket": "resource"})
        after = client.get(f"/api/decks/{pid}").json()
        check("a graduated note keeps its deck", after["active"] == before and before > 0)


def test_graduation_prompt():
    section("graduation prompt")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(vault=Path(tmp) / "vault")
        cfg.llm.provider = "heuristic"
        cfg.calendar.sink = "ics"
        engine = Engine(cfg)

        result = engine.capture("Learn the Krebs cycle, it is a learning project", "project")
        note = engine.note(result["note"]["id"])
        note.project.learning = True
        engine.vault.save(note)

        deck = engine.deck(note.id, create=True)
        for i in range(8):
            c = deck.add(front=f"q{i}?", back="a", status="active")
            c.reps, c.stability, c.difficulty = 6, 45.0, 5.0
            c.due = dt.date.today() + dt.timedelta(days=30)
            c.last_review = dt.datetime.now().astimezone()
        engine.decks.save(deck)

        check("a mastered learning project is offered for graduation",
              [g["note_id"] for g in engine.graduation_candidates()] == [note.id])
        check("the dashboard carries the prompt",
              len(engine.dashboard()["study"]["graduation"]) == 1)

        # A non-learning project with the same numbers is never offered: the
        # graduate-to-Resource lifecycle is for learning material only (§4).
        other = engine.capture("Pack for school", "project")
        oid = other["note"]["id"]
        odeck = engine.deck(oid, create=True)
        for i in range(8):
            c = odeck.add(front=f"q{i}?", back="a", status="active")
            c.reps, c.stability = 6, 45.0
            c.last_review = dt.datetime.now().astimezone()
        engine.decks.save(odeck)
        check("a non-learning project is never offered",
              oid not in [g["note_id"] for g in engine.graduation_candidates()])

        # Answering marks the note as awaiting confirmation, but does not move
        # it — the human confirms (§4).
        card = deck.cards[0]
        engine.study_answer(note.id, card.id, grade=3)
        moved = engine.note(note.id)
        check("the note is flagged as graduating",
              moved.project.status.value == "graduating")
        check("but it has not moved on its own", moved.bucket.value == "project")
        check("the srs rollup lands in the frontmatter",
              moved.srs is not None and moved.srs.mastery >= 0.85)


# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# body preservation, structured materials, and link-following generation
# --------------------------------------------------------------------------


def test_body_preservation():
    """The bug this prevents: a Project body was regenerated wholesale from
    frontmatter, so any heading the renderer did not write itself — an
    Assignment's ## Answers, a Quiz's ## Key Concepts — was silently deleted
    the next time a step was ticked or a deadline edited."""
    section("body preservation")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(vault=Path(tmp) / "vault")
        cfg.llm.provider = "heuristic"
        cfg.calendar.sink = "ics"
        engine = Engine(cfg)

        result = engine.capture("Econ chapter 3 assignment", "project")
        note = engine.note(result["note"]["id"])
        note.project.steps = [Step(id="s1", text="Answer the questions", minutes=30)]
        engine.vault.save(note)
        note = engine.note(note.id)

        # lj writes an answer into the note by hand, in Obsidian.
        note.body = note.body.replace(
            "## Capture",
            "## Answers\n\nResources are not equally suited to both goods.\n\n## Capture",
        )
        engine.vault.save(note)

        # ...and then does something perfectly ordinary through the dashboard.
        step_id = engine.note(note.id).project.steps[0].id
        engine.toggle_step(note.id, step_id)
        after = engine.note(note.id)

        check("a hand-written section survives a step toggle",
              "## Answers" in after.body)
        check("and so does its content",
              "Resources are not equally suited" in after.body)
        check("the renderer's own sections are still rewritten",
              after.body.count("## Steps") == 1)
        check("the preserved section is not duplicated",
              after.body.count("## Answers") == 1)

        # Idempotence: rendering twice more must not stack headings or lose text.
        engine.toggle_step(note.id, step_id)
        engine.toggle_step(note.id, step_id)
        twice = engine.note(note.id)
        check("still exactly one copy after three renders",
              twice.body.count("## Answers") == 1)
        check("capture is not nested",
              twice.body.count("## Capture") == 1)


def test_materials_kinds():
    section("materials — one list, three kinds")
    from sb.models import Material, MaterialKind

    # Old notes on disk store materials as bare strings. Reading one has to
    # keep working: nobody is migrating the vault by hand.
    legacy = ProjectMeta(materials=["The Rust Book ch.10"])
    check("a legacy string material still loads",
          legacy.materials[0].text == "The Rust Book ch.10")
    check("and defaults to kind=material",
          legacy.materials[0].kind == MaterialKind.MATERIAL)

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(vault=Path(tmp) / "vault")
        cfg.llm.provider = "heuristic"
        cfg.calendar.sink = "ics"
        engine = Engine(cfg)

        result = engine.capture("Build a mechanical keyboard", "project")
        note = engine.note(result["note"]["id"])
        note.project.steps = [Step(id="s1", text="Solder the switches", minutes=30)]
        note.project.materials = [
            Material(text="Soldering iron", kind=MaterialKind.HARDWARE),
            Material(text="KiCad", kind=MaterialKind.SOFTWARE),
            Material(text="Switch datasheet"),
        ]
        engine.vault.save(note)
        engine.toggle_step(note.id, note.project.steps[0].id)
        rendered = engine.note(note.id).body

        check("hardware renders as its own subsection", "### Hardware" in rendered)
        check("software too", "### Software" in rendered)
        check("plain materials stay under the main heading",
              "- [ ] Switch datasheet" in rendered)

        # Ticking a box in Obsidian is a real state change, not a note the
        # app overwrites on its next render.
        note = engine.note(note.id)
        note.body = rendered.replace("- [ ] KiCad", "- [x] KiCad")
        engine.vault.save(note)
        engine.toggle_step(note.id, note.project.steps[0].id)
        after = engine.note(note.id)
        kicad = [m for m in after.project.materials if m.text == "KiCad"][0]
        check("ticking a material in the body reaches frontmatter", kicad.done is True)
        check("and survives the re-render", "- [x] KiCad" in after.body)


def test_materials_absorbed():
    """Hardware and Software were hand-written headings before they were a
    tracked kind. Re-rendering an old note must migrate them, not delete
    them."""
    section("materials — absorbing the old body sections")
    from sb.models import MaterialKind

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(vault=Path(tmp) / "vault")
        cfg.llm.provider = "heuristic"
        cfg.calendar.sink = "ics"
        engine = Engine(cfg)

        result = engine.capture("Build a shelf", "project")
        note = engine.note(result["note"]["id"])
        note.project.steps = [Step(id="s1", text="Cut the boards", minutes=30)]
        note.project.materials = []
        note.body = note.body.replace(
            "## Capture",
            "## Hardware\n\n- Circular saw\n\n## Software\n\n- [x] SketchUp\n\n## Capture",
        )
        engine.vault.save(note)
        engine.toggle_step(note.id, note.project.steps[0].id)
        after = engine.note(note.id)

        kinds = {m.text: m.kind for m in after.project.materials}
        check("an old Hardware bullet becomes tracked state",
              kinds.get("Circular saw") == MaterialKind.HARDWARE)
        check("and an old Software bullet too",
              kinds.get("SketchUp") == MaterialKind.SOFTWARE)
        check("its checked state comes across",
              [m for m in after.project.materials if m.text == "SketchUp"][0].done is True)
        check("the old headings are not left behind as duplicates",
              after.body.count("Circular saw") == 1)


def test_link_expansion():
    """A Quiz note is a list of links. Before this, generating cards from one
    read a page of link syntax and correctly found nothing to test."""
    section("card generation follows wikilinks")

    vault = {
        "opportunity cost": "# Opportunity Cost\n\n## Definition\n\nThe value of the next best alternative given up.",
        "sunk cost": "# Sunk Cost\n\n## Definition\n\nA cost already incurred and unrecoverable.",
    }

    def resolve(title):
        return vault.get(title.strip().lower())

    text = "- [ ] [[Opportunity Cost]]\n- [ ] [[Sunk Cost]]\n- [ ] [[Never Written]]"
    out = generate.expand_links(text, resolve)

    check("a linked note's body is pulled in",
          "next best alternative" in out)
    check("more than one link resolves", "already incurred" in out)
    check("a link pointing nowhere is skipped, not fatal",
          "Never Written" in out and out.count("## Never Written") == 0)
    check("the linked note gets a heading so the chunker can see it",
          "## Opportunity Cost" in out)
    check("its own H1 is not duplicated under that heading",
          out.count("# Opportunity Cost") == 1)

    # Aliases and heading anchors are ordinary Obsidian link syntax.
    aliased = generate.expand_links("[[Sunk Cost|the sunk cost fallacy]]", resolve)
    check("an aliased link resolves on its target", "already incurred" in aliased)
    anchored = generate.expand_links("[[Sunk Cost#Definition]]", resolve)
    check("a heading anchor resolves too", "already incurred" in anchored)

    # One level deep, deliberately: following transitively turns "cards from
    # this quiz" into "cards from the whole vault".
    deep = {"a": "# A\n\nSee [[B]].", "b": "# B\n\nThe hidden fact."}
    out2 = generate.expand_links("[[A]]", lambda t: deep.get(t.strip().lower()))
    check("expansion does not recurse", "The hidden fact" not in out2)

    check("a repeated link is only expanded once",
          generate.expand_links("[[Sunk Cost]] and [[Sunk Cost]]", resolve).count("already incurred") == 1)

    # The whole point: cards now come out of a link-only note.
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(vault=Path(tmp) / "vault")
        cfg.llm.provider = "heuristic"
        cfg.calendar.sink = "ics"
        engine = Engine(cfg)

        atomic = engine.capture(
            "Opportunity cost is the value of the next best alternative given up "
            "when a choice is made between competing options.",
            "resource",
        )
        anote = engine.note(atomic["note"]["id"])
        anote.title = "Opportunity Cost"
        engine.vault.save(anote)

        quiz = engine.capture("Econ quiz 1", "project")
        qnote = engine.note(quiz["note"]["id"])
        qnote.body = "# Econ quiz 1\n\n## Key Concepts\n\n- [ ] [[Opportunity Cost]]\n"
        engine.vault.save(qnote)

        expanded = generate.expand_links(qnote.body, engine._link_resolver())
        check("the engine's resolver finds a note by title",
              "next best alternative" in expanded)
        check("a link-only quiz now yields passages to test",
              len(generate.chunk(expanded)) > 0)


# --------------------------------------------------------------------------
# not reading the whole vault, and smart connections
# --------------------------------------------------------------------------


def _count_reads(vault):
    """Wrap Vault.read so a test can assert how many files an operation opened.

    Counting reads rather than timing is the point: a performance promise that
    is not asserted is a performance promise that quietly stops being true.
    """
    calls = {"n": 0}
    original = vault.read

    def counting(path):
        calls["n"] += 1
        return original(path)

    vault.read = counting
    return calls


def test_link_resolution_is_cheap():
    section("link resolution reads only what is linked")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(vault=Path(tmp) / "vault")
        cfg.llm.provider = "heuristic"
        cfg.calendar.sink = "ics"
        engine = Engine(cfg)

        # A vault with some bulk in it, and two notes worth linking to.
        for i in range(12):
            engine.capture(f"Filler resource number {i} about nothing", "resource")
        for title, text in [
            ("Opportunity Cost", "The value of the next best alternative given up."),
            ("Sunk Cost", "A cost already incurred and unrecoverable."),
        ]:
            r = engine.capture(text, "resource")
            n = engine.note(r["note"]["id"])
            n.title = title
            engine.vault.save(n)

        quiz = engine.capture("Econ quiz", "project")
        qnote = engine.note(quiz["note"]["id"])
        qnote.body = "## Key Concepts\n\n- [[Opportunity Cost]]\n- [[Sunk Cost]]\n"
        engine.vault.save(qnote)

        total = len(engine.vault.notes())
        calls = _count_reads(engine.vault)
        resolve = engine._link_resolver()
        resolve("Opportunity Cost")
        resolve("Sunk Cost")
        resolve("Sunk Cost")          # cached
        resolve("Does Not Exist")     # resolves to nothing, reads nothing

        check("two linked notes cost two reads, not a whole vault",
              calls["n"] == 2, f"read {calls['n']} of {total}")
        check("a retitled note is renamed so its links still resolve",
              engine.vault.resolve_title("Opportunity Cost") is not None)
        check("the vault is big enough for that to mean something", total >= 14)
        check("resolution still works", resolve("Opportunity Cost") is not None)
        check("a link pointing nowhere is None", resolve("Does Not Exist") is None)

        # Renaming a file by hand in Obsidian must not break its links.
        path, note = engine.vault.get(qnote.id)
        renamed = path.with_name("Econ Quiz Renamed.md")
        path.rename(renamed)
        check("a hand-renamed file still resolves by its filename",
              engine.vault.resolve_title("Econ Quiz Renamed") is not None)


def test_capture_reads_once():
    section("capture does not walk the vault three times")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(vault=Path(tmp) / "vault")
        cfg.llm.provider = "heuristic"
        cfg.calendar.sink = "ics"
        engine = Engine(cfg)

        for i in range(10):
            engine.capture(f"Existing project {i}", "project")

        existing = len(engine.vault.notes())
        calls = _count_reads(engine.vault)
        engine.capture("Learn Rust generics", "project", due="2099-01-15")

        # The planner, the Area blocks and the calendar sync used to take a
        # walk each. One pass over the vault is the floor; two would mean a
        # duplicate crept back in.
        check("one capture reads the vault about once, not three times",
              calls["n"] <= existing + 2, f"{calls['n']} reads for {existing} notes")
        check("and the note still landed", len(engine.vault.notes()) == existing + 1)

        # The freshly written note has to reach the calendar even though the
        # snapshot was taken before it existed. A deadline is what gives it
        # something to put there.
        ics = cfg.ics_path.read_text(encoding="utf-8")
        check("the new note reaches the calendar from the snapshot",
              "Learn Rust generics" in ics)


def test_connect_sections():
    section("connect — the Related section")
    from sb import connect as connectmod

    body = "# T\n\n## Definition\n\nstuff\n\n## Capture\n\nraw text\n"
    links = [connectmod.Link(title="Sunk Cost", why="the other half of the idea")]

    note = Note(id="x", title="T", body=body)
    changed = connectmod.apply(note, links)
    check("applying writes a Related section", "## Related" in note.body)
    check("with the link", "[[Sunk Cost]]" in note.body)
    check("and its reason", "the other half of the idea" in note.body)
    check("marked as machine-suggested", connectmod.MARKER in note.body)
    check("it reports the change", changed is True)
    check("Capture stays last so re-render stays idempotent",
          note.body.index("## Related") < note.body.index("## Capture"))
    check("the original content survives", "raw text" in note.body)

    # Re-running replaces rather than stacking.
    first = note.body
    connectmod.apply(note, links)
    check("re-running does not duplicate the section",
          note.body.count("## Related") == 1)
    check("and is stable", note.body.strip() == first.strip())

    # A Related section lj wrote by hand is not ours to touch.
    manual = Note(id="y", title="T", body="# T\n\n## Related\n\n- [[Mine]]\n")
    kept = connectmod.strip_related(manual.body)
    check("a hand-written Related section is left alone", "[[Mine]]" in kept)

    # Removing every link removes the section.
    connectmod.apply(note, [])
    check("empty links clears the section", "## Related" not in note.body)
    check("without eating the rest", "raw text" in note.body)

    # Links already in the note are not re-proposed.
    seen = connectmod.existing_links("see [[Alpha]] and [[Beta|b]] and [[Gamma#h]]")
    check("existing links are found, aliases and anchors included",
          seen == {"alpha", "beta", "gamma"})


def test_filename_repair():
    section("filenames follow titles")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(vault=Path(tmp) / "vault")
        cfg.llm.provider = "heuristic"
        cfg.calendar.sink = "ics"
        engine = Engine(cfg)

        r = engine.capture("Some long rambling capture about economics", "resource")
        note = engine.note(r["note"]["id"])
        note.title = "Opportunity Cost"
        engine.vault.save(note)

        path, _ = engine.vault.get(note.id)
        check("saving a retitled note renames its file",
              path.stem.startswith("opportunity-cost--"), path.name)
        check("and the link now resolves",
              engine.vault.resolve_title("Opportunity Cost") is not None)

        # A file lj renamed by hand in Obsidian is not ours to correct.
        manual = engine.capture("Another note entirely", "resource")
        mnote = engine.note(manual["note"]["id"])
        mpath, _ = engine.vault.get(mnote.id)
        hand = mpath.with_name("My Own Name.md")
        mpath.rename(hand)
        engine.vault.repair_filenames()
        check("a hand-renamed file is left alone", hand.exists())

        # Repair fixes drift that predates the rename-on-save rule.
        legacy = engine.capture("Legacy note text here", "resource")
        lnote = engine.note(legacy["note"]["id"])
        lpath, _ = engine.vault.get(lnote.id)
        lnote.title = "Sunk Cost"
        lpath.write_text(
            lpath.read_text(encoding="utf-8").replace(
                "title: Legacy note text here", "title: Sunk Cost"),
            encoding="utf-8")
        check("drift exists before repair",
              engine.vault.resolve_title("Sunk Cost") is None)
        fixed = engine.vault.repair_filenames()
        check("repair renames it", any("sunk-cost" in f for f in fixed), str(fixed))
        check("and the link resolves after",
              engine.vault.resolve_title("Sunk Cost") is not None)


def test_find_by_id_is_cheap():
    """`note()` and `save()` both go through `find`, so a whole-vault scan
    here taxed every single operation in the system."""
    section("finding a note by id")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(vault=Path(tmp) / "vault")
        cfg.llm.provider = "heuristic"
        cfg.calendar.sink = "ics"
        engine = Engine(cfg)

        ids = [engine.capture(f"Note number {i}", "resource")["note"]["id"]
               for i in range(15)]
        total = len(engine.vault.notes())

        calls = _count_reads(engine.vault)
        note = engine.note(ids[0])
        check("opening one note reads one file",
              calls["n"] == 1, f"read {calls['n']} of {total}")
        check("and it is the right note", note.id == ids[0])

        calls["n"] = 0
        check("a missing id still returns None", engine.vault.find("nope") is None)
        check("looking for a missing id falls back to a scan, once",
              calls["n"] <= total + 1)

        # A file renamed by hand loses the id stamp; correctness must not.
        path, target = engine.vault.get(ids[5])
        path.rename(path.with_name("Renamed By Hand.md"))
        found = engine.vault.find(ids[5])
        check("a hand-renamed note is still found by the fallback",
              found is not None and found[1].id == ids[5])


def _fake_llm(payload):
    """A provider that records how many times it was actually called."""
    calls = {"n": 0}

    class Fake:
        name = "fake"
        is_llm = True

        def complete_json(self, prompt, system="", schema_hint=""):
            calls["n"] += 1
            if isinstance(payload, Exception):
                raise payload
            return payload

    return (lambda cfg, role="": Fake()), calls


def test_connect_tiers_are_free_first():
    """The whole point of the restructure: a note whose connections are
    obvious must not cost a model call."""
    section("connect — cheap tiers come first")
    from sb import connect as C

    guide = Note(id="g1", title="Econ Assignment 1", body="# Econ Assignment 1\n")
    a = Note(id="a1", title="Opportunity Cost",
             body="# Opportunity Cost\n\n*From:* [[Econ Assignment 1]]\n\n## Definition\n\nvalue given up\n")
    b = Note(id="b1", title="Comparative Advantage",
             body="# Comparative Advantage\n\n*From:* [[Econ Assignment 1]]\n\n## Definition\n\nlower opportunity\n")
    far = Note(id="c1", title="Sourdough Starter", body="# Sourdough Starter\n\nflour and water\n")

    links = C.structural_links(a, [guide, b, far])
    titles = [l.title for l in links]
    check("a sibling from the same guide note is found",
          "Comparative Advantage" in titles)
    check("the guide note is not re-proposed — *From:* already links it",
          "Econ Assignment 1" not in titles)
    check("an unrelated note is not", "Sourdough Starter" not in titles)
    check("structural links are marked as such",
          all(l.source == "structural" for l in links))
    check("and carry a real reason",
          all(l.why and "related to" not in l.why for l in links))

    # Backlinks: a quiz pointing at a concept means the concept points back.
    quiz = Note(id="q1", title="Econ Quiz", body="## Key Concepts\n\n- [[Opportunity Cost]]\n")
    back = [l.title for l in C.structural_links(a, [quiz])]
    check("a note that links here is linked back", "Econ Quiz" in back)

    # A note never links itself, even via a self-reference.
    selfish = Note(id="s1", title="Recursion", body="# Recursion\n\nsee [[Recursion]]\n")
    check("a note never links itself",
          C.structural_links(selfish, [selfish]) == [])


def test_title_matching_guards():
    section("connect — literal title matching")
    from sb import connect as C

    tiny = Note(id="t1", title="1", body="x")
    year = Note(id="t2", title="2026", body="x")
    real = Note(id="t3", title="Opportunity Cost", body="x")
    other = Note(id="t4", title="Sunk Cost", body="x")

    subject = Note(
        id="s", title="Notes",
        body="Chapter 1 of the 2026 edition explains opportunity cost and sunk cost.\n",
    )
    links = [l.title for l in C.title_links(subject, [tiny, year, real, other])]

    check("a title that appears verbatim is linked", "Opportunity Cost" in links)
    check("matching is case-insensitive", "Sunk Cost" in links)
    check("a one-character title never matches", "1" not in links)
    check("a numeric title never matches", "2026" not in links)
    check("matches are marked as title matches",
          all(l.source == "title" for l in C.title_links(subject, [real])))

    # Already-linked notes are not re-proposed, and code blocks are not prose.
    linked = Note(id="s2", title="Notes",
                  body="see [[Opportunity Cost]] and also sunk cost\n")
    again = [l.title for l in C.title_links(linked, [real, other])]
    check("an existing link is not proposed again", "Opportunity Cost" not in again)
    check("but a new mention still is", "Sunk Cost" in again)

    fenced = Note(id="s3", title="Notes", body="```\nopportunity cost\n```\n")
    check("a mention inside a code block is ignored",
          C.title_links(fenced, [real]) == [])

    # Longest-first: the more specific title should win the overlap.
    specific = Note(id="t5", title="Opportunity Cost of Capital", body="x")
    overlap = Note(id="s4", title="N", body="the opportunity cost of capital matters\n")
    got = [l.title for l in C.title_links(overlap, [real, specific])]
    check("the longer overlapping title wins",
          got == ["Opportunity Cost of Capital"], str(got))


def test_connect_bands_the_model():
    section("connect — the model sees only the ambiguous band")
    from sb import connect as C

    found = [
        C.Link(title="Very Close", score=0.80),
        C.Link(title="Borderline", score=0.45),
        C.Link(title="Also Borderline", score=0.40),
    ]
    sure, unsure = C.split_by_confidence(found)
    check("confident candidates skip the model",
          [l.title for l in sure] == ["Very Close"])
    check("and get a reason anyway", sure[0].why == "closely related")
    check("only the middle band is left to judge",
          [l.title for l in unsure] == ["Borderline", "Also Borderline"])

    cfg = Config(vault=Path("/tmp/nowhere"))
    note = Note(id="z", title="Thing", body="body text")
    real = C.resolve_provider
    try:
        C.resolve_provider, calls = _fake_llm(
            {"links": [{"title": "Borderline", "why": "the prerequisite for this"}]})
        res = C.judge(note, unsure, cfg)
        check("the model is called once", calls["n"] == 1)
        check("its verdict is honoured", [l.title for l in res.links] == ["Borderline"])
        check("the reason survives", res.links[0].why == "the prerequisite for this")
        check("judged links are marked", res.links[0].source == "judged")
        check("used_model is reported", res.used_model is True)

        # An invented title must never become a link.
        C.resolve_provider, _ = _fake_llm({"links": [{"title": "Made Up", "why": "no"}]})
        check("an invented title is dropped", C.judge(note, unsure, cfg).links == [])

        # A dead model drops the ambiguous band rather than guessing.
        C.resolve_provider, _ = _fake_llm(RuntimeError("model down"))
        dead = C.judge(note, unsure, cfg)
        check("a dead model keeps nothing uncertain", dead.links == [])
        check("and says so", dead.degraded is True)
    finally:
        C.resolve_provider = real


def test_connect_avoids_the_model():
    """End to end: when the free tiers can fill the quota, no call is made."""
    section("connect — free tiers displace the model")
    from sb import connect as C

    cfg = Config(vault=Path("/tmp/nowhere"))
    guide = Note(id="g", title="Econ Assignment 1", body="# Econ Assignment 1\n")
    sibs = [
        Note(id=f"s{i}", title=f"Concept Number {i}",
             body=f"# Concept Number {i}\n\n*From:* [[Econ Assignment 1]]\n")
        for i in range(8)
    ]
    subject = sibs[0]
    others = [guide] + sibs[1:]

    real = C.resolve_provider
    try:
        C.resolve_provider, calls = _fake_llm({"links": []})
        res = C.connect_note(subject, None, cfg, others=others, max_links=4)
        check("the quota is filled", len(res.links) == 4)
        check("entirely from free tiers",
              all(l.source in ("structural", "title") for l in res.links))
        check("with no model call at all", calls["n"] == 0)
        check("and it is reported as such", res.used_model is False)
        check("the section was written", "## Related" in subject.body)
    finally:
        C.resolve_provider = real


def test_connect_engine_pass():
    section("connect — a full pass, and re-running it")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(vault=Path(tmp) / "vault")
        cfg.llm.provider = "heuristic"
        cfg.calendar.sink = "ics"
        engine = Engine(cfg)

        for text in [
            "Opportunity cost is the value of the next best alternative given up.",
            "Sunk cost is a cost already incurred and unrecoverable.",
            "Sourdough starter feeding schedule and hydration ratios.",
        ]:
            engine.capture(text, "resource")

        first = engine.connect_all()
        check("it scans the connectable notes", first["scanned"] >= 3)
        check("it reports model calls", "model_calls" in first)
        check("and which tier produced each link", "by_source" in first)
        check("nothing was skipped on the first run", first["skipped"] == 0)

        # No note may link itself.
        for r in first["results"]:
            check(f"{r['title'][:20]!r} does not link itself",
                  r["title"] not in [l["title"] for l in r["links"]])

        # Re-running an untouched vault must do essentially nothing.
        second = engine.connect_all()
        check("a re-run skips every unchanged note",
              second["skipped"] == second["scanned"], str(second))
        check("and makes no model calls", second["model_calls"] == 0)
        check("and changes nothing", second["changed"] == 0)

        # Editing a note brings just that one back into scope.
        target = engine.notes()[0]
        target.body = target.body.rstrip() + "\n\nA new sentence about economics.\n"
        engine.vault.save(target)
        third = engine.connect_all()
        check("an edited note is reconsidered",
              third["skipped"] == third["scanned"] - 1, str(third))

        # Sections never stack.
        for note in engine.notes():
            if "## Related" in note.body:
                check(f"{note.title[:20]!r} has one Related section",
                      note.body.count("## Related") == 1)


def test_rename_never_clobbers():
    """Renaming on title change must not overwrite another note's file.
    Two notes sharing an id is invalid input, but losing one silently is
    the wrong way to find out."""
    section("renaming never destroys a note")
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(vault=Path(tmp) / "vault")
        cfg.llm.provider = "heuristic"
        cfg.calendar.sink = "ics"
        engine = Engine(cfg)

        a = engine.capture("First note about things", "resource")
        b = engine.capture("Second note about things", "resource")
        bnote = engine.note(b["note"]["id"])
        anote = engine.note(a["note"]["id"])

        # Force a collision: rename b to a's exact filename target.
        bnote.title = anote.title
        engine.vault.save(bnote)

        ids = {n.id for n in engine.notes()}
        check("both notes still exist",
              {anote.id, bnote.id} <= ids, f"{len(ids)} notes")
        check("and both are still findable by id",
              engine.vault.find(anote.id) is not None
              and engine.vault.find(bnote.id) is not None)


def main():
    for fn in [
        test_frontmatter, test_dates, test_steps_and_prior, test_coercion,
        test_planner, test_urgency_and_queue, test_ics, test_taxonomy,
        test_areas_recur, test_google_token_validation, test_google_sync_logic,
        test_gtasks_sync_logic, test_task_shape, test_llm_path,
        test_vault_and_engine, test_api,
        # -- dates
        test_date_confidence, test_deadline_approval, test_concurrent_writes,
        test_resource_reviews, test_habit_checkin,
        # -- the info manager
        test_model_lanes, test_lane_routing, test_model_wire,
        test_index_chunking, test_index_build, test_index_search,
        test_ask, test_ask_api,
        # -- the tutor
        test_fsrs, test_deck_roundtrip, test_card_generation, test_session_mix,
        test_recall_grading, test_progress_and_mastery, test_study_api,
        test_graduation_prompt,
        # -- templates: preservation, materials, link-following
        test_body_preservation, test_materials_kinds,
        test_materials_absorbed, test_link_expansion,
        # -- not scanning the vault, and smart connections
        test_link_resolution_is_cheap, test_capture_reads_once,
        test_connect_sections, test_connect_tiers_are_free_first,
        test_title_matching_guards, test_connect_bands_the_model,
        test_connect_avoids_the_model, test_connect_engine_pass,
        test_filename_repair, test_find_by_id_is_cheap, test_rename_never_clobbers,
    ]:
        try:
            fn()
        except Exception:
            traceback.print_exc()
            FAILED.append(fn.__name__ + " (exception)")

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED:")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
