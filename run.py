#!/usr/bin/env python3
"""Second Brain — entry point.

    python run.py                 start the server (default)
    python run.py init            create the vault structure and templates
    python run.py doctor          check vault, Ollama and calendar wiring
    python run.py capture "..." --bucket project
    python run.py intake          file whatever is in the Drop folder
    python run.py next            print the execution queue
    python run.py sync            regenerate the calendar
    python run.py review          the week: what closed, slipped, is next
    python run.py estimates       how long things really take vs the plan
    python run.py lint            notes holding more than one idea
    python run.py thresholds      are the auto-accept floors set right?
    python run.py fit [--write]   fit FSRS to your own review history
"""

from __future__ import annotations

import argparse
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


def cmd_doctor(args) -> int:
    cfg = load(args.config)
    engine = Engine(cfg)
    h = engine.health(progress=True)
    ok = "OK "
    bad = "!! "
    print(f"{ok if h['vault_exists'] else bad}vault      {h['vault']}")
    print(f"           notes: " + ", ".join(f"{k}={v}" for k, v in h["counts"].items()))
    llm = h["llm"]
    print(f"{ok if llm['available'] else bad}llm        {llm['provider']} · {llm['model']}")
    if llm["available"] and llm["installed_models"]:
        print(f"           installed: {', '.join(llm['installed_models'][:8])}")
        if llm["model"] not in llm["installed_models"]:
            print(f"{bad}           configured model not pulled — run: ollama pull {llm['model']}")
    elif not llm["available"]:
        print("           start it with `ollama serve`, or captures use the rule-based parser")
    lanes = llm.get("lanes")
    if lanes:
        study = lanes.get("study")
        if study:
            mark = ok if study["pulled"] else bad
            print(f"{mark}study llm  {study['model']} · flashcards, marking, explain, ask")
            if study.get("warning"):
                print(f"           {study['warning']}")
            print(f"           everything else: {lanes['fast']['model']}")
        else:
            print(f"{ok}study llm  not set — {lanes['fast']['model']} does every job")
    print(
        f"{ok}calendar   events={h['calendar']['sink']}  "
        f"tasks={h['calendar']['task_sink']}  "
        f"{h['calendar']['categories']} colours  {h['calendar']['ics']}"
    )
    drop = h.get("drop") or {}
    if drop:
        waiting = drop["waiting"]
        print(
            f"{ok}drop       "
            + (f"{waiting} file(s) waiting to be filed" if waiting else "empty")
            + f" · auto-files above {drop['auto_floor']:.2f}"
            + ("  · watching" if drop["watch"] else "  · watch off")
        )
        print(f"           {drop['path']}")
    ix = h.get("index") or {}
    if ix.get("built"):
        mode = "semantic" if ix.get("semantic") else "keyword only — no embeddings"
        print(f"{ok}index      {ix['chunks']} passages from {ix['notes']} notes · {mode}")
    else:
        print(f"{ok}index      not built — ask a question and it builds itself")
    st = h.get("study") or {}
    print(
        f"{ok}tutor      {st.get('decks', 0)} deck(s)  "
        f"retention target {int(float(st.get('retention', 0.9)) * 100)}%  "
        f"{st.get('path', '')}"
    )
    # Everything that measures itself, and how far off it still is. A feature
    # that unlocks on a trigger looks broken rather than pending unless the
    # distance is stated.
    pr = h.get("progress") or {}
    if pr:
        print()
        fsrs_p = pr["fsrs"]
        if fsrs_p["using_fitted"]:
            print(f"{ok}fsrs       using weights fitted from your own {fsrs_p['have']} reviews")
        elif fsrs_p["have"] >= fsrs_p["need"]:
            print(f"{ok}fsrs       {fsrs_p['have']} reviews — ready to fit: `python run.py fit --write`")
        else:
            print(f"   fsrs       {fsrs_p['have']}/{fsrs_p['need']} reviews before personal weights mean anything")

        cal = pr["calibration"]
        print(f"   calibration {cal['have']}/{cal['need']} predictions"
              + ("" if cal["have"] >= cal["need"] else " — tap j/k/l before revealing an answer"))

        th = pr["thresholds"]
        print(f"   thresholds  intake {th['intake']}/{th['need']} · "
              f"connect {th['connect']}/{th['need']} labelled examples")

        es = pr["estimates"]
        line = f"   estimates   {es['have']}/{es['need']} timed steps"
        if es["untimed"]:
            line += f" · {es['untimed']} finished without a clock"
        print(line)

        hb = pr["habits"]
        if hb["areas"]:
            if hb["without_plan"]:
                print(f"{bad}habits     {hb['without_plan']} of {hb['areas']} areas have no "
                      "\u201cwhen X, I will Y at Z\u201d — the biggest lever there is")
            else:
                print(f"{ok}habits     all {hb['areas']} areas carry an implementation intention")

        smells = pr["atomicity"]
        if smells:
            print("   notes       " + ", ".join(f"{v} {k}" for k, v in sorted(smells.items()))
                  + "  (`python run.py lint`)")

        np_p = pr["numpy"]
        if np_p["accelerated"]:
            print(f"{ok}index math  numpy ({np_p['chunks']} chunks)")
        elif np_p["chunks"] and np_p["at"] and np_p["chunks"] >= np_p["at"]:
            print(f"{bad}index math  {np_p['chunks']} chunks and numpy is not installed — "
                  "`pip install numpy` would speed searches up")
        else:
            print(f"   index math  pure python ({np_p['chunks']} chunks; "
                  f"numpy engages at {np_p['at']})")

        print(("OK " if pr["plugin"] else "   ")
              + "obsidian   capture plugin "
              + ("installed — enable it in Settings \u2192 Community plugins"
                 if pr["plugin"] else "not present"))
        print()

    g = h["calendar"].get("google")
    if g:
        mark = "OK " if g["ready"] else "!! "
        state = "authorised" if g["ready"] else (g["reason"] or "not authorised")
        print(f"{mark}google     {state}")
        if not g["ready"]:
            print("           run `python run.py sync` — a browser will open once")
    return 0


def cmd_capture(args) -> int:
    cfg = load(args.config)
    engine = Engine(cfg)
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


def cmd_sync(args) -> int:
    cfg = load(args.config)
    print(Engine(cfg).sync_calendar())
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


def main() -> int:
    ap = argparse.ArgumentParser(prog="second-brain", description=__doc__)
    ap.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="run the local web app (default)")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_serve)

    sub.add_parser("init", help="create vault folders and templates").set_defaults(func=cmd_init)
    sub.add_parser("doctor", help="check the wiring").set_defaults(func=cmd_doctor)
    sub.add_parser("next", help="print the execution queue").set_defaults(func=cmd_next)
    sub.add_parser("sync", help="regenerate the calendar").set_defaults(func=cmd_sync)
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

    c = sub.add_parser("capture", help="capture from the command line")
    c.add_argument("text")
    c.add_argument("--bucket", default="project", choices=["inbox", "area", "project", "resource"])
    c.set_defaults(func=cmd_capture)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        args.func = cmd_serve
        args.no_browser = False
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
