# Scrum Backlog — Public Release ("secondbrain" → product)

Written 2026-08-29. Successor to `claude/roadmap_phase7_plus.md`, which is a
*personal* roadmap. This backlog covers the work to take a single-user,
single-machine, local-vault system to a product other people can sign up for
and trust.

**Product vision** — Anyone gets a personal assistant that captures what they
want to learn or do, files it, schedules it, quizzes them until they know it,
and holds them to their habits — on their own data, without them having to
build it.

**Release goal (R1, Private Beta)** — 50 external users complete signup →
capture → first study session → 7-day habit streak without a developer touching
their environment.

**Legend** — Pts = story points (Fibonacci, 1 ≈ half a day). MoSCoW: M=Must,
S=Should, C=Could, W=Won't (R1).

---

## 0. The gap between "works for lj" and "works for everyone"

Named plainly, because every epic below descends from one of these:

| # | Assumption baked into the current build | What it must become |
|---|---|---|
| G1 | One user, one machine, no auth | Accounts, sessions, per-user isolation |
| G2 | Local Obsidian vault is the source of truth | Storage is an interface; local vault is *one* backend |
| G3 | Flask/local server, run by hand (`run.py`, `doctor.bat`) | Deployed service, or a signed installer, or both |
| G4 | Files on disk, no concurrency control | Two devices editing one account without data loss |
| G5 | LLM calls are free/local, unmetered | Per-user cost ceiling, BYO key, or paid tiers |
| G6 | No privacy surface — data never leaves the machine | DPA, deletion, export, LLM data handling disclosed |
| G7 | Failures are read by the developer in a terminal | Errors surface to users; telemetry surfaces to the team |
| G8 | Config = editing Python constants | Settings UI with safe defaults |
| G9 | Onboarding = reading a build log | Empty state that teaches in under 10 minutes |
| G10 | Tests run on one Windows box, ad hoc | CI on every push, on every supported OS |

---

## 1. Epics

| ID | Epic | R1? | Rough pts |
|---|---|---|---|
| E1 | Engineering foundations (VCS, CI, packaging, release) | M | 34 |
| E2 | Storage abstraction & sync | M | 55 |
| E3 | Identity, accounts, multi-tenancy | M | 34 |
| E4 | Onboarding & first-run experience | M | 34 |
| E5 | Capture & classification, productized | M | 21 |
| E6 | Learning engine quality (Tier 2 science) | M | 55 |
| E7 | Habits & Areas rewrite | S | 21 |
| E8 | Projects, scheduling & calendar/tasks sync | S | 34 |
| E9 | Tutor / RAG at multi-user scale & cost | M | 34 |
| E10 | LLM routing, keys, quotas & cost control | M | 21 |
| E11 | Privacy, security & compliance | M | 34 |
| E12 | Billing & plans | S | 21 |
| E13 | Observability, support & on-call | M | 21 |
| E14 | Cross-platform surfaces (mobile capture, PWA) | C | 34 |
| E15 | Import & migration (Obsidian, Anki, Notion, CSV) | S | 21 |
| E16 | Accessibility, i18n & content polish | S | 21 |
| E17 | Docs, marketing site & community | S | 21 |
| E18 | Beta program & feedback loop | M | 13 |

---

## 2. Product backlog

### E1 — Engineering foundations

| ID | Story | Pts | M |
|---|---|---|---|
| E1-1 | As a maintainer, the vault repo has a real commit history so a collision is a diff, not a discovery. **AC:** working tree committed; `.gitignore` excludes `_decks/`, `_system/`, `.venv`, tarballs; branch protection on `main`. | 3 | M |
| E1-2 | As a maintainer, every push runs the suite on Linux **and** Windows so a Windows-only path/CRLF/`os.replace` bug can't ship. **AC:** GitHub Actions matrix (win/linux × py3.10–3.13); 1,077 assertions green; badge in README. | 5 | M |
| E1-3 | As a maintainer, the app is an installable package, not a folder of scripts. **AC:** `pyproject.toml`, pinned deps, `pip install .` gives a `secondbrain` entrypoint; reproducible lockfile. | 5 | M |
| E1-4 | As a maintainer, releases are versioned and changelogged. **AC:** SemVer tags, `CHANGELOG.md`, release workflow builds artifacts. | 3 | M |
| E1-5 | As a maintainer, the code has typing + lint gates. **AC:** `ruff` + `mypy` in CI, zero errors on `sb/`. | 5 | S |
| E1-6 | As a maintainer, `doctor` runs on a user's machine and produces a redacted support bundle. **AC:** `secondbrain doctor --bundle` writes a zip with no note content. | 5 | S |
| E1-7 | As a maintainer, migrations are versioned. **AC:** schema/frontmatter version stamped on every note; forward migration runner + test on a v1 fixture vault. | 8 | M |

### E2 — Storage abstraction & sync

| ID | Story | Pts | M |
|---|---|---|---|
| E2-1 | As an architect, storage sits behind one interface so the vault is a backend, not the design. **AC:** `Store` protocol (list/read/write/move/delete/watch); `LocalVaultStore` passes a shared conformance suite; no `open()` outside store impls. | 13 | M |
| E2-2 | As a hosted user, my data lives in a managed store so I need no local install. **AC:** `ObjectStore` backend (S3-compatible + Postgres metadata) passes the same conformance suite. | 13 | M |
| E2-3 | As a user with two devices, edits from both merge without loss. **AC:** per-note version/etag; conflict → both kept, `-conflict-<device>-<ts>` sibling, surfaced in UI; test simulating concurrent writes. | 13 | M |
| E2-4 | As a user, writes are atomic and sync-client safe. **AC:** write-temp-then-rename with retry on Windows lock; documented "exclude these folders" for OneDrive/Dropbox; `doctor` detects a syncing vault and warns. | 5 | M |
| E2-5 | As a user, deleting is recoverable. **AC:** soft delete → trash with 30-day retention; restore path from UI. | 5 | S |
| E2-6 | As a user, my data is backed up automatically. **AC:** nightly snapshot, restore runbook tested end to end. | 5 | M |

### E3 — Identity, accounts, multi-tenancy

| ID | Story | Pts | M |
|---|---|---|---|
| E3-1 | As a new user, I sign up and sign in. **AC:** email+password (argon2) and one OAuth provider; verified email; rate-limited; sessions expire and can be revoked. | 8 | M |
| E3-2 | As a user, no request can read another user's data. **AC:** tenant id on every store call; automated test asserting cross-tenant 404 on every endpoint; CI fails on an unscoped query. | 8 | M |
| E3-3 | As a user, I can reset my password and change my email. | 3 | M |
| E3-4 | As a user, I can delete my account and everything in it. **AC:** hard delete within 30 days incl. index, logs, embeddings; confirmation email. | 5 | M |
| E3-5 | As a team lead, I can invite members to a shared workspace. | 8 | W |
| E3-6 | As an operator, I have an admin console for support (impersonate with consent, usage, plan). | 5 | S |

### E4 — Onboarding & first-run

| ID | Story | Pts | M |
|---|---|---|---|
| E4-1 | As a new user, I reach my first captured item in under 2 minutes. **AC:** 3-step wizard (goal → first capture → classify); measured funnel event at each step. | 8 | M |
| E4-2 | As a new user, I get a first study session with real cards on day one. **AC:** paste-or-upload a source → cards generated → session runnable, no vault knowledge required. | 8 | M |
| E4-3 | As a new user, empty states teach rather than sit blank. **AC:** every list/tab has copy + a primary action + a sample. | 5 | M |
| E4-4 | As a new user, a starter template pack seeds a usable structure. **AC:** PARA folders, 3 example Areas, 1 example Project, 1 Resource, all deletable in one click. | 5 | M |
| E4-5 | As a new user, an interactive tour explains the four buckets. | 3 | S |
| E4-6 | As a returning user, a "resume where you left off" card is the dashboard's first element. | 3 | S |

### E5 — Capture & classification, productized

| ID | Story | Pts | M |
|---|---|---|---|
| E5-1 | As a user, I capture from anywhere the fastest way available. **AC:** web quick-capture (global hotkey in installed app), email-to-inbox address, share target on mobile web. | 8 | M |
| E5-2 | As a user, classification suggests but never decides. **AC:** Area/Project/Resource buttons pre-highlighted by a model suggestion with confidence; one click confirms; suggestion accuracy logged. | 5 | M |
| E5-3 | As a user, a Project capture expands into the six metadata fields and I can correct any of them inline. **AC:** parse failures degrade to an editable blank field, never an error page. | 5 | M |
| E5-4 | As a user, capture works offline and syncs later. | 5 | C |
| E5-5 | As a user, I can capture a URL and get the readable text filed. **AC:** server-side fetch + readability extraction; paywalled/blocked pages fail gracefully. | 5 | S |

### E6 — Learning engine quality *(the differentiator)*

| ID | Story | Pts | M |
|---|---|---|---|
| E6-1 | As a learner, cards obey the minimum information principle. **AC:** validator rejects/splits multi-fact and enumeration answers and answers >15 words; prefers cloze for definitions; regression set of 100 labelled cards, precision ≥0.9. *(Woźniak, rules 4/5/7/8)* | 8 | M |
| E6-2 | As a learner, I predict before I reveal and see my calibration. **AC:** 3-point confidence tap logged beside grade; calibration curve + Brier score on Progress; high-confidence-wrong band is a priority queue. *(Koriat & Bjork; Dunlosky & Rawson)* | 8 | M |
| E6-3 | As a learner, I explain before I'm told. **AC:** on Again/Hard, one-line self-explanation prompt, model-marked, logged. *(Chi; Slamecka & Graf)* | 5 | S |
| E6-4 | As a learner on new material, cards start as worked examples and fade. *(Sweller)* | 8 | S |
| E6-5 | As a learner, practice interleaves problem types within a subject, not just across decks. *(Rohrer & Taylor)* | 5 | S |
| E6-6 | As a learner, I choose my retention/workload tradeoff and see projected daily minutes. **AC:** `desired_retention` slider 0.7–0.95 with a simulated minutes/day figure. *(Ye, FSRS simulator)* | 5 | S |
| E6-7 | As a learner, the scheduler personalizes once I have data, and tells me how far off that is until then. **AC:** FSRS weight fit at ≥1,000 reviews; `doctor`-style progress lines exposed in the UI, not just CLI. | 8 | M |
| E6-8 | As a learner, graduation Project→Resource is prompted and confirmed, never silent. | 3 | M |
| E6-9 | As a product owner, auto-accept thresholds are measured, not guessed. **AC:** 40+ labelled pairs each for intake and connect; floors set where precision ≥0.9; documented. | 5 | M |

### E7 — Habits & Areas

| ID | Story | Pts | M |
|---|---|---|---|
| E7-1 | As a user, each habit has an implementation intention: "When [cue], I will [behaviour] at [place]". **AC:** required field on Area creation, rendered into the reminder body. *(Gollwitzer, d≈0.65)* | 5 | M |
| E7-2 | As a user, my check-in surfaces consecutive misses, not broken streaks. **AC:** "never miss twice" alert; streak framing removed from failure copy. *(Lally et al.)* | 3 | M |
| E7-3 | As a user, I record an anchor routine and one friction change per habit. *(Fogg; Wood)* | 3 | S |
| E7-4 | As a user, context stability (same time/place) is tracked and shown alongside count. | 5 | S |
| E7-5 | As a user, the weekly check-in lets me continue, change the count, pause, or retire. | 3 | M |

### E8 — Projects, scheduling, external sync

| ID | Story | Pts | M |
|---|---|---|---|
| E8-1 | As a user, I connect Google Calendar via OAuth in the UI. **AC:** per-user tokens encrypted at rest, refresh handled, disconnect revokes. | 8 | M |
| E8-2 | As a user, deadlines and step blocks appear on my calendar and update when I change them. **AC:** idempotent upsert by external id; no duplicate events on re-sync. | 5 | M |
| E8-3 | As a user, completing a task on my phone completes it in the system. **AC:** Google Tasks read-back with the decided rule — **vault wins on content, Google wins on completion**; conflict test. | 8 | S |
| E8-4 | As a user, notifications reach me where I actually am. **AC:** calendar alerts + optional email digest + web push; per-channel opt-out. | 5 | S |
| E8-5 | As a user, my time estimates get corrected by my own history. **AC:** log actual elapsed per step; personal multiplier applied after 5 timed steps; est-vs-actual chart. *(Buehler/Kahneman)* | 5 | S |
| E8-6 | As a user, non-Google calendars work. **AC:** ICS feed export; CalDAV read. | 5 | C |

### E9 — Tutor / RAG at scale

| ID | Story | Pts | M |
|---|---|---|---|
| E9-1 | As a user, retrieval is scoped to my data by construction. **AC:** tenant filter in the index, not the query layer; cross-tenant retrieval test. | 8 | M |
| E9-2 | As a user, search stays fast as my vault grows. **AC:** swap pure-python cosine for a vector index (numpy → sqlite-vec/pgvector) above 5,000 chunks; p95 < 300ms at 100k chunks. | 8 | M |
| E9-3 | As a user, answers cite the notes they came from and I can open them. | 3 | M |
| E9-4 | As a user, Archive is excluded by default with an explicit "search archive too". | 2 | M |
| E9-5 | As a user, indexing is incremental and shows progress. **AC:** only changed notes re-embedded; visible reindex state; resumable. | 5 | M |
| E9-6 | As a user, the tutor says "I don't have that" instead of inventing. **AC:** grounding check; refusal path; hallucination eval set with a tracked score. | 5 | M |

### E10 — LLM routing, keys, quotas

| ID | Story | Pts | M |
|---|---|---|---|
| E10-1 | As a user, I can bring my own API key **or** use the hosted allowance. **AC:** key stored encrypted, validated on save, never logged. | 5 | M |
| E10-2 | As an operator, per-user token spend is metered and capped. **AC:** cost per call recorded; soft warn at 80%, hard stop at 100%; clear in-product message. | 8 | M |
| E10-3 | As a user, a local model (Ollama) is a supported backend for privacy or cost. **AC:** documented setup; capability matrix of what degrades. | 5 | S |
| E10-4 | As an operator, model choice per task is config, not code. **AC:** routing table (cheap for parsing, strong for card generation/tutoring) with per-task overrides. | 3 | M |
| E10-5 | As a user, a provider outage degrades the app, not breaks it. **AC:** retry+fallback provider; queued generation; UI states the delay. | 5 | M |

### E11 — Privacy, security & compliance

| ID | Story | Pts | M |
|---|---|---|---|
| E11-1 | As a user, I can export everything in an open format. **AC:** one-click zip of markdown + JSON metadata + review log; re-importable. | 5 | M |
| E11-2 | As a user, I know what leaves my machine and to whom. **AC:** privacy policy, sub-processor list, in-product disclosure at the point of the first LLM call; opt-out of any training use stated. | 5 | M |
| E11-3 | As a user, my data is encrypted in transit and at rest. **AC:** TLS everywhere, at-rest encryption on store + DB, secrets in a manager not env files in the repo. | 5 | M |
| E11-4 | As an operator, the app passes a baseline security review. **AC:** OWASP top-10 pass, dependency scanning in CI, CSRF/CORS/CSP set, authenticated rate limits, pen-test checklist. | 8 | M |
| E11-5 | As an EU user, GDPR rights are honored. **AC:** access/rectify/erase/portability flows; DPA available; retention documented. | 8 | M |
| E11-6 | As an operator, there is a documented incident response + breach notification runbook. | 3 | S |

### E12 — Billing & plans

| ID | Story | Pts | M |
|---|---|---|---|
| E12-1 | As a product owner, plans are defined and enforced. **AC:** Free (capped items/AI calls) vs Pro; limits enforced server-side; upgrade CTA at the limit. | 8 | S |
| E12-2 | As a user, I subscribe, change and cancel a plan. **AC:** Stripe checkout + portal, webhooks idempotent, dunning handled. | 8 | S |
| E12-3 | As a user, downgrading never deletes my data. **AC:** read-only overflow, explicit copy. | 3 | S |

### E13 — Observability, support & on-call

| ID | Story | Pts | M |
|---|---|---|---|
| E13-1 | As an operator, errors reach me before users report them. **AC:** Sentry (or equiv.) with PII scrubbing; alerting on error-rate SLO. | 3 | M |
| E13-2 | As an operator, I have product analytics on the activation funnel. **AC:** events for signup → capture → classify → first session → day-7 return; consented, anonymizable. | 5 | M |
| E13-3 | As an operator, uptime and latency have SLOs and a status page. | 5 | S |
| E13-4 | As a user, in-app feedback and a support inbox reach a human. | 3 | M |
| E13-5 | As an operator, a health check covers store, index and LLM provider — without the footer paying for it. | 3 | M |

### E14 — Cross-platform surfaces

| ID | Story | Pts | M |
|---|---|---|---|
| E14-1 | As a user, the web app is a usable PWA on my phone (installable, offline shell). | 8 | C |
| E14-2 | As a user, I can review cards on my phone comfortably. **AC:** touch grading, one-hand reach, 60fps. | 8 | C |
| E14-3 | As a user, the Obsidian plugin is published and works against the hosted account too. | 8 | C |
| E14-4 | As a user, a desktop installer exists for the local-only mode. **AC:** signed win/mac builds, auto-update. | 13 | C |

### E15 — Import & migration

| ID | Story | Pts | M |
|---|---|---|---|
| E15-1 | As an Obsidian user, I point at my vault and it's imported non-destructively. | 8 | S |
| E15-2 | As an Anki user, I import `.apkg` decks with scheduling history preserved where possible. | 8 | S |
| E15-3 | As a Notion/Evernote user, I import a markdown/HTML export. | 5 | C |
| E15-4 | As any user, import is previewed and reversible. | 3 | S |

### E16 — Accessibility, i18n, content

| ID | Story | Pts | M |
|---|---|---|---|
| E16-1 | As a keyboard/screen-reader user, the study loop is fully usable. **AC:** WCAG 2.2 AA on capture, dashboard, study; axe clean; focus order verified. | 8 | S |
| E16-2 | As a user, light and dark are both correct and text meets contrast. | 3 | M |
| E16-3 | As a non-English user, the UI is translatable. **AC:** strings externalized, one second locale shipped. | 8 | C |
| E16-4 | As a user, the product's copy is consistent and non-punitive. **AC:** voice/tone guide; every failure message reviewed. | 3 | S |

### E17 — Docs, site, community

| ID | Story | Pts | M |
|---|---|---|---|
| E17-1 | Marketing site: what it is, who it's for, pricing, demo video. | 8 | S |
| E17-2 | User docs: getting started, PARA concepts, study loop, habits, FAQ, troubleshooting. | 8 | M |
| E17-3 | Public roadmap + changelog + issue tracker. | 3 | S |
| E17-4 | Contribution/plugin docs if any part goes open source; license decided. | 5 | S |

### E18 — Beta program

| ID | Story | Pts | M |
|---|---|---|---|
| E18-1 | Waitlist + invite codes. | 3 | M |
| E18-2 | Weekly beta feedback loop: survey, session recordings (consented), triage ritual. | 5 | M |
| E18-3 | Activation dashboard against the R1 goal (50 users to day-7 streak). | 5 | M |

---

## 3. Carry-over from the personal roadmap (do before or during Sprint 1)

Small, overdue, and each one blocks something above.

- **T0.1** Run the suite on lj-studio (`doctor.bat`) — folded into E1-2.
- **T0.2** Exclude `_decks/` and `_system/` from OneDrive sync — precondition for E2-4.
- **T0.3** Clear `_to_delete/` (install tarballs + four backups).
- **T0.4** Wire the connect buttons into `sb/web/` — `POST /api/connect` and `/api/notes/{id}/connect` have zero callers.
- **T0.5** Commit the vault working tree (one commit exists) — precondition for E1-1.
- **T0.6** Run `test-log.bat` — the only phase-11 claim resting on a Linux run.
- **T1.1** Move linking into the atomize/generate call; `connect.py` becomes a periodic tidy.

---

## 4. Sprint plan (2-week sprints, R1 = Private Beta)

| Sprint | Theme | Committed |
|---|---|---|
| **S1** | Stop the bleeding | T0.1–T0.6, T1.1, E1-1, E1-2, E1-3 |
| **S2** | Storage becomes an interface | E2-1, E2-2, E2-4, E1-7 |
| **S3** | Accounts | E3-1, E3-2, E3-3, E3-4, E9-1, E13-1 |
| **S4** | A stranger can use it | E4-1..E4-4, E5-1, E5-2, E5-3 |
| **S5** | The thing that makes it worth using | E6-1, E6-2, E6-7, E6-9, E7-1, E7-2 |
| **S6** | Costs, limits, and not lying | E10-1, E10-2, E10-4, E10-5, E9-2, E9-5, E9-6 |
| **S7** | Trust | E11-1..E11-5, E2-3, E2-5, E2-6, E13-2, E13-5 |
| **S8** | Let people in | E8-1, E8-2, E17-2, E18-1, E18-2, E18-3, E1-6 |
| **S9+** | Post-beta | E12, E14, E15, E16, remaining S/C items |

---

## 5. Definition of Ready

A story enters a sprint only when it has: a user-facing outcome; acceptance
criteria that are testable; a named store/tenant impact (or "none"); a decision
on what happens on failure; and no unresolved external dependency.

## 6. Definition of Done

Merged behind review; automated tests covering the AC, green on Windows **and**
Linux CI; no new lint/type errors; tenant-isolation test if it touches data;
telemetry event if it's in the activation funnel; user-facing copy reviewed;
docs updated; migration written if the schema moved; feature-flagged if risky.

## 7. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Storage rewrite (E2) is bigger than 55 pts | Slips everything | Spike in S1; conformance suite written *first* |
| LLM cost per active user exceeds price | Unit economics fail | E10-2 metering before any public signup; BYO-key as the release valve |
| Two sessions/devices editing one vault | Silent data loss | E2-3 + E1-1 committed history, both early |
| Scheduling quality regresses under generic defaults | Core value lost | E6-7 progress lines; FSRS fit gated at 1,000 reviews |
| Privacy expectations for a "second brain" are unusually high | Adoption stalls | E11 in S7, before beta invites; local-model backend (E10-3) as a stated option |
| Solo maintainer | Everything | Cut E14/E15/E16 from R1; hold R1 scope to Musts |
