#!/usr/bin/env python3
"""Second Brain — entry point.

    python run.py                 start the server (default)
    python run.py init            create the vault structure and templates
    python run.py doctor [--write]  check vault, Ollama and calendar wiring
    python run.py capture "..." --bucket project
    python run.py intake          file whatever is in the Drop folder
    python run.py next            print the execution queue
    python run.py sync            regenerate the calendar
    python run.py review          the week: what closed, slipped, is next
    python run.py today [--format text|long]   what's going on today (F3')
    python run.py estimates       how long things really take vs the plan
    python run.py lint            notes holding more than one idea
    python run.py thresholds      are the auto-accept floors set right?
    python run.py fit [--write]   fit FSRS to your own review history
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sb.config import load  # noqa: E402
from sb.engine import Engine  # noqa: E402


def cmd_serve(args) -> int:
    import uvicorn

    from sb.api import build_app

    cfg = load(args.config)
    app = build_app(cfg)
    url = f"http://{cfg.host}:{cfg.port}"
    print(f"Second Brain  ·  vault: {cfg.vault}")
    print(f"                 open: {url}")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    uvicorn.run(
        app,
        host=cfg.host,
        port=cfg.port,
        log_level="warning",
        # A local capture can sit on Ollama for half a minute. The default
        # 5s keep-alive closes idle sockets under that, which the browser
        # reports as a failed fetch.
        timeout_keep_alive=120,
    )
    return 0


def cmd_init(args) -> int:
    from sb.templates import write_templates

    cfg = load(args.config)
    engine = Engine(cfg)
    written = write_templates(engine.vault)
    print(f"Vault ready at {cfg.vault}")
    print("  folders:   00-Inbox, 10-Areas, 20-Projects, 30-Resources, 40-Archive, _system")
    print(f"  drop:      {engine.ensure_drop_folder()}  (put notes you already wrote here)")
    print(f"  templates: {len(written)} written to _templates/" if written
          else "  templates: already present")
    counts = engine.vault.counts()
    print("  notes:     " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


def cmd_templates(args) -> int:
    """Check the templates, and put them back when one is broken.

    Separate from `init` because the two want opposite things. `init` must
    never clobber a template lj has customised, so it only writes what is
    missing. Repair is the case where a template has been *corrupted* rather
    than customised — which has happened once already, to all nine at once —
    and there the whole point is to overwrite.

    So repair is opt-in, it prints what it is about to overwrite, and the
    plain form checks without touching anything.
    """
    from sb.render import check
    from sb.templates import write_templates

    cfg = load(args.config)
    engine = Engine(cfg)
    rows = check(engine.templates)
    broken = [r for r in rows if not r["ok"]]

    for row in rows:
        mark = "OK " if row["ok"] else "!! "
        print(f"{mark}{row['template']:14} {row.get('bucket', '') or '':9}"
              f"{len(row.get('headings') or [])} headings")
        for problem in row["problems"]:
            print(f"                  · {problem}")

    if not args.repair:
        print()
        if broken:
            print(f"{len(broken)} of {len(rows)} templates would not produce a readable note.")
            print("Run `python run.py templates --repair` to write them back.")
            return 1
        print(f"All {len(rows)} templates build a readable note.")
        return 0

    written = write_templates(engine.vault, overwrite=True)
    print()
    print(f"Rewrote {len(written)} files in _templates/.")
    print("Anything you had customised in them is gone — that is what repair means.")
    print("Never put a `#` comment inside the --- block: Obsidian's Properties")
    print("editor swallows it and leaves the rest of the YAML in the note body.")
    return 0


def cmd_doctor(args) -> int:
    """The wiring check — and, since Sprint 3, an audited one.

    Every line here is a claim, and `doctor` is the one output in this system
    that is *trusted without being checked*: nobody re-verifies a line that
    says OK. Sprint 2 caught three lines whose claim outran their evidence in
    a single day, so the rule this function is now held to is one sentence —
    **a marker states what was proved, not what was hoped.**

      * ``OK`` — something was checked and it passed. Never a config echo.
      * ``!!`` — something was checked and it failed, or could not be read.
      * ``  `` (blank) — nothing is wrong and nothing was proved either: a
        counter at zero, a feature that has not been triggered yet, a state
        that is neither good nor bad. This marker is the fix for most of what
        was wrong: "0 decks" and "index not built" were both wearing an OK.

    `--write` saves the same text to `_system/logs/doctor-YYYYMMDD.txt` so the
    weekly review can say when the check last ran (story J2).
    """
    cfg = load(args.config)
    engine = Engine(cfg)
    h = engine.health(progress=True)
    lines: list[str] = []

    def out(text: str = "") -> None:
        lines.append(text)
        print(text)

    ok = "OK "
    bad = "!! "
    meh = "   "  # checked, nothing proved either way

    # -- what is broken right now ------------------------------------------
    # First, because a banner lj has not looked at today is exactly what this
    # command exists to surface. See sb/incidents.py.
    problems = h.get("problems") or []
    if problems:
        out(f"{bad}problems   {len(problems)} open")
        for prob in problems:
            out(f"           · {prob.get('message', '')}")
            out(f"             since {prob.get('since', '')}"
                + (f" · x{prob['count']}" if int(prob.get("count") or 1) > 1 else ""))
            if prob.get("hint"):
                out(f"             {prob['hint']}")
        out()

    # -- vault --------------------------------------------------------------
    # `vault_ok` is exists AND holds at least one readable note: a wrong path
    # that resolves to some empty directory still "exists". Sprint 2's fix,
    # kept and now stated.
    out(f"{ok if h['vault_ok'] else bad}vault      {h['vault']}")
    if h.get("vault_note"):
        out(f"           {h['vault_note']}")
    if not h["vault_ok"]:
        out("           " + ("path does not exist" if not h["vault_exists"]
                             else "the path exists but no note in it could be read"))
    out("           notes: " + ", ".join(f"{k}={v}" for k, v in h["counts"].items()))

    # -- templates ----------------------------------------------------------
    # The shape of every note, and a file lj edits in Obsidian — which is how
    # all nine came to be silently corrupted for three weeks while still
    # looking fine in the sidebar. A template that no longer builds a Note is
    # a hand-written note that will not be readable, so this is a failure and
    # not a warning.
    t = h.get("templates") or {}
    if t.get("error"):
        out(f"{bad}templates  could not be checked: {t['error']}")
    elif not t.get("total"):
        out(f"{meh}templates  none found — `python run.py templates` writes them")
    elif t.get("broken"):
        out(f"{bad}templates  {t['ok']}/{t['total']} usable")
        for row in t["broken"]:
            out(f"           · {row['template']}: {'; '.join(row['problems'])}")
        out("           `python run.py templates --repair` puts them back")
        out("           never put a `#` comment inside the --- block: Obsidian's")
        out("           Properties editor swallows it and truncates the rest")
    else:
        out(f"{ok}templates  {t['ok']}/{t['total']} build a readable note")

    # -- models -------------------------------------------------------------
    llm = h["llm"]
    installed = llm.get("installed_models") or []
    # Reachable is not the same as usable. A server that answers /api/tags
    # while the configured tag is not among them will fail every real call,
    # so the headline marker has to fail with it rather than print OK and
    # bury the problem in a sub-line.
    fast_lane = (llm.get("lanes") or {}).get("fast") or {}
    model_ready = bool(llm["available"]) and (
        # `lane_report` matches tags the way Ollama reports them (`phi4` is
        # pulled as `phi4:latest`), so prefer its answer; `None` there means
        # the tag list came back empty and nothing was proved either way.
        fast_lane["pulled"] if fast_lane.get("pulled") is not None
        else (not installed or llm["model"] in installed)
    )
    out(f"{ok if model_ready else bad}llm        {llm['provider']} · {llm['model']}"
        + ("" if llm["available"] else "  (unreachable)"))
    if llm["available"] and installed:
        out(f"           installed: {', '.join(installed[:8])}")
        if not model_ready:
            out(f"           configured model is not pulled — run: ollama pull {llm['model']}")
    elif not llm["available"]:
        out("           start it with `ollama serve`, or captures use the rule-based parser")
    lanes = llm.get("lanes")
    if lanes:
        study = lanes.get("study")
        if study:
            # `pulled` is None when the tag list came back empty (Ollama down),
            # which is "could not tell", not "missing".
            mark = ok if study["pulled"] else (meh if study["pulled"] is None else bad)
            out(f"{mark}study llm  {study['model']} · flashcards, marking, explain, ask")
            if study.get("warning"):
                out(f"           {study['warning']}")
            out(f"           everything else: {lanes['fast']['model']}")
        else:
            out(f"{meh}study llm  not set — {lanes['fast']['model']} does every job")

    # -- calendar -----------------------------------------------------------
    # This line used to be an unconditional OK over four values read straight
    # back out of config.yaml. Naming a sink is not evidence that anything was
    # ever written to it; the .ics file on disk is.
    cal = h["calendar"]
    writes_ics = cal["sink"] in ("ics", "both") or cal["task_sink"] in ("ics", "both")
    cal_mark = ok if (cal.get("ics_exists") or not writes_ics) else bad
    out(f"{cal_mark}calendar   events={cal['sink']}  tasks={cal['task_sink']}  "
        f"{cal['categories']} colours")
    if writes_ics:
        out(f"           {cal['ics']}"
            + (f"  · written {cal['ics_written']}" if cal.get("ics_written")
               else "  · never written — run `python run.py sync`"))

    # -- drop folder --------------------------------------------------------
    drop = h.get("drop") or {}
    if drop:
        # `intake.candidates()` returns [] for a folder that is not there, so
        # "0 waiting" was printed as OK over a Drop folder lj had deleted.
        # A count of zero is only good news once the thing counted exists.
        if not drop.get("exists"):
            out(f"{bad}drop       folder is missing — nothing dropped there can be filed")
        else:
            waiting = drop["waiting"]
            out(
                f"{ok if drop['watch'] else meh}drop       "
                + (f"{waiting} file(s) waiting to be filed" if waiting else "empty")
                + f" · auto-files above {drop['auto_floor']:.2f}"
                + ("  · watching" if drop["watch"]
                   else "  · watch off — files sit until you press the button")
            )
        out(f"           {drop['path']}")

    # -- index --------------------------------------------------------------
    ix = h.get("index") or {}
    if ix.get("built"):
        if ix.get("semantic"):
            out(f"{ok}index      {ix['chunks']} passages from {ix['notes']} notes · semantic")
        elif ix.get("dim"):
            # The vector file did not line up with the chunk file, so `load()`
            # dropped it. That is a broken index falling back, not a vault
            # that never had embeddings — and they are not the same news.
            out(f"{bad}index      {ix['chunks']} passages · embeddings were built but "
                "discarded as inconsistent — `python run.py` then rebuild the index")
        else:
            out(f"{meh}index      {ix['chunks']} passages from {ix['notes']} notes · "
                "keyword only — no embeddings")
    else:
        out(f"{meh}index      not built — ask a question and it builds itself")

    # -- tutor --------------------------------------------------------------
    # Zero decks is not a passing check, and `DeckStore.all()` skips a deck
    # file it cannot parse — so a corrupted deck used to vanish from this
    # count without a word.
    st = h.get("study") or {}
    unreadable = int(st.get("unreadable") or 0)
    decks = int(st.get("decks") or 0)
    out(f"{bad if unreadable else (ok if decks else meh)}tutor      "
        f"{decks} deck(s)  "
        f"retention target {int(float(st.get('retention', 0.9)) * 100)}%  "
        f"{st.get('path', '')}")
    if unreadable:
        out(f"           {unreadable} deck file(s) could not be read and are not counted")
    queued = int(h.get("queued_cards") or 0)
    if queued:
        out(f"{meh}           {queued} note(s) queued for card generation, "
            "waiting on a model")

    # Everything that measures itself, and how far off it still is. A feature
    # that unlocks on a trigger looks broken rather than pending unless the
    # distance is stated.
    pr = h.get("progress") or {}
    if pr:
        out()
        fsrs_p = pr["fsrs"]
        if fsrs_p["using_fitted"]:
            out(f"{ok}fsrs       using weights fitted from your own {fsrs_p['have']} reviews")
        elif fsrs_p["have"] >= fsrs_p["need"]:
            out(f"{ok}fsrs       {fsrs_p['have']} reviews — ready to fit: `python run.py fit --write`")
        else:
            out(f"{meh}fsrs       {fsrs_p['have']}/{fsrs_p['need']} reviews before personal weights mean anything")

        cal_p = pr["calibration"]
        out(f"{meh}calibration {cal_p['have']}/{cal_p['need']} predictions"
            + ("" if cal_p["have"] >= cal_p["need"] else " — tap j/k/l before revealing an answer"))

        th = pr["thresholds"]
        out(f"{meh}thresholds  intake {th['intake']}/{th['need']} · "
            f"connect {th['connect']}/{th['need']} labelled examples")

        es = pr["estimates"]
        line = f"{meh}estimates   {es['have']}/{es['need']} timed steps"
        if es["untimed"]:
            line += f" · {es['untimed']} finished without a clock"
        out(line)

        hb = pr["habits"]
        if hb["areas"]:
            if hb["without_plan"]:
                out(f"{bad}habits     {hb['without_plan']} of {hb['areas']} areas have no "
                    "\u201cwhen X, I will Y at Z\u201d — the biggest lever there is")
            else:
                out(f"{ok}habits     all {hb['areas']} areas carry an implementation intention")

        smells = pr["atomicity"]
        if smells:
            out(f"{meh}notes       " + ", ".join(f"{v} {k}" for k, v in sorted(smells.items()))
                + "  (`python run.py lint`)")

        np_p = pr["numpy"]
        if np_p["accelerated"]:
            out(f"{ok}index math  numpy ({np_p['chunks']} chunks)")
        elif np_p["chunks"] and np_p["at"] and np_p["chunks"] >= np_p["at"]:
            out(f"{bad}index math  {np_p['chunks']} chunks and numpy is not installed — "
                "`pip install numpy` would speed searches up")
        else:
            out(f"{meh}index math  pure python ({np_p['chunks']} chunks; "
                f"numpy engages at {np_p['at']})")

        # Three states, not two. "I could not read Obsidian's plugin list" was
        # being printed as "the plugin is not enabled", which sent lj to turn
        # on a plugin that was already on. See Engine._obsidian_plugin_status.
        if not pr["plugin"]:
            out(f"{meh}obsidian   capture plugin not present")
        elif not pr.get("plugin_enabled_known", True):
            out(f"{meh}obsidian   capture plugin installed; could not read "
                ".obsidian/community-plugins.json, so whether it is enabled is unknown")
        elif pr.get("plugin_enabled"):
            out(f"{ok}obsidian   capture plugin enabled")
        else:
            out(f"{bad}obsidian   capture plugin installed but not enabled "
                "-- turn it on in Settings -> Community plugins")
        out()

    # -- google -------------------------------------------------------------
    g = h["calendar"].get("google")
    if g:
        # File inspection only — `_google_auth.status()` never touches the
        # network, so this line cannot claim Google accepts the token. It
        # claims exactly what it checked.
        mark = ok if g["ready"] else bad
        state = ("token on disk looks usable (not checked against Google)"
                 if g["ready"] else (g["reason"] or "not authorised"))
        out(f"{mark}google     {state}")
        if not g["ready"]:
            out("           run `python run.py sync` — a browser will open once")

    if getattr(args, "write", False):
        path = engine.write_doctor_report("\n".join(lines))
        print()
        print(f"written to {path}")
        print(f"keeping the last {Engine.DOCTOR_REPORTS_KEPT} reports")
    return 0


def _capture_split(engine, args) -> int:
    """`--split`: show what the capture becomes, then file it unless it is a
    dry run. The preview is printed either way, because a splitter you cannot
    see the output of is a splitter you stop trusting."""
    plan = engine.capture_plan(args.text, args.bucket)
    kind = "project" if plan["mode"] == "tasks" else "note"
    print(f"{len(plan['items'])} {kind}(s), split on {plan['strategy']} "
          f"({plan['words']} words in)")
    if plan.get("lossless") is False:
        print("  !! some lines landed in no note:")
        for line in plan.get("lost_lines", []):
            print(f"     {line}")
    for item in plan["items"]:
        due = f"  due {item['due']}" if item["due"] else ""
        print(f"  {item['ordinal']:>2}. {item['title']}{due}   ({item['words']} words)")
    if plan.get("note"):
        print(f"  {plan['note']}")
    if args.dry_run:
        print("\ndry run — nothing was written")
        return 0
    out = engine.capture_commit(plan["items"])
    print(f"\n{out['note']}")
    for row in out["failed"]:
        print(f"  !! {row['title']}  {row['error']}")
    return 1 if out["failed"] and not out["created"] else 0


def cmd_capture(args) -> int:
    cfg = load(args.config)
    engine = Engine(cfg)
    if getattr(args, "split", False) or getattr(args, "dry_run", False):
        return _capture_split(engine, args)
    result = engine.capture(args.text, args.bucket)
    note = result["note"]
    print(f"{note['bucket']}: {note['title']}")
    print(f"  {result['path']}")
    if note.get("project"):
        p = note["project"]
        print(f"  deadline={p.get('deadline')} level={p['level']} est={p['estimate_minutes']}m")
        for s in p["steps"]:
            when = f"  @ {s['scheduled'][:16].replace('T', ' ')}" if s.get("scheduled") else ""
            print(f"    - {s['text']} ({s['minutes']}m){when}")
    if result.get("parser", {}).get("degraded"):
        print(f"  ! {result['parser']['note']}")
    return 0


def cmd_cards(args) -> int:
    """Draft cards for every note under a folder.

    No time budget here, unlike the web route — this is the run you leave
    going. `--dry-run` first is the habit worth having: it names every note it
    would touch and every note it would skip, and skipping is where the
    surprises are.
    """
    cfg = load(args.config)
    engine = Engine(cfg)
    r = engine.generate_folder(
        args.folder,
        limit=args.limit,
        max_cards=args.max_cards,
        include_existing=args.include_existing,
        dry_run=args.dry_run,
        budget_seconds=float(args.budget),
    )
    scope = r["folder"] or "the whole vault"
    print(f"{scope}: {r['candidates']} note(s) need cards, {len(r['skipped'])} skipped")
    for row in r["notes"]:
        if args.dry_run:
            print(f"  · {row['title']}  ({row['words']} words)  {row['folder']}")
        elif row.get("error"):
            print(f"  !! {row['title']}  {row['error']}")
        else:
            extra = f", {row['rejected']} dropped" if row.get("rejected") else ""
            print(f"  + {row['cards']:>3} cards  {row['title']}{extra}")
    if args.verbose:
        for row in r["skipped"]:
            print(f"  -  skip  {row['title']}  ({row['reason']})")
    print(f"\n{r['note']}")
    if r.get("stopped_early"):
        return 0
    return 0


def cmd_intake(args) -> int:
    cfg = load(args.config)
    engine = Engine(cfg)
    r = engine.intake(dry_run=args.dry_run, limit=args.limit)
    print(f"Drop folder: {r['folder']}")
    if not r["scanned"]:
        print("  nothing to file"
              + (f"  ({r['waiting']} still settling)" if r.get("waiting") else ""))
        return 0
    for item in r["results"]:
        if item["status"] == "unreadable":
            print(f"  !! {item['file']}  {item['error']}")
            continue
        sure = item["status"] in ("filed", "would file")
        where = item.get("bucket") or item.get("suggested")
        print(f"  {'->' if sure else '??'} {item['file']}  {where}"
              f"  ({item['confidence']:.2f} {item['decided_by']})")
        print(f"       {item['reason']}")
    print(f"  {r['filed']} filed · {r['asking']} waiting in the Inbox · "
          f"{r['unreadable']} unreadable · {r['model_calls']} model call(s)")
    if r["asking"]:
        print("  confirm those on the dashboard — each carries its suggestion")
    return 0


def cmd_next(args) -> int:
    cfg = load(args.config)
    d = Engine(cfg).dashboard()
    if not d["next_actions"]:
        print("Queue is empty.")
        return 0
    for a in d["next_actions"]:
        due = f"  (due {a['deadline']})" if a["deadline"] else ""
        print(f"[{int(a['urgency'] * 100):3d}] {a['step']['text']}")
        print(f"      {a['note_title']}{due}")
    return 0


def cmd_today(args) -> int:
    """F3' spike: "what's going on today" — for SMS/push, or read at the
    terminal. Same payload as GET /api/today."""
    from sb.digest import render_long, render_text

    cfg = load(args.config)
    d = Engine(cfg).today_digest()
    print(render_text(d) if args.format == "text" else render_long(d))
    return 0


def cmd_sync(args) -> int:
    """Push the vault out, after reading ticks back in.

    The read-back gets its own line rather than being left inside the result
    dict. It is the half of the round trip nobody can see from the outside —
    a push that failed shows up as a calendar that stopped changing, while a
    read that failed looks exactly like "lj did not tick anything" — so the
    hand-verification runbook in docs/google-round-trip.md keys on this line
    at every step.
    """
    cfg = load(args.config)
    result = Engine(cfg).sync_calendar()
    pulled = result.get("pulled") or {}
    if pulled.get("error"):
        print(f"read-back FAILED ({pulled['error']}) — ticks made on the phone "
              f"did not reach the vault; see _system/logs/")
    elif pulled.get("enabled"):
        applied = pulled.get("applied", 0)
        tail = (": " + ", ".join(pulled.get("notes") or [])) if applied else ""
        print(f"read-back: {pulled.get('checked', 0)} ticked task(s) on Google, "
              f"{applied} applied to the vault{tail}")
    elif pulled.get("reason") == "off":
        print("read-back: off (calendar.read_back_completions: false)")
    else:
        print("read-back: not applicable (due-date tasks are not going to Google)")
    print(result)
    return 0


def cmd_study_reminder(args) -> int:
    """What the desktop reminder would do right now, and why (story H3).

    Purely a read: it never writes `fired_at`, so running this to check
    cannot rob lj of tonight's notification.
    """
    cfg = load(args.config)
    d = Engine(cfg).study_reminder()
    verdict = "WOULD FIRE" if d["decision"]["fire"] else "quiet"
    print(f"{verdict} — {d['decision']['reason']}")
    print(f"  {d['cards_due']} card(s) due across {d['decks']} deck(s); "
          f"{d['reviewed_today']} answered today; block starts {d['starts_at']}")
    print(f"  last fired: {d['state']['fired_at'] or 'never'}    "
          f"snoozed until: {d['state']['snoozed_until'] or '—'}")
    return 0


def cmd_review(args) -> int:
    """The weekly review, in the terminal. Same payload as /review."""
    cfg = load(args.config)
    d = Engine(cfg).weekly_review(args.days)
    c, sl, n = d["closed"], d["slipped"], d["next"]
    print(f"Week of {d['since']} — {d['days']} days\n")
    print(f"CLOSED   {len(c['steps'])} steps · {c['reviews']} reviews · "
          f"{c['minutes_studied']} min studying · {len(c['projects'])} projects finished")
    for step in c["steps"][:8]:
        took = f"{step['actual']}m" if step["actual"] else "untimed"
        print(f"    {step['step']}  ({step['estimated']}m planned, {took})")
    print(f"\nSLIPPED  {len(sl['projects'])} overdue · {len(sl['steps'])} steps left behind")
    for project in sl["projects"][:8]:
        print(f"    {project['title']}  {project['days']}d late")
    for habit in sl["habits"][:8]:
        print(f"    {habit['title']}: {habit['message']}")
    print(f"\nNEXT     {len(n['actions'])} queued · {len(n['habit_checkins'])} check-ins · "
          f"{len(n['graduation'])} ready to graduate")
    for action in n["actions"][:8]:
        print(f"    {action['step']['text']}   ({action['note_title']})")
    print(f"\n{d['estimates']['message']}")
    print(d["calibration"]["message"])
    print(d["atomicity"]["message"])
    # The health check is part of the week too — a `doctor` that was supposed
    # to run weekly and has not run in a month is its own finding (story J2).
    doc = d.get("doctor") or {}
    if doc.get("message"):
        print(("!! " if doc.get("stale") else "   ") + doc["message"])
    for prob in d.get("problems") or []:
        print(f"!! {prob.get('message', '')}  (since {prob.get('since', '')})")
    return 0


def cmd_estimates(args) -> int:
    cfg = load(args.config)
    d = Engine(cfg).estimates()
    print(d["message"])
    for cls in d["classes"]:
        if cls["n"]:
            print(f"  {cls['name']:>10}  n={cls['n']:<4} median {cls['median_ratio']}×"
                  f"  {'applied' if cls['enough'] else '(not enough yet)'}")
    for worst in d["worst"]:
        print(f"    {worst['ratio']}×  {worst['step']}  ({worst['estimated']}m -> {worst['actual']}m)")
    return 0


def cmd_lint(args) -> int:
    cfg = load(args.config)
    d = Engine(cfg).atomicity()
    print(d["message"])
    for finding in d["findings"]:
        print(f"  [{finding['rule']}] {finding['title']}\n      {finding['detail']}")
    return 0 if d["clean"] else 1


def cmd_thresholds(args) -> int:
    cfg = load(args.config)
    for name, sweep in Engine(cfg).thresholds().items():
        print(f"{name}: {sweep['message']}")
        if sweep["usable"]:
            for point in sweep["points"]:
                if point["accepted"]:
                    print(f"    {point['cutoff']:.2f}  precision {point['precision']}"
                          f"  recall {point['recall']}  asks {point['asks']}")
    return 0


def cmd_fit(args) -> int:
    """Tier 4. The ~1,000-review trigger is enforced, not suggested."""
    cfg = load(args.config)
    d = Engine(cfg).fit_weights(write=args.write)
    print(d["message"])
    if d.get("written"):
        print(f"written to {d['written']}")
    elif d.get("refused"):
        print(f"not written: {d['refused']}")
    if d["improved"]:
        print("weights: " + ", ".join(str(w) for w in d["weights"]))
    return 0


def cmd_reteval(args) -> int:
    """Score retrieval against a checked-in question set (sb/reteval.py).

    Read-only: it builds nothing and writes nothing to the vault, so it is
    safe to run against the live index at any moment, including while the
    server is up.
    """
    from sb import reteval

    cfg = load(args.config)
    engine = Engine(cfg)
    if not engine.index.exists():
        print("no index yet — run `python run.py serve` once, or ask a question.")
        return 1
    qset = reteval.load_set(args.set)
    result = reteval.run(engine.index, qset, depth=args.depth)
    print(reteval.format_report(result))
    if args.json:
        Path(args.json).write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nwritten to {args.json}")
    return 0


def cmd_adopt(args) -> int:
    """Adopt a folder of notes lj already wrote (see sb/adopt.py).

    Dry by default. `adopt <folder>` reports and writes nothing; only
    `--write` touches the vault. The pile this exists for is lj's only copy
    of 674 notes, and a command that adopts on its first invocation is one
    keystroke away from doing it to the wrong folder.
    """
    from sb import adopt as adoptmod
    from sb.frontmatter import dump

    cfg = load(args.config)
    engine = Engine(cfg)
    vault = engine.vault

    if args.undo:
        manifest = adoptmod.load_manifest(vault, args.undo)
        result = adoptmod.undo(vault, manifest, dry_run=not args.write)
        if result["refused"]:
            print("refused: the originals are not where the manifest left them")
            for row in result["refused"]["missing"]:
                print(f"  missing  {row}")
            for row in result["refused"]["changed"]:
                print(f"  changed  {row}")
            return 1
        verb = "would remove" if result["dry_run"] else "removed"
        print(f"undo {manifest['run']}: {verb} {len(result['removed'])} file(s)")
        for row in result["kept"]:
            print(f"  kept  {row['dest']}  ({row['why']})")
        if result["dry_run"]:
            print("  nothing removed — re-run with --write")
        return 0

    plan = adoptmod.plan(vault, Path(args.folder), limit=args.limit)
    print(f"{plan.source_rel}  ->  {plan.dest_rel}")
    print(f"  {len(plan.decisions)} markdown file(s) considered"
          + ("  (--limit applied)" if plan.limited else ""))

    for d in plan.adopting:
        flag = "  ~provisional created" if d.provisional else ""
        print(f"  + {d.dest_rel}")
        print(f"      from {d.rel}  ({d.words}w, created from {d.created_from}{flag})")
        if d.dropped and args.verbose:
            for key, why in d.dropped.items():
                print(f"      - dropped {key}: {why}")

    for reason, n in plan.skips_by_reason().items():
        print(f"  ?? {n:3d} skipped — {reason}")
        if args.verbose:
            for d in plan.skipping:
                if d.reason == reason:
                    print(f"        {d.rel}")

    if plan.assets:
        print(f"  ++ {len(plan.assets)} linked attachment(s) carried across")
    if plan.missing_assets:
        print(f"  !! {len(plan.missing_assets)} linked attachment(s) not in this folder"
              " — those links were already broken")
    if plan.unreferenced_assets:
        print(f"  .. {len(plan.unreferenced_assets)} attachment(s) nothing links to, left behind")

    if plan.adopting:
        sample = plan.adopting[0]
        note = adoptmod.build_note(sample, "dry-run", cfg.review.resource_cycle_days)
        print(f"\n  the frontmatter this writes, for {sample.rel}:")
        for line in dump(note.frontmatter(), "").splitlines():
            print(f"      {line}")

    print(f"\n  {len(plan.adopting)} would be adopted · {len(plan.skipping)} skipped")
    if not args.write:
        print("  nothing written — re-run with --write")
        return 0

    result = adoptmod.apply(vault, plan, cfg.review.resource_cycle_days)
    print(f"  adopted {result['adopted']} · assets {result['assets']}"
          + (f" · manifest {result['manifest']}" if result["manifest"]
             else " · nothing to record"))
    intact = result["originals_intact"]
    print(f"  originals: {intact['checked']} re-hashed, "
          + ("all intact" if intact["ok"] else f"MISSING {intact['missing']} CHANGED {intact['changed']}"))
    for row in result["failures"]:
        print(f"  !! {row['source']}  {row['error']}")
    return 0 if intact["ok"] and not result["failures"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="second-brain", description=__doc__)
    ap.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="run the local web app (default)")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_serve)

    sub.add_parser("init", help="create vault folders and templates").set_defaults(func=cmd_init)
    t = sub.add_parser("templates", help="check the note templates, or put them back")
    t.add_argument("--repair", action="store_true",
                   help="overwrite every template with the built-in copy")
    t.set_defaults(func=cmd_templates)
    d = sub.add_parser("doctor", help="check the wiring")
    d.add_argument("--write", action="store_true",
                   help="also save the report to _system/logs/doctor-YYYYMMDD.txt")
    d.set_defaults(func=cmd_doctor)
    sub.add_parser("next", help="print the execution queue").set_defaults(func=cmd_next)

    t = sub.add_parser("today", help="what is going on today (F3')")
    t.add_argument("--format", choices=["long", "text"], default="long")
    t.set_defaults(func=cmd_today)
    sub.add_parser("sync", help="regenerate the calendar").set_defaults(func=cmd_sync)
    sub.add_parser(
        "study-reminder", help="would the study reminder fire right now, and why"
    ).set_defaults(func=cmd_study_reminder)
    sub.add_parser("estimates", help="estimated vs actual, and your multiplier").set_defaults(func=cmd_estimates)
    sub.add_parser("lint", help="notes holding more than one idea").set_defaults(func=cmd_lint)
    sub.add_parser("thresholds", help="are the auto-accept floors set right?").set_defaults(func=cmd_thresholds)

    r = sub.add_parser("review", help="the week: what closed, what slipped, what is next")
    r.add_argument("--days", type=int, default=7)
    r.set_defaults(func=cmd_review)

    f = sub.add_parser("fit", help="fit FSRS weights to your own review history")
    f.add_argument("--write", action="store_true", help="save them (needs ~1,000 reviews)")
    f.set_defaults(func=cmd_fit)

    i = sub.add_parser("intake", help="file whatever is sitting in the Drop folder")
    i.add_argument("--dry-run", action="store_true",
                   help="say where each file would go without moving anything")
    i.add_argument("--limit", type=int, default=None)
    i.set_defaults(func=cmd_intake)

    e = sub.add_parser("eval-retrieval", help="score search against a fixed question set")
    e.add_argument("--set", default="accounting_retrieval",
                   help="evaluation set name (sb/evalsets/) or path to a .json")
    e.add_argument("--depth", type=int, default=10, help="how deep to look for a hit")
    e.add_argument("--json", default=None, help="also write the full result here")
    e.set_defaults(func=cmd_reteval)

    a = sub.add_parser("adopt", help="adopt a folder of notes into 30-Resources")
    a.add_argument("folder", nargs="?", help="folder to adopt, relative to the vault root")
    a.add_argument("--write", action="store_true",
                   help="actually write; without it this only reports")
    a.add_argument("--dry-run", action="store_true",
                   help="the default: report and write nothing")
    a.add_argument("--limit", type=int, default=None, help="only the first N files")
    a.add_argument("--verbose", "-v", action="store_true",
                   help="name every skipped file and every dropped key")
    a.add_argument("--undo", metavar="RUN", nargs="?", const="latest",
                   help="undo an adoption run (default: the latest)")
    a.set_defaults(func=cmd_adopt)

    c = sub.add_parser("capture", help="capture from the command line")
    c.add_argument("text")
    c.add_argument("--bucket", default="project", choices=["inbox", "area", "project", "resource"])
    c.add_argument("--split", action="store_true",
                   help="one prompt into several Projects, or long text into "
                        "several notes")
    c.add_argument("--dry-run", action="store_true",
                   help="show the split and write nothing (implies --split)")
    c.set_defaults(func=cmd_capture)

    g = sub.add_parser("cards", help="draft cards for every note under a folder")
    g.add_argument("folder", nargs="?", default="",
                   help="vault-relative, e.g. 30-Resources/Accounting (default: everything)")
    g.add_argument("--limit", type=int, default=None, help="only the first N notes")
    g.add_argument("--max-cards", type=int, default=None, help="cap per note")
    g.add_argument("--include-existing", action="store_true",
                   help="also add to notes that already have cards")
    g.add_argument("--dry-run", action="store_true", help="say what it would do")
    g.add_argument("--budget", type=int, default=100000,
                   help="seconds before it stops on a note boundary")
    g.add_argument("--verbose", "-v", action="store_true", help="name every skipped note")
    g.set_defaults(func=cmd_cards)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        args.func = cmd_serve
        args.no_browser = False
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
