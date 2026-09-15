# Surf Guaynabo U10 Black

A phone-friendly page showing this team's fixtures, results and group
standings, scraped from [ystpr.com](https://ystpr.com) every 30 minutes.

The page is rebuilt on every run so the `Actualizado` line always reflects the
last check, not the last change. Commits are labelled `data:` when the
fixtures, results or standings actually moved, and `refresh:` when only the
clock did.

Live page: `https://<your-username>.github.io/soccer-updates`

No server, no accounts, no cost. GitHub Actions runs the scrape and GitHub
Pages serves the result.

## What the page shows

- **Partidos** opens on the next fixture, tagged `(Próximo)`. Arrows move
  through every jornada the site publishes. Played ones show a single
  `RESULTADO` line with our goals first and a ✅ / 🟡 / ❌ marker.
- **Tabla de posiciones** opens on our group, with our row highlighted.
  Arrows page through the other groups.
- **Compartir vía** builds a short plain-text summary of whatever is on
  screen and hands it to WhatsApp, Messages or email. Nothing is ever sent
  automatically.
- Two buttons sit top right. **Light/dark** follows the phone's setting until
  you override it. **EN/ES** switches the page language. Both choices are
  remembered separately on that device.

On iOS, Share then Add to Home Screen gives it an app icon.

### About the language toggle

Both languages are built into the page at scrape time, so switching is instant
and needs no network. Only the league's own vocabulary is translated, word by
word, keeping the original capitalisation: `Fecha` to `Date`, `JORNADA 5` to
`MATCHDAY 5`, `Finalizado` to `Final`, `PJ` to `GP`. Anything not in that
dictionary passes through untouched, which is what keeps club names and venues
like `TORRIMAR ROJA` intact. The dictionaries are `TERMS` and `VALUES` near the
top of `main.py`; add a pair if the league starts using a word it misses.

The share text follows whichever language is on screen.

## Email alerts

Optional, and off unless you set the secrets. It emails on exactly two events:

- a jornada appears that wasn't there before
- our group's finishing order changes

Points moving without the order changing does not trigger one. Neither does
anything in another group. The first run after setup records the current state
without emailing, so you don't get a spurious "everything is new" message.

Add three repo secrets: `SMTP_USER` (a Gmail address), `SMTP_PASS` (a Google
app password from myaccount.google.com/apppasswords, not your normal password,
and it needs 2-Step Verification switched on), and `EMAIL_TO` (recipients,
comma-separated). Leave them unset and the page still builds, it just never
emails. `SMTP_HOST` and `SMTP_PORT` are optional and default to Gmail.

Alerts are written in whatever `LANG_OUT` is set to, since an email has no
toggle to click. The page's own toggle doesn't affect them.

`state.json` holds the last known jornada list and group order. It's committed
so it survives between runs. Don't delete it, or the next run treats itself as
a first run.

## Files

| File | Purpose |
|---|---|
| `main.py` | Scrapes the three ystpr pages and writes `index.html` |
| `.github/workflows/updates.yml` | Runs it on a schedule and commits changes |
| `index.html` | Generated. Never edit by hand, every run overwrites it |
| `state.json` | Generated. What the last email alert was based on |
| `icon.png` | Home-screen icon and link-preview image. Static, upload once |

## Setup

1. The repo must be **public**. GitHub Pages only serves private repos on a
   paid plan, so making it private breaks the site.
2. Settings > Pages > Source: *Deploy from a branch*, branch `main`, folder `/ (root)`.
3. Settings > Actions > General > Workflow permissions: **Read and write**.
4. Upload `icon.png` to the repo root (Add file > Upload files). It never
   changes, so this is one-time.
5. Actions tab > soccer-updates > Run workflow.

## Configuration

Set as repository variables, or edit the constants at the top of `main.py`.

| Name | Default |
|---|---|
| `TEAM` | `SURF GUAYNABO U10 2016 Black` |
| `TOURNAMENT` | `CHAMPIONSHIP` |
| `DIVISION` | `TC 2026 U10 DIVISION 2 - Fase 1` |
| `GROUP` | `Grupo A` |
| `NICK` | page title |
| `SUBTITLE` | line under the title. A label, not scraped |
| `LANG_OUT` | `es` or `en`. The language the page opens in, and the only one used for email alerts |
| `MAX_JORNADAS` | `40`. Cap on how many jornadas to walk |
| `PAGE_URL` | only if the Pages URL isn't the default shape, e.g. a custom domain |

Dropdown matching ignores punctuation, accents and spacing, so only the words
have to line up. The division carries the season year while the team name
carries the birth year. They differ, and that's correct.

## How it avoids showing you the wrong data

The site's pickers aren't `<select>` elements, and a failed selection leaves
the previous group on screen rather than raising an error. That looks normal
and is wrong. So every run records which filters actually took:

- the run log prints the division and group per jornada
- the page shows them under each heading
- any jornada whose filters couldn't be confirmed gets an amber warning, on
  the page and in the shared text

## When it breaks

It will, because it depends on someone else's layout. The symptom is a stale
`Actualizado` line or missing rows. Open the failed run on the Actions tab and
download the `debug` artifact: a screenshot and text dump of each page. When a
selection fails, the log also prints every option that was visible at the time.

## Cost

Nothing. Public repos get unlimited Actions minutes and free Pages.
