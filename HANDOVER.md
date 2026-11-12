# Handover

`README.md` tells you how to run it. `ARCHITECTURE.md` tells you how it works.
`ENGINEERING-LOG.md` tells you why every decision was made.

This file is the fourth thing: **what you need access to, what is not in the
repository, and what is waiting on somebody.** None of it can be worked out
from the code.

No credential values appear here. Only what exists and where to find it.

---

## 1. Accounts and access you need

| What | Why | How you get it |
|---|---|---|
| **Slack app** (workspace `mmc sport`, bot `commentary_qc`) | Alerts | Admin on the app at api.slack.com/apps — ask the outgoing owner to add you |
| **GitHub** `venkat9furmi/Signal-AI` | The code | Collaborator access |
| **The host machine** | It only runs where Docker runs | See §4 |
| **Gemini API key** *(optional)* | Live AI dubbing | aistudio.google.com — free tier covers it |
| **Camb AI key** *(optional)* | The second dubbing model | client.camb.ai — see the terms warning in §5 |

Nothing in the quality-monitoring half needs a login. Stream discovery and
audio capture have never required credentials of any kind, and that is
deliberate — see `ENGINEERING-LOG.md`, "Scope settled".

---

## 2. Credentials: what exists, where it lives, what to do

Two files, both gitignored and dockerignored, both read from the environment
by the containers. Neither has ever been committed.

### `.slack.env`

```
SLACK_BOT_TOKEN     starts xoxb-, from api.slack.com/apps -> OAuth & Permissions
SLACK_CHANNEL_ID    starts C, from the channel's About panel in Slack
QC_PASSCODE         optional; set it before the page leaves localhost
```

**You do not need to create a new Slack app or a new bot.** The token belongs
to the app, not to a person, so the same workspace and the same channel keep
working when the project changes hands. What has to change hands is **admin
access to the app**, so you can rotate the token and change scopes.

**Rotate the bot token on day one.** It was pasted into a chat during
development. Regenerate it in the same app — do not create a second one — and
put the new value in `.slack.env`, then `docker compose restart`.

Without this file the recorder still runs and alerts still reach the page;
Slack simply stays in dry-run. It is not a hard dependency.

### `.translate.env`

```
GEMINI_API_KEY      optional
CAMB_API_KEY        optional
```

`.translate.env.example` is committed as the template. Without the file the
dubbing half starts switched off and costs nothing.

### Also rotate

- The **`xapp-` app-level token** — same reason as the bot token
- The **MOD / vendor panel password** — pasted in a chat during the one-off
  read of the channel list

---

## 3. What is NOT in this repository

| | Where it is | Can it be recreated? |
|---|---|---|
| The databases (`live.db`, `claims.db`, `translate.db`) | Docker named volume `commentary-qc-data` | **No.** 1.5M+ measurements and every alert back to 15 September |
| `.slack.env`, `.translate.env` | The host machine only | Yes, if you have the accounts |
| `clips/` — 1.7 GB of recorded match audio | The host machine only | **No.** The platform is live-only |
| `assessment*/` — the listening tests | The host machine only | **No.** They are the provenance of every threshold |

The last two matter more than they look. They are the evidence behind
"why −40 LUFS?", and the platform cannot be re-recorded. **Copy them off the
machine before it is wiped.**

Snapshot the databases with:

```
docker compose run --rm backup      # writes ./backup/
```

---

## 4. Where it runs, and the problem with that

Two Docker containers on a Windows laptop. `restart: unless-stopped` handles
crashes, but **not a reboot** — Docker Desktop does not start itself. It was
down eleven hours once for exactly that reason.

Immediate fix: Docker Desktop → Settings → General → *Start Docker Desktop
when you sign in*.

Real fix: move it to a machine that is always on. A small mini-PC or a cloud
VM removes a whole class of problem, and nothing in the design assumes a
laptop. This is the single highest-value piece of ops work outstanding.

---

## 5. Decisions somebody made, that you should not quietly reverse

- **Do not build on the vendor's Playlist Controller or its data feed.** It
  was read once, with permission, to obtain the channel list; the session and
  browser profile were deleted afterwards. Audio comes from the open stream
  host and needs no login.
- **Cricket is out of scope.** "We only do football."
- **Never put the SQLite databases on a host bind mount.** It looks like it
  works and silently is not. `ENGINEERING-LOG.md`, 2026-10-20.
- **Nothing the dubbing half produces is published.** Before that changes,
  somebody needs a written answer on both vendors' terms: Camb names gambling
  as prohibited content and restricts passing output to third parties, and
  Gemini's free tier states that input and output are used to improve Google's
  products. The audience here would be a bookmaker's customers.

---

## 6. Waiting on people, not on code

- Ask **bet365, via MMC**, what loudness target they actually expect. Every
  loud/quiet threshold rests on an assumption of −18 LUFS cross-checked
  against standards and one listening test by one person. **This is the
  biggest open question in the project.**
- Ask the **vendor (mediaondemand / Matt Job)** for a stable whitelisted
  stream endpoint. The current path has survived one panel migration; nobody
  has promised it will survive the next.
- **Sit with a commentator during a match.** The thresholds come from
  recordings. Watching somebody use it will find things no test will.

---

## 7. Your first week

1. `docker compose up -d --build`, then watch `docker compose logs -f recorder`
   for ten minutes. Open <http://127.0.0.1:8642>.
2. Run the tests. They pass, they need no API key, and they are the fastest
   description of what the system promises.
3. Read `README.md` → "If you are picking this up". It gives a file-by-file
   order and roughly how long each takes.
4. Rotate the Slack token. Get admin on the Slack app.
5. Copy `clips/`, `assessment*/` and a database snapshot off the machine.
6. Read `ARCHITECTURE.md` §9 and §10 — what to improve, ranked, and the
   to-do list. Two entries there say *investigate, do not patch*; they mean it.
