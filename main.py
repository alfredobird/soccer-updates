#!/usr/bin/env python3
"""
Scrapes ystpr.com for one team's fixtures across every jornada, plus the full
group standings, and writes a static index.html for GitHub Pages.

No email is sent. The page carries a share button that opens a mail client.
"""

import asyncio
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright

# ---------------------------------------------------------------- config

TEAM = os.environ.get("TEAM", "SURF GUAYNABO U10 2016 Black")
TOURNAMENT = os.environ.get("TOURNAMENT", "CHAMPIONSHIP")
DIVISION = os.environ.get("DIVISION", "TC 2026 U10 DIVISION 2 - Fase 1")
GROUP = os.environ.get("GROUP", "Grupo A")

SCHEDULE_URL = "https://ystpr.com/itinerario"
STANDINGS_URL = "https://ystpr.com/posiciones"
RESULTS_URL = "https://ystpr.com/resultados"

NICK = os.environ.get("NICK", "Surf Guaynabo U10 Black")
LANG = os.environ.get("LANG_OUT", "es")
MAX_JORNADAS = int(os.environ.get("MAX_JORNADAS", "40"))

DEBUG = Path("debug")
AST = timezone(timedelta(hours=-4))  # Puerto Rico, no DST

MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

# ---------------------------------------------------------------- helpers


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def matches(option: str, wanted: str) -> bool:
    have = set(norm(option).split())
    return bool(have) and set(norm(wanted).split()) <= have


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def stamp() -> str:
    now = datetime.now(AST)
    hour = now.hour % 12 or 12
    if LANG.startswith("es"):
        ampm = "a. m." if now.hour < 12 else "p. m."
        return (
            f"Actualizado: {now.day} de {MESES[now.month - 1]} de {now.year}, "
            f"{hour}:{now.minute:02d} {ampm}"
        )
    return (
        f"As of: {now:%B} {now.day}, {now.year}, "
        f"{hour}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}"
    )


async def settle(page, ms: int = 800) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(ms)


# ---------------------------------------------------------------- selection


async def list_controls(page) -> list[str]:
    found: list[str] = []
    selects = page.locator("select")
    for i in range(await selects.count()):
        try:
            opts = await selects.nth(i).locator("option").all_inner_texts()
        except Exception:
            continue
        found += [f"select[{i}]: {o.strip()}" for o in opts if o.strip()]
    try:
        for t in await page.get_by_role("option").all_inner_texts():
            if t.strip():
                found.append(f"option: {t.strip()}")
    except Exception:
        pass
    try:
        seen = set()
        for t in await page.locator(
            "li, a, button, div[role], label"
        ).all_inner_texts():
            t = " ".join(t.split())
            if t and len(t) <= 60 and t not in seen:
                seen.add(t)
                found.append(f"clickable: {t}")
    except Exception:
        pass
    return found


async def choose(page, wanted: str, latest: bool = False, quiet: bool = False):
    """Pick an option matching `wanted`, whatever kind of control holds it."""

    def rank(text: str) -> int:
        nums = re.findall(r"\d+", text)
        return int(nums[-1]) if nums else -1

    def best(items):
        return max(items, key=lambda x: rank(x[1])) if latest else items[0]

    selects = page.locator("select")
    for i in range(await selects.count()):
        sel = selects.nth(i)
        try:
            opts = await sel.locator("option").all_inner_texts()
        except Exception:
            continue
        hits = [(j, o) for j, o in enumerate(opts) if matches(o, wanted)]
        if hits:
            idx, text = best(hits)
            await sel.select_option(index=idx)
            await settle(page)
            if not quiet:
                log(f"  selected '{text.strip()}'")
            return text.strip()

    triggers = page.get_by_role("combobox")
    for i in range(await triggers.count()):
        try:
            await triggers.nth(i).click(timeout=2500)
        except Exception:
            continue
        await page.wait_for_timeout(450)
        opts = page.get_by_role("option")
        try:
            texts = await opts.all_inner_texts()
        except Exception:
            texts = []
        hits = [(j, t) for j, t in enumerate(texts) if matches(t, wanted)]
        if hits:
            idx, text = best(hits)
            await opts.nth(idx).click()
            await settle(page)
            if not quiet:
                log(f"  clicked '{text.strip()}'")
            return text.strip()
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    # Plain clickable text. Many of this site's pickers are not <select>
    # elements, so this path does most of the real work.
    try:
        bits = page.locator("li, a, button, td, th, div[role], span, p, label")
        texts = await bits.all_inner_texts()
    except Exception:
        texts = []
    hits = [(j, t) for j, t in enumerate(texts) if t.strip() and matches(t, wanted)]
    if hits:
        # Shortest match is the label itself rather than a wrapper element.
        hits.sort(key=lambda x: (-rank(x[1]), len(x[1])) if latest else (len(x[1]),))
        for idx, text in hits[:4]:
            try:
                await bits.nth(idx).click(timeout=2000)
                await settle(page)
                if not quiet:
                    log(f"  clicked text '{text.strip()[:60]}'")
                return text.strip()
            except Exception:
                continue

    if not quiet:
        log(f"  !! no control matched '{wanted}'. Visible options:")
        for line in await list_controls(page):
            log(f"       {line}")
    return None


async def option_labels(page, keyword: str) -> list[str]:
    """Every option whose text contains `keyword`, in the site's own order."""
    selects = page.locator("select")
    for i in range(await selects.count()):
        try:
            opts = await selects.nth(i).locator("option").all_inner_texts()
        except Exception:
            continue
        if any(keyword in norm(o) for o in opts):
            return [o.strip() for o in opts if o.strip() and keyword in norm(o)]

    triggers = page.get_by_role("combobox")
    for i in range(await triggers.count()):
        try:
            await triggers.nth(i).click(timeout=2000)
        except Exception:
            continue
        await page.wait_for_timeout(400)
        try:
            texts = await page.get_by_role("option").all_inner_texts()
        except Exception:
            texts = []
        hits = [t.strip() for t in texts if keyword in norm(t)]
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        if hits:
            return hits

    # Anything clickable whose text mentions the keyword.
    try:
        texts = await page.locator(
            "li, a, button, td, th, div[role], span, p, label"
        ).all_inner_texts()
    except Exception:
        texts = []
    seen, hits = set(), []
    for t in texts:
        t = " ".join(t.split())
        # A label, not a whole panel that happens to contain the word.
        if keyword in norm(t) and len(t) <= 30 and t not in seen:
            seen.add(t)
            hits.append(t)
    if hits:
        return hits
    return []


# ---------------------------------------------------------------- reading


async def read_table(page, limit: int = 400) -> list[list[str]]:
    """Every table row as a list of cell strings, structure preserved."""
    rows: list[list[str]] = []
    trs = page.locator("table tr")
    try:
        total = await trs.count()
    except Exception:
        return rows
    for i in range(min(total, limit)):
        try:
            cells = await trs.nth(i).locator("th, td").all_inner_texts()
        except Exception:
            continue
        cells = [" ".join(c.split()) for c in cells]
        if any(c for c in cells):
            rows.append(cells)
    return rows


def header_row(rows: list[list[str]], data_row: list[str]) -> list[str]:
    """The last row above `data_row` of equal width carrying no digits."""
    try:
        cut = rows.index(data_row)
    except ValueError:
        cut = len(rows)
    cands = [
        r for r in rows[:cut]
        if len(r) == len(data_row) and not any(re.search(r"\d", c) for c in r)
    ]
    return cands[-1] if cands else []


def dedupe(rows: list[list[str]]) -> list[list[str]]:
    """Drop rows whose text is contained in a wider row already kept."""
    out: list[list[str]] = []
    for r in sorted(rows, key=len, reverse=True):
        sig = norm(" ".join(r))
        if sig and not any(sig in norm(" ".join(k)) for k in out):
            out.append(r)
    return out


def parse_table(rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """Split scraped rows into a header and its data rows."""
    header = next(
        (r for r in rows
         if len(r) >= 3 and not any(re.search(r"\d", c) for c in r)),
        [],
    )
    width = len(header) if header else (max((len(r) for r in rows), default=0))
    data = dedupe([
        r for r in rows
        if len(r) == width and r != header and any(re.search(r"\d", c) for c in r)
    ])
    data.sort(key=lambda r: rows.index(r) if r in rows else 0)
    return header, data


def find_rows(rows: list[list[str]], needle: str) -> list[list[str]]:
    return dedupe([r for r in rows if any(norm(needle) in norm(c) for c in r)])


def pairs(headers: list[str], row: list[str]) -> list[tuple[str, str]]:
    out = []
    for i, value in enumerate(row):
        if not value.strip():
            continue
        out.append((headers[i].strip() if i < len(headers) else "", value.strip()))
    return out


async def dump(page, name: str) -> None:
    DEBUG.mkdir(exist_ok=True)
    try:
        await page.screenshot(path=str(DEBUG / f"{name}.png"), full_page=True)
        (DEBUG / f"{name}.txt").write_text(await page.inner_text("body"))
    except Exception as e:
        log(f"  (could not save debug output: {e})")


# ---------------------------------------------------------------- scraping


async def get_fixtures(page) -> list[dict]:
    """One entry per jornada: the label and our team's row, if any."""
    log("Loading schedule page")
    await page.goto(SCHEDULE_URL, wait_until="domcontentloaded")
    await settle(page, 2500)

    log("  controls on the page before any selection:")
    for line in (await list_controls(page))[:80]:
        log(f"       {line}")

    await choose(page, TOURNAMENT)
    labels = await option_labels(page, "jornada")
    if not labels:
        log("  !! found no jornada options")
        await dump(page, "schedule")
        return []
    labels = labels[:MAX_JORNADAS]
    log(f"  {len(labels)} jornadas to walk")

    out = []
    bad = 0
    for n, label in enumerate(labels, 1):
        if not await choose(page, label, quiet=True):
            continue
        got_div = await choose(page, DIVISION, quiet=True)
        got_grp = await choose(page, GROUP, quiet=True)
        await settle(page, 900)

        # A silent fallback would show another group's fixtures and look fine,
        # so record what the pickers actually landed on.
        ok = bool(got_div) and bool(got_grp)
        if not ok:
            bad += 1

        rows = await read_table(page)
        hits = find_rows(rows, TEAM) or find_rows(rows, "SURF GUAYNABO")
        entry = {
            "jornada": label,
            "headers": [],
            "row": [],
            "group": got_grp or "",
            "division": got_div or "",
            "verified": ok,
        }
        if hits:
            entry["row"] = hits[0]
            entry["headers"] = header_row(rows, hits[0])
        out.append(entry)
        log(
            f"  [{n}/{len(labels)}] {label} | {got_div or 'DIVISION NOT SET'}"
            f" | {got_grp or 'GROUP NOT SET'}"
            f" | {'match found' if hits else 'no match'}"
        )
        if n == len(labels):
            await dump(page, "schedule")

    if bad:
        log(f"  !! {bad} of {len(out)} jornadas could not confirm the filters")
    return out


async def get_results(page, wanted: set[str]) -> dict:
    """Our team's result row for each jornada already played."""
    if not wanted:
        return {}
    log("Loading results page")
    await page.goto(RESULTS_URL, wait_until="domcontentloaded")
    await settle(page, 2500)
    await choose(page, TOURNAMENT)

    labels = [l for l in await option_labels(page, "jornada") if l in wanted]
    if not labels:
        log("  !! no matching jornadas on the results page")
        await dump(page, "results")
        return {}
    log(f"  {len(labels)} played jornadas to check")

    out = {}
    for n, label in enumerate(labels, 1):
        if not await choose(page, label, quiet=True):
            continue
        await choose(page, DIVISION, quiet=True)
        await choose(page, GROUP, quiet=True)
        await settle(page, 900)

        rows = await read_table(page)
        hits = find_rows(rows, TEAM) or find_rows(rows, "SURF GUAYNABO")
        if hits:
            out[label] = {"headers": header_row(rows, hits[0]), "row": hits[0]}
            log(f"  [{n}/{len(labels)}] {label}: {' | '.join(hits[0])}")
        else:
            log(f"  [{n}/{len(labels)}] {label}: no result row")
        if n == len(labels):
            await dump(page, "results")
    return out


async def get_standings(page) -> dict:
    log("Loading standings page")
    await page.goto(STANDINGS_URL, wait_until="domcontentloaded")
    await settle(page, 2500)

    picked = [
        await choose(page, TOURNAMENT) or TOURNAMENT,
        await choose(page, DIVISION) or DIVISION,
    ]
    await settle(page, 900)

    groups = await option_labels(page, "grupo") or [GROUP]
    log(f"  {len(groups)} groups to walk")

    tables = []
    for g in groups:
        await choose(page, DIVISION, quiet=True)
        if not await choose(page, g, quiet=True):
            continue
        await settle(page, 900)
        headers, rows = parse_table(await read_table(page))
        if not rows:
            continue
        tables.append({"group": g, "headers": headers, "rows": rows})
        log(f"  {g}: {len(rows)} teams")

    await dump(page, "standings")
    if not tables:
        log("  !! no standings tables found")
        return {}
    return {"context": picked, "tables": tables}


async def scrape():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 1600}, locale="es-PR"
        )
        page = await ctx.new_page()
        try:
            fixtures = await get_fixtures(page)

            # Only chase results for jornadas that have already been played.
            today = datetime.now(AST).date()
            played = {
                f["jornada"] for f in fixtures
                if f.get("row") and (fixture_date(f) or today) <= today
            }
            results = await get_results(page, played)
            for f in fixtures:
                f["result"] = results.get(f["jornada"], {})

            return fixtures, await get_standings(page)
        finally:
            await browser.close()


# ---------------------------------------------------------------- page

ES = {"match": "Partidos", "table": "Tabla de posiciones",
      "none": "Sin partido en esta jornada.", "share": "Email",
      "wa": "WhatsApp", "sms": "Texto", "shareVia": "Compartir vía:",
      "unverified": "No se pudo confirmar el grupo. Verifica en ystpr.com.",
      "next": "Próximo", "result": "Resultado",
      "notable": "Tabla no disponible."}
EN = {"match": "Matches", "table": "Standings",
      "none": "No match this jornada.", "share": "Email",
      "wa": "WhatsApp", "sms": "Text", "shareVia": "Share via:",
      "unverified": "Could not confirm the group. Check ystpr.com.",
      "next": "Next", "result": "Result",
      "notable": "Standings unavailable."}


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def page_url() -> str:
    """The published Pages URL, worked out from the repo at build time."""
    explicit = os.environ.get("PAGE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return ""
    owner, name = repo.split("/", 1)
    if name.lower() == f"{owner.lower()}.github.io":
        return f"https://{name.lower()}"
    return f"https://{owner.lower()}.github.io/{name}"


def parse_date(text: str):
    """Best-effort date out of a cell. Tries US order first, then day-first."""
    m = re.search(r"\d{1,4}[/-]\d{1,2}[/-]\d{2,4}", (text or "").strip())
    if not m:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(m.group(0), fmt).date()
        except ValueError:
            continue
    return None


def fixture_date(entry: dict):
    """The date of a jornada's match, preferring a column labelled Fecha."""
    headers, row = entry.get("headers") or [], entry.get("row") or []
    for i, h in enumerate(headers):
        if "fecha" in norm(h) and i < len(row):
            d = parse_date(row[i])
            if d:
                return d
    for cell in row:
        d = parse_date(cell)
        if d:
            return d
    return None


def result_summary(res: dict) -> str:
    """One line: our goals first, then theirs, with a win/loss/draw marker.

    The results page's column layout is unknown, so this works it out from the
    row itself: find the two goal numbers, then find which side we are on.
    """
    row = [c.strip() for c in (res.get("row") or [])]
    if not row:
        return ""

    # Goals may be one cell ("3 - 1") or two separate numeric cells.
    goals, goal_at = None, None
    for i, cell in enumerate(row):
        m = re.fullmatch(r"(\d{1,2})\s*[-–:xX]\s*(\d{1,2})", cell)
        if m:
            goals, goal_at = (int(m.group(1)), int(m.group(2))), i
            break
    if goals is None:
        nums = [(i, int(c)) for i, c in enumerate(row) if re.fullmatch(r"\d{1,2}", c)]
        if len(nums) < 2:
            return " · ".join(c for c in row if c)  # unparseable, show it raw
        goals, goal_at = (nums[0][1], nums[1][1]), nums[0][0]

    us_idx = next((i for i, c in enumerate(row) if matches(c, TEAM)), None)
    if us_idx is None:
        us_idx = next(
            (i for i, c in enumerate(row) if "surf guaynabo" in norm(c)), None
        )
    if us_idx is None:
        return " · ".join(c for c in row if c)

    # The home side is written before the score, the away side after it.
    ours, theirs = goals if us_idx < goal_at else (goals[1], goals[0])
    mark = "✅" if ours > theirs else ("❌" if ours < theirs else "🟡")
    return f"{ours}-{theirs} {mark}"


def team_name(row: list[str]) -> str:
    """The longest cell that isn't a number: the club name."""
    words = [c for c in row if c.strip() and not re.fullmatch(r"-?\d+", c.strip())]
    return max(words, key=len).strip() if words else ""


def fixture_text(entry: dict, t: dict, label: str = "") -> str:
    """One jornada, short enough to read in a chat bubble."""
    lines = [label or entry["jornada"]]
    if not entry.get("verified", True):
        lines.append(f"({t['unverified']})")
    if not entry.get("row"):
        return "\n".join(lines + [t["none"]])
    for head, value in pairs(entry.get("headers", []), entry["row"]):
        lines.append(f"{head}: {value}" if head else value)

    summary = result_summary(entry.get("result") or {})
    if summary:
        lines.append(f"{t['result']}: {summary}")
    return "\n".join(lines)


def standings_text(table: dict, t: dict) -> str:
    """Rank and club name only. No column alignment to survive."""
    if not table:
        return f"{t['table']}\n{t['notable']}"
    out = [f"{t['table']} ({table['group']})"]
    for n, r in enumerate(table["rows"], 1):
        rank = r[0].strip() if r and re.fullmatch(r"\d+", r[0].strip()) else str(n)
        out.append(f"{rank}. {team_name(r)}")
    return "\n".join(out)


def digest(fixtures: list[dict], standings: dict) -> str:
    """Fingerprint of the data only. Deliberately excludes the timestamp."""
    blob = json.dumps(
        {"f": fixtures, "s": standings, "nick": NICK, "lang": LANG},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def unchanged(fp: str) -> bool:
    page = Path("index.html")
    if not page.exists():
        return False
    try:
        return f'data-digest="{fp}"' in page.read_text(encoding="utf-8")
    except Exception:
        return False


def write_page(fixtures: list[dict], standings: dict, fp: str) -> None:
    t = ES if LANG.startswith("es") else EN
    es = LANG.startswith("es")

    # The next fixture is the earliest one dated today or later.
    today = datetime.now(AST).date()
    upcoming = next(
        (
            i for i, f in enumerate(fixtures)
            if f.get("row") and (fixture_date(f) or today - timedelta(days=1)) >= today
        ),
        None,
    )
    labels = [
        f"{f['jornada']} ({t['next']})" if i == upcoming else f["jornada"]
        for i, f in enumerate(fixtures)
    ]
    # Open on the upcoming jornada, else the last one with a match.
    start = upcoming if upcoming is not None else max(
        (i for i, f in enumerate(fixtures) if f.get("row")),
        default=len(fixtures) - 1,
    )

    cards = []
    for f in fixtures:
        if f.get("verified"):
            tag = " · ".join(x for x in (f.get("division"), f.get("group")) if x)
            banner = f"<p class=sub>{esc(tag)}</p>" if tag else ""
        else:
            banner = f"<p class=warn>{esc(t['unverified'])}</p>"
        if f.get("row"):
            body = "".join(
                f"<div class=row><span class=k>{esc(h)}</span>"
                f"<span class=v>{esc(v)}</span></div>"
                for h, v in pairs(f.get("headers", []), f["row"])
            )
        else:
            body = f"<p class=empty>{esc(t['none'])}</p>"

        summary = result_summary(f.get("result") or {})
        if summary:
            body += (
                f"<div class=row><span class=k>{esc(t['result'])}</span>"
                f"<span class='v score'>{esc(summary)}</span></div>"
            )
        cards.append(banner + body)

    context = [c for c in (standings.get("context") or []) if c]
    context_html = (
        f"<p class=sub>{esc(' · '.join(context))}</p>" if context else ""
    )

    tables = standings.get("tables", [])
    gLabels, gTables, gTexts = [], [], []
    gStart = 0
    for n, tb in enumerate(tables):
        if any(any(matches(c, TEAM) for c in r) for r in tb["rows"]):
            gStart = n  # open on the group our team plays in
        thead = "".join(f"<th>{esc(h)}</th>" for h in tb["headers"])
        body = ""
        for r in tb["rows"]:
            mine = " class=mine" if any(matches(c, TEAM) for c in r) else ""
            body += f"<tr{mine}>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>"
        gLabels.append(tb["group"])
        gTables.append(
            f"<div class=scroll><table><thead><tr>{thead}</tr></thead>"
            f"<tbody>{body}</tbody></table></div>"
        )
        gTexts.append(standings_text(tb, t))
    if not tables:
        gLabels, gTables, gTexts = [""], [f"<p class=empty>{esc(t['notable'])}</p>"], [
            f"{t['table']}\n{t['notable']}"
        ]

    share_parts = [f"{NICK}\n{stamp()}\n"]
    payload = {
        "labels": labels,
        "cards": cards,
        "texts": [fixture_text(f, t, labels[i]) for i, f in enumerate(fixtures)],
        "start": start,
        "head": "".join(share_parts),
        "gLabels": gLabels,
        "gTables": gTables,
        "gTexts": gTexts,
        "gStart": gStart,
        "subject": f"{NICK} - {stamp()}",
        "url": page_url(),
    }

    html = f"""<!doctype html>
<html lang="{'es' if es else 'en'}" data-digest="{fp}">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0b1b2b">
<title>{esc(NICK)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin:0; padding:24px 16px 48px;
    font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
    background:#f4f6f8; color:#10202e;
  }}
  main {{ max-width:560px; margin:0 auto; }}
  h1 {{ font-size:22px; margin:0 0 20px; letter-spacing:-.01em; }}
  .stamp {{
    color:#5d7080; font-size:12.5px; line-height:1.4; margin:0 0 22px;
  }}
  section {{
    background:#fff; border-radius:14px; padding:14px 16px;
    margin-bottom:16px; box-shadow:0 1px 3px rgba(16,32,46,.10);
  }}
  h2 {{
    font-size:12px; letter-spacing:.09em; text-transform:uppercase;
    color:#5d7080; margin:0 0 12px; font-weight:600;
  }}
  .nav {{ display:flex; align-items:center; gap:10px; margin-bottom:12px; }}
  .nav button {{
    flex:0 0 40px; height:40px; border:0; border-radius:10px;
    background:#eef1f4; color:#10202e; font-size:20px; line-height:1;
    cursor:pointer;
  }}
  .nav button:disabled {{ opacity:.32; cursor:default; }}
  .nav .label {{ flex:1; text-align:center; font-weight:600; font-size:14px; }}
  .row {{ display:flex; gap:12px; padding:8px 0; border-top:1px solid #eef1f4; }}
  .row:first-child {{ border-top:0; }}
  .k {{
    flex:0 0 36%; color:#5d7080; font-size:12px; font-weight:600;
    text-transform:uppercase; letter-spacing:.04em; padding-top:2px;
  }}
  .v {{ flex:1; font-size:14px; font-variant-numeric:tabular-nums; }}
  .empty {{ color:#5d7080; margin:8px 0; }}
  .warn {{
    color:#8a4b00; background:#fdf1e0; border-radius:8px;
    padding:8px 10px; font-size:13px; margin:0 0 10px;
  }}
  .sub {{
    color:#5d7080; font-size:12.5px; line-height:1.4; margin:2px 0 2px;
  }}
  .scroll {{ overflow-x:auto; -webkit-overflow-scrolling:touch; }}
  table {{ border-collapse:collapse; width:100%; font-size:14px; }}
  th,td {{
    padding:8px 7px; text-align:right; white-space:nowrap;
    font-variant-numeric:tabular-nums; border-top:1px solid #eef1f4;
  }}
  th {{
    color:#5d7080; font-weight:600; font-size:12px; border-top:0;
    text-transform:uppercase; letter-spacing:.04em;
  }}
  th:nth-child(2),td:nth-child(2) {{ text-align:left; white-space:normal; min-width:150px; }}
  tr.mine td {{ background:#eaf1fd; font-weight:600; }}
  .send {{
    display:block; text-align:center; text-decoration:none;
    background:#10202e; color:#fff; border-radius:12px;
    padding:14px 18px; font-size:16px; font-weight:500; margin-bottom:16px;
  }}
  .send:active {{ opacity:.75; }}
  .actions {{ display:flex; gap:8px; margin-bottom:16px; }}
  .actions .send {{
    flex:1 1 0; margin-bottom:0; padding:14px 6px; font-size:15px;
    min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  }}
  .score {{ font-weight:700; font-size:15px; }}
  .sharelabel {{
    font-size:12px; letter-spacing:.09em; text-transform:uppercase;
    color:#5d7080; font-weight:600; margin:22px 0 10px;
  }}
  .send.wa {{ background:#1f9d5b; }}
  .send.sms {{ background:#2f6fed; }}
  footer {{
    text-align:center; font-size:12.5px; line-height:1.5;
    color:#5d7080; margin-top:26px;
  }}
  footer p {{ margin:0; }}
  footer a {{ color:#5d7080; display:inline-block; margin-top:8px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background:#0b1b2b; color:#e8eef4; }}
    section {{ background:#14293c; box-shadow:none; }}
    .row,th,td {{ border-top-color:#1e3752; }}
    .k,.stamp,.empty,.sub,.sharelabel,th,footer a {{ color:#90a6b8; }}
    .warn {{ color:#ffcf93; background:#3a2a12; }}
    .nav button {{ background:#1e3752; color:#e8eef4; }}
    tr.mine td {{ background:#1d3f63; }}
    .send {{ background:#2f6fed; }}
    .send.wa {{ background:#1f9d5b; }}
    .send.sms {{ background:#2f6fed; }}
  .send.sms {{ background:#2f6fed; }}
  }}
</style>
<main>
  <h1>{esc(NICK)}</h1>

  <section>
    <h2>{esc(t['match'])}</h2>
    <div class=nav>
      <button id=prev aria-label="anterior">&lsaquo;</button>
      <span class=label id=jlabel></span>
      <button id=next aria-label="siguiente">&rsaquo;</button>
    </div>
    <div id=card></div>
  </section>

  <section>
    <h2>{esc(t['table'])}</h2>
    <div class=nav>
      <button id=gprev aria-label="grupo anterior">&lsaquo;</button>
      <span class=label id=glabel></span>
      <button id=gnext aria-label="grupo siguiente">&rsaquo;</button>
    </div>
    <div id=gtable></div>
  </section>

  <p class=sharelabel>{esc(t['shareVia'])}</p>
  <div class=actions>
    <a class="send wa" id=wa href="#">{esc(t['wa'])}</a>
    <a class="send sms" id=sms href="#">{esc(t['sms'])}</a>
    <a class=send id=share href="#">{esc(t['share'])}</a>
  </div>
  <footer>
    {context_html}
    <p class=stamp>{esc(stamp())}</p>
    <a href="https://ystpr.com/itinerario">ystpr.com</a>
  </footer>
</main>
<script>
const D = {json.dumps(payload, ensure_ascii=False)};
let i = D.start;
let g = D.gStart;
const card = document.getElementById('card');
const label = document.getElementById('jlabel');
const prev = document.getElementById('prev');
const next = document.getElementById('next');
const share = document.getElementById('share');
const wa = document.getElementById('wa');
const sms = document.getElementById('sms');
const gprev = document.getElementById('gprev');
const gnext = document.getElementById('gnext');
const glabel = document.getElementById('glabel');
const gtable = document.getElementById('gtable');
// iOS and Android disagree on the sms: separator; this form satisfies both.
const SMS_SEP = /iPhone|iPad|iPod|Macintosh/.test(navigator.userAgent) ? '&' : '?';

function render() {{
  label.textContent = D.labels[i] || '';
  card.innerHTML = D.cards[i] || '';
  prev.disabled = i <= 0;
  next.disabled = i >= D.labels.length - 1;

  glabel.textContent = D.gLabels[g] || '';
  gtable.innerHTML = D.gTables[g] || '';
  gprev.disabled = g <= 0;
  gnext.disabled = g >= D.gLabels.length - 1;

  let body = D.head + '\\n' + D.texts[i] + '\\n\\n' + D.gTexts[g] + '\\n';
  if (D.url) body += '\\n' + D.url + '\\n';
  share.href = 'mailto:?subject=' + encodeURIComponent(D.subject)
             + '&body=' + encodeURIComponent(body);
  wa.href = 'https://wa.me/?text=' + encodeURIComponent(body);
  sms.href = 'sms:' + SMS_SEP + 'body=' + encodeURIComponent(body);
}}
prev.onclick = () => {{ if (i > 0) {{ i--; render(); }} }};
next.onclick = () => {{ if (i < D.labels.length - 1) {{ i++; render(); }} }};
gprev.onclick = () => {{ if (g > 0) {{ g--; render(); }} }};
gnext.onclick = () => {{ if (g < D.gLabels.length - 1) {{ g++; render(); }} }};
render();
</script>
"""
    Path("index.html").write_text(html, encoding="utf-8")
    log("Wrote index.html")


def main() -> None:
    fixtures, standings = asyncio.run(scrape())
    if not fixtures and not standings:
        log("Nothing extracted. Check the debug/ artifacts.")
        sys.exit(1)

    fp = digest(fixtures, standings)
    if unchanged(fp) and "--force" not in sys.argv:
        log("No change in fixtures or standings. Leaving index.html alone.")
        return
    write_page(fixtures, standings, fp)


if __name__ == "__main__":
    main()
