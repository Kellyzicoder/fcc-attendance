# ⛪ FCC Attendance Tracker

Attendance tracking for **Favourite Child Church**: ushers tick people in on their phones, leaders see who has been
missing and follow up.

**Live:** https://fcc-attendance.streamlit.app

## Pages

| Page | What it does |
|---|---|
| Dashboard (home) | KPI tiles (last service, 4-service average, who needs a call, first-timers this month), where everyone stands (donut), people present over time, needs-follow-up list, and a side panel with notifications, latest check-ins and who to call next. Refreshes every 30 s. |
| Follow-up & Check-in | **Needs follow-up**: 🟡 yellow = 3–4 services missed in a row, 🔴 red = 5+; filter by pastor; CSV download. **Check-in**: ushers tick people as they arrive, ticks sync to every phone within ~3 s; admins can untick everyone for a service in one step. **One person**: pick someone and see every day they came. **Pastors**: each pastor's list of about ten people, who came, who to call, and a WhatsApp message to copy. **Archive**: people not seen for two years. |
| Live | Real-time view of today's check-ins — count, first-timers, arrivals over time, latest arrivals. Refreshes itself; good on a screen during service. |
| Insights | Attendance per service (members vs first-timers, 4-service average), first-timers per month, first-timer return rate, attendance by group. |
| Members | The register (editable), sign-ups from the welcome form to approve, add people, import the Google Sheets CSV exports, and an **Activity** log of every change. |
| Reports | The daily email to leaders: who gets it (add or remove addresses), a live preview, **Send report now**, the Excel attachment, and a log of every email sent. |
| SQL | Read-only SQL queries against the database, with ready-made examples; download results. |

**Who sees what.** Two passwords, set in the app's Secrets:

- `attendance_password`: the team (ushers, leaders). Opens Dashboard, Follow-up & Check-in, Live and Insights.
- `admin_password`: admins only. Also shows the **Admin** section (Members, Reports, SQL). People signed in with the
  team password don't see it at all. If `admin_password` isn't set, the team password opens everything.

The sidebar shows who is signed in and has a **Sign out** button.

**Safe when many people use it at once.** The same ideas banks use for payments:

| Situation | What happens |
|---|---|
| Two ushers tick the same person | One tick is kept; ticking is "make present", so repeating it changes nothing. |
| Someone unticks from an out-of-date screen | The untick only removes the tick that usher was looking at. If another phone re-ticked the person meanwhile, the newer tick stays and the usher gets a message. |
| Two admins approve the same sign-up | Approving is one all-or-nothing transaction that starts by claiming the sign-up, so only the first admin succeeds and nobody is added twice. If anything fails part-way, the sign-up stays pending. |
| Two admins edit the same person | Each member has a version number. A save made from a stale screen is refused, and that admin is asked to redo it on the latest details. |
| Welcome form sent twice (bad signal) | Each sign-up carries its own id, so a resend can't create a second copy. |
| "Who unticked Grace?" | Members → **Activity** lists every tick, untick, edit and approval with who and when. It is only ever added to. |

Ushers can type their name on the Check-in tab so it shows in the Activity log. The app adds the `activity_log`
table and the `members.version` column itself; there is no SQL to run.

**Taking someone off the red list.** Tick them in when they come (it clears itself), or set their Status in
Members → Register to **Away** (travelling, unwell) or Moved/Inactive. People not seen for two years move to the
Archive on their own and return the day they are ticked in again.

**WhatsApp summary.** Dashboard → *Summary for WhatsApp* gives this week's numbers as text to copy and paste;
names are left out unless you tick *Include names*.

## Making changes safely

Nothing goes straight to the live app. Changes are pushed to the preview branch, which updates
https://fcc-attendance-preview.streamlit.app (demo data only). A pull request into `main` runs the checks in
`tests/` (the app starts, every page opens, and the follow-up, archive, check-in and approval rules still hold).
Merge only when the checks are green and the preview looks right; the live app then updates itself.
To undo, press **Revert** on the merged pull request.

Dark dashboard theme in the church colours (logo greens and gold). Chart colours are checked for colour-blind
separation and contrast. The sidebar has **Layout** controls (names per row on Check-in, panel stacking).

## Daily email

Every day by 5pm (NZ) a summary goes to the addresses on the Reports page (default greaterloveauckland@gmail.com):
check-ins, who needs a follow-up call (with phone numbers), new welcome-form sign-ups, plus an Excel workbook
(Checked in · Follow-up · Sign-ups · Services) that opens in Excel or Google Sheets.

- Scheduled by `.github/workflows/daily-report.yml` (GitHub Actions). Cron is UTC, so it tries several times across
  NZST/NZDT; `scripts/daily_report.py` sends on the first run after 4:40pm NZ and logs it in `email_log`, so later
  runs that evening skip. A failed run makes GitHub email the repo owner.
- Sent through Brevo's free email API (300/day). Secrets: in the app `brevo_api_key`, `report_sender`; in GitHub
  Actions `DATABASE_URL`, `BREVO_API_KEY`, `REPORT_SENDER`. (A Gmail app password via `smtp_user`/`smtp_password`
  also works as a fallback.)
- Leaders can also press **Send report now** (Dashboard or Reports) any time, e.g. right after a service.

## Data

Stored in a **Postgres** database (Supabase) — never in this repo. Tables:

- `members` — id, full_name, phone, email, group_name, role, status, type (member / first_timer), date_joined, first_visit, invited_by, follow_up
- `services` — service_date, name
- `attendance` — service_date, member_id, checked_at (one row per person ticked per service)
- `settings`, `email_log` — report recipients and a record of every email sent
- `registrations` — sign-ups from the [welcome form](https://github.com/Kellyzicoder/fcc-welcome), approved under *Members → Sign-ups*

Until `database_url` is set in the app's Streamlit **Secrets**, the pages run on a SQLite demo database with invented
names. Setup steps are in the app under *Members → Setup*. Query the data from the SQL page, Supabase's SQL editor,
or Python (`pandas.read_sql`).

CSV files and connection strings are blocked by `.gitignore`; don't commit them — this repo is public.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

For real data locally, put `database_url`, `attendance_password` and `admin_password` in `.streamlit/secrets.toml` (git-ignored).
