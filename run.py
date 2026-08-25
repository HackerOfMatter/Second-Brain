#!/usr/bin/env python3
"""Second Brain — entry point.

    python run.py                 start the server (default)
    python run.py init            create the vault structure and templates
    python run.py doctor          check vault, Ollama and calendar wiring
    python run.py capture "..." --bucket project
    python run.py next            print the execution queue
    python run.py sync            regenerate the calendar
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
    print(f"  templates: {len(written)} written to _templates/" if written
          else "  templates: already present")
    counts = engine.vault.counts()
    print("  notes:     " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


def cmd_doctor(args) -> int:
    cfg = load(args.config)
    engine = Engine(cfg)
    h = engine.health()
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
