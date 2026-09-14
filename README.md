# Surf Guaynabo U10 Black

A page that shows this team's fixtures and its Grupo A standings, scraped from
[ystpr.com](https://ystpr.com) every three hours.

Live page: `https://<your-username>.github.io/soccer-updates`

Nothing to install and nothing to pay for. GitHub Actions runs the scrape,
GitHub Pages serves the result.

## What the page does

- **Partido** opens on the most recent jornada with a match for this team.
  The arrows move through every other jornada the site publishes, past and
  upcoming. Jornadas where the team doesn't play say so.
- **Tabla de posiciones** shows the full group table in the site's own order,
  with this team's row highlighted. It scrolls sideways on a phone.
- **Compartir por correo** opens a mail app with the jornada currently on
  screen, plus the whole table, already written into the body. No email is
  ever sent automatically.

On an iPhone, Share then Add to Home Screen gives it an app icon.

## Files

| File | Purpose |
|---|---|
| `main.py` | Scrapes both ystpr pages and writes `index.html` |
| `.github/workflows/updates.yml` | Runs it every three hours and commits the result |
| `index.html` | Generated. Don't edit by hand, it's overwritten each run |

## Setup

1. Repo must be **public** for free GitHub Pages.
2. Settings > Pages > Source: *Deploy from a branch*, branch `main`, folder `/ (root)`.
3. Actions tab > soccer-updates > Run workflow, to publish the first page.

## Changing the team or division

Set these as repository variables (Settings > Secrets and variables > Actions >
Variables), or edit the top of `main.py`.

| Name | Default |
|---|---|
| `TEAM` | `SURF GUAYNABO U10 2016 Black` |
| `TOURNAMENT` | `CHAMPIONSHIP` |
| `DIVISION` | `TC 2026 U10 DIVISION 2 - Fase 1` |
| `GROUP` | `Grupo A` |
| `NICK` | `Surf Guaynabo U10 Black` (the page title) |
| `LANG_OUT` | `es`, or `en` for English section headings |

The division uses the season year and the team name uses the birth year. They
don't match, and that's correct.

Dropdown matching is punctuation-blind, so accents, en dashes and extra spaces
in these values won't break it. Only the words have to line up.

## When it breaks

It will eventually, because it depends on someone else's page layout.

The symptom is a stale `Actualizado` timestamp or missing rows. Open the
failed run on the Actions tab and download the `debug` artifact at the bottom.
It has a screenshot and a text dump of what the browser actually saw. If a
dropdown couldn't be found, the log also prints every option that was visible
at that moment, which is usually enough to spot a renamed division.

## Cost

Nothing. Public repos get unlimited Actions minutes and free Pages.
