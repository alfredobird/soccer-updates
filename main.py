#!/usr/bin/env python3
"""
Builds a static page showing one team's fixtures, results and group standings,
scraped from ystpr.com.

Writes index.html only when the data has actually changed, so an unchanged run
leaves git with nothing to commit.
"""

import asyncio
import hashlib
import json
import os
import re
import smtplib
import sys
import unicodedata
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright

# --------------------------------------------------------------------- config

TEAM = os.environ.get("TEAM", "SURF GUAYNABO U10 2016 Black")
TOURNAMENT = os.environ.get("TOURNAMENT", "CHAMPIONSHIP")
DIVISION = os.environ.get("DIVISION", "TC 2026 U10 DIVISION 2 - Fase 1")
GROUP = os.environ.get("GROUP", "Grupo A")

NICK = os.environ.get("NICK", "Surf Guaynabo U10 Black")
# A label, not scraped. Edit if the league renames the competition.
SUBTITLE = os.environ.get("SUBTITLE", "TORNEO CHAMPIONSHIP 2026 - Fase 1")
LANG = os.environ.get("LANG_OUT", "es")
MAX_JORNADAS = int(os.environ.get("MAX_JORNADAS", "40"))

SCHEDULE_URL = "https://ystpr.com/itinerario"
RESULTS_URL = "https://ystpr.com/resultados"
STANDINGS_URL = "https://ystpr.com/posiciones"

DEBUG = Path("debug")
PAGE = Path("index.html")
STATE = Path("state.json")
AST = timezone(timedelta(hours=-4))  # Puerto Rico, no daylight saving

# Abbreviated to four letters or fewer. Months already that short are left
# whole, which is the convention in both languages.
MESES = [
    "ene.", "feb.", "mar.", "abr.", "mayo", "jun.",
    "jul.", "ago.", "sept.", "oct.", "nov.", "dic.",
]
MONTHS = [
    "Jan.", "Feb.", "Mar.", "Apr.", "May", "June",
    "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec.",
]

ES = {
    "mailNew": "Nueva jornada publicada",
    "mailRank": "Posiciones actualizadas",
    "newJornadas": "Nuevas jornadas",
    "match": "Partidos", "table": "Tabla de posiciones", "result": "Resultado",
    "none": "Sin partido en esta jornada.", "notable": "Tabla no disponible.",
    "next": "Próximo", "shareVia": "Compartir vía:", "share": "Email",
    "wa": "WhatsApp", "sms": "Texto",
    "unverified": "No se pudo confirmar el grupo. Verifica en ystpr.com.",
}
EN = {
    "mailNew": "New jornada published",
    "mailRank": "Standings changed",
    "newJornadas": "New jornadas",
    "match": "Matches", "table": "Standings", "result": "Result",
    "none": "No match this jornada.", "notable": "Standings unavailable.",
    "next": "Next", "shareVia": "Share via:", "share": "Email",
    "wa": "WhatsApp", "sms": "Text",
    "unverified": "Could not confirm the group. Check ystpr.com.",
}

# Spanish terms the site uses, for the English view. Anything not listed is
# left exactly as scraped, which is right for team names and venues.
TERMS = {
    "fecha": "Date", "dia": "Date", "partido": "Match", "posicion": "Rank",
    "oponente": "Opponent",
    "pos": "Rank", "hora": "Time", "local": "Home", "visitante": "Away",
    "cancha": "Field", "sede": "Venue", "estatus": "Status", "estado": "Status",
    "arbitro": "Referee", "equipo": "Team", "grupo": "Group",
    "jornada": "Matchday", "division": "Division", "resultado": "Result",
    "pj": "GP", "pg": "W", "pe": "D", "pp": "L", "gf": "GF", "gc": "GA",
    "dif": "GD", "pts": "Pts", "puntos": "Pts",
}
VALUES = {
    "finalizado": "Final", "programado": "Scheduled", "aplazado": "Postponed",
    "suspendido": "Suspended", "cancelado": "Cancelled", "por jugar": "To play",
    "en vivo": "Live", "descanso": "Bye",
}

# -------------------------------------------------------------------- helpers


def norm(s: str) -> str:
    """Lowercase, strip accents, drop punctuation. For fuzzy text matching."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-zA-Z0-9]+", " ", s)).strip().lower()


def matches(option: str, wanted: str) -> bool:
    """True when every word of `wanted` appears in `option`.

    Punctuation-blind, so an en dash or an accented DIVISION still matches.
    """
    have = set(norm(option).split())
    return bool(have) and set(norm(wanted).split()) <= have


def like(source: str, translated: str) -> str:
    """Give `translated` the capitalisation of `source`."""
    letters = [c for c in source if c.isalpha()]
    if not letters:
        return translated
    if all(c.isupper() for c in letters):
        return translated.upper()
    if all(c.islower() for c in letters):
        return translated.lower()
    if source[:1].isupper():
        return translated[:1].upper() + translated[1:]
    return translated


def to_en(text: str) -> str:
    """Translate the league's own vocabulary, word by word.

    Unknown words pass through untouched, so club names survive intact, and
    each translation copies the capitalisation of the word it replaces.
    """
    if not text:
        return text
    whole = VALUES.get(norm(text))
    if whole:
        return like(text, whole)
    out = []
    for word in text.split():
        core = norm(word)
        out.append(like(word, TERMS[core]) if core in TERMS else word)
    return " ".join(out)


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def long_date(d, lang: str) -> str:
    """14 de septiembre de 2026 / September 14, 2026."""
    if lang.startswith("es"):
        return f"{d.day} de {MESES[d.month - 1]} de {d.year}"
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"


def stamp(lang: str = "") -> str:
    lang = lang or LANG
    now = datetime.now(AST)
    hour = now.hour % 12 or 12
    when = long_date(now, lang)
    if lang.startswith("es"):
        ampm = "a. m." if now.hour < 12 else "p. m."
        return f"Actualizado: {when}, {hour}:{now.minute:02d} {ampm}"
    ampm = "AM" if now.hour < 12 else "PM"
    return f"Updated: {when}, {hour}:{now.minute:02d} {ampm}"


def date_only(text: str):
    """The date, when the whole cell is nothing but a date."""
    t = (text or "").strip()
    if not re.fullmatch(r"\d{1,4}[/-]\d{1,2}[/-]\d{2,4}", t):
        return None
    return parse_date(t)


def value_conv(lang: str):
    """Render a scraped cell: long-form dates, then vocabulary."""
    def convert(text: str) -> str:
        d = date_only(text)
        if d:
            return long_date(d, lang)
        return text if lang.startswith("es") else to_en(text)
    return convert


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def page_url() -> str:
    """The published Pages URL, derived from the repo at build time."""
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


async def settle(page, ms: int = 800) -> None:
    """Let the app finish re-rendering after a selection."""
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(ms)


async def dump(page, name: str) -> None:
    DEBUG.mkdir(exist_ok=True)
    try:
        await page.screenshot(path=str(DEBUG / f"{name}.png"), full_page=True)
        (DEBUG / f"{name}.txt").write_text(await page.inner_text("body"))
    except Exception as e:
        log(f"  (could not save debug output: {e})")


# ------------------------------------------------------------------ selection


async def list_controls(page) -> list[str]:
    """Every option visible right now. Printed when a selection fails."""
    out: list[str] = []
    selects = page.locator("select")
    for i in range(await selects.count()):
        try:
            opts = await selects.nth(i).locator("option").all_inner_texts()
        except Exception:
            continue
        out += [f"select[{i}]: {o.strip()}" for o in opts if o.strip()]

    for locator, tag in ((page.get_by_role("option"), "option"),
                         (page.locator("li, a, button, div[role], label"), "clickable")):
        try:
            seen = set()
            for txt in await locator.all_inner_texts():
                txt = " ".join(txt.split())
                if txt and len(txt) <= 60 and txt not in seen:
                    seen.add(txt)
                    out.append(f"{tag}: {txt}")
        except Exception:
            pass
    return out


def _rank(text: str) -> int:
    nums = re.findall(r"\d+", text)
    return int(nums[-1]) if nums else -1


async def choose(page, wanted: str, latest: bool = False, quiet: bool = False):
    """Select `wanted` from whatever kind of control holds it.

    Tries native selects, then ARIA comboboxes, then plain clickable text.
    Most of this site's pickers are not <select> elements, so the last path
    does the real work. Returns the label it landed on, or None.
    """
    def best(hits):
        return max(hits, key=lambda h: _rank(h[1])) if latest else hits[0]

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
        hits = [(j, x) for j, x in enumerate(texts) if matches(x, wanted)]
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

    # Interactive tags first: clicking a <span> often does nothing at all.
    for selector in ("a, button, li, [role=option], [role=tab], [role=button]",
                     "td, th, div[role], span, p, label, div"):
        bits = page.locator(selector)
        try:
            texts = await bits.all_inner_texts()
        except Exception:
            continue
        hits = [(j, x) for j, x in enumerate(texts) if x.strip() and matches(x, wanted)]
        if not hits:
            continue
        # Exact label beats a wrapper that merely contains it.
        hits.sort(key=lambda h: (-_rank(h[1]),) if latest else
                  (norm(h[1]) != norm(wanted), len(h[1])))
        for idx, text in hits[:4]:
            try:
                await bits.nth(idx).click(timeout=2000)
                await settle(page)
                if not quiet:
                    log(f"  clicked <{selector.split(',')[0]}> '{text.strip()[:50]}'")
                return text.strip()
            except Exception:
                continue

    if not quiet:
        log(f"  !! no control matched '{wanted}'. Visible options:")
        for line in await list_controls(page):
            log(f"       {line}")
    return None


_JS_DESCRIBE = """(stem) => {
  const rx = new RegExp(stem, 'i');
  const out = [];
  document.querySelectorAll('*').forEach(el => {
    if (el.children.length) return;                 // leaf elements only
    const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
    if (!t || t.length > 40 || !rx.test(t)) return;
    const r = el.getBoundingClientRect();
    const p = el.parentElement;
    out.push({
      text: t,
      tag: el.tagName.toLowerCase(),
      cls: String(el.className || '').slice(0, 70),
      visible: !!(r.width && r.height),
      parent: p ? p.tagName.toLowerCase() + '.' + String(p.className || '').slice(0, 60) : '',
      html: p ? p.outerHTML.replace(/\\s+/g, ' ').slice(0, 300) : ''
    });
  });
  return out.slice(0, 10);
}"""


async def describe(page, stem: str) -> None:
    """Print what the picker for `stem` actually looks like in the DOM."""
    try:
        found = await page.evaluate(_JS_DESCRIBE, stem)
    except Exception as e:
        log(f"  (describe failed: {e})")
        return
    log(f"  --- elements matching '{stem}' ---")
    for f in found:
        log(f"    <{f['tag']} class='{f['cls']}'> visible={f['visible']}"
            f" text='{f['text']}' parent={f['parent']}")
        log(f"      {f['html']}")
    log("  --- end ---")


async def choose_in_dropdown(page, wanted: str, stem: str):
    """Open a collapsed picker, then click the option inside it.

    The site's pickers show the current value and hide the rest until the
    trigger is clicked, so clicking the option directly does nothing.
    """
    exact = re.compile(rf"^\s*{re.escape(wanted)}\s*$", re.I)
    trigs = page.locator(
        "button, [role=button], [role=combobox], [aria-haspopup], "
        "[data-toggle=dropdown], .dropdown-toggle, summary, select"
    )
    try:
        total = await trigs.count()
    except Exception:
        return None

    for i in range(min(total, 30)):
        trig = trigs.nth(i)
        try:
            text = " ".join((await trig.inner_text()).split())
        except Exception:
            continue
        if stem not in norm(text):
            continue
        try:
            await trig.click(timeout=2000)
        except Exception:
            continue
        await page.wait_for_timeout(450)

        opt = page.get_by_text(exact)
        try:
            if await opt.count():
                await opt.first.click(timeout=2000)
                await settle(page)
                log(f"  opened '{text[:30]}' then picked '{wanted}'")
                return wanted
        except Exception:
            pass
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
    return None


# The jornada picker is a Kendo toggle-button group: the active button carries
# aria-pressed="true", which is a real signal rather than a guess.
_JS_TABS = """() => {
  const out = [];
  document.querySelectorAll('button, [role=tab]').forEach(b => {
    const t = (b.innerText || '').replace(/\\s+/g, ' ').trim();
    if (!/jornada/i.test(t) || t.length > 30) return;
    out.push({
      text: t,
      pressed: b.getAttribute('aria-pressed') === 'true',
      disabled: b.disabled || b.getAttribute('aria-disabled') === 'true'
                || b.className.includes('k-disabled')
    });
  });
  return out;
}"""


async def jornada_tabs(page) -> list[dict]:
    try:
        return await page.evaluate(_JS_TABS)
    except Exception as e:
        log(f"  (tab read failed: {e})")
        return []


async def table_sig(page) -> str:
    rows = await read_cells(page)
    return norm(" | ".join(" ".join(r) for r in rows))[:400]


async def active_jornada(page) -> str:
    for tab in await jornada_tabs(page):
        if tab["pressed"]:
            return tab["text"]
    return ""


async def select_jornada(page, label: str) -> bool:
    """Click a jornada button, then wait for the view to actually follow.

    Kendo flips aria-pressed on click, but the table re-renders separately,
    so a pressed button alone does not mean the data on screen changed.
    """
    tabs = await jornada_tabs(page)
    if not tabs:
        # No toggle group on this page: fall back to a plain click.
        got = await choose(page, label, quiet=True)
        await settle(page)
        return bool(got)

    # Only trust aria-pressed if this page actually sets it.
    verifiable = any(t["pressed"] for t in tabs)
    before_sig = await table_sig(page)
    already = norm(await active_jornada(page)) == norm(label)
    if not verifiable and len(tabs) == 1:
        already = True

    exact = re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
    try:
        btn = page.get_by_role("button", name=exact)
        if not await btn.count():
            log(f"  no button found for '{label}'")
            return False
        await btn.first.click(timeout=3000)
    except Exception as e:
        log(f"  could not click '{label}': {e}")
        return False

    # Poll rather than sleep a fixed amount: the re-render is asynchronous.
    for _ in range(16):
        await page.wait_for_timeout(500)
        pressed = (not verifiable
                   or norm(await active_jornada(page)) == norm(label))
        if pressed and (already or await table_sig(page) != before_sig):
            return True

    log(f"  '{label}' did not take: active='{await active_jornada(page)}',"
        f" verifiable={verifiable}, already={already},"
        f" table changed={await table_sig(page) != before_sig}")
    return False


async def option_labels(page, keyword: str) -> list[str]:
    """Every option mentioning `keyword`, in the site's own order."""
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

    try:
        texts = await page.locator(
            "li, a, button, td, th, div[role], span, p, label"
        ).all_inner_texts()
    except Exception:
        return []
    seen, hits = set(), []
    for t in texts:
        t = " ".join(t.split())
        # A label, not a panel that merely contains the word.
        if keyword in norm(t) and len(t) <= 30 and t not in seen:
            seen.add(t)
            hits.append(t)
    return hits


# -------------------------------------------------------------------- reading

# One evaluate call per table, rather than a round trip per row.
_JS_CELLS = """() => {
  const out = [];
  document.querySelectorAll('table tr').forEach(tr => {
    const row = [...tr.querySelectorAll('th, td')]
      .map(c => c.innerText.replace(/\\s+/g, ' ').trim());
    if (row.some(c => c)) out.push(row);
  });
  return out;
}"""

# The results page puts a whole match in one cell with the scores as adjacent
# inline nodes, so cell text gives "Black32CATALA". Text nodes give the pieces.
_JS_NODES = """() => {
  const out = [];
  document.querySelectorAll('table tr').forEach(tr => {
    const row = [];
    const w = document.createTreeWalker(tr, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
      const t = n.textContent.replace(/\\s+/g, ' ').trim();
      if (t) row.push(t);
    }
    if (row.length) out.push(row);
  });
  return out;
}"""


async def read_cells(page) -> list[list[str]]:
    try:
        return await page.evaluate(_JS_CELLS)
    except Exception as e:
        log(f"  (cell read failed: {e})")
        return []


async def read_nodes(page) -> list[list[str]]:
    try:
        return await page.evaluate(_JS_NODES)
    except Exception as e:
        log(f"  (node read failed: {e})")
        return []


def dedupe(rows: list[list[str]]) -> list[list[str]]:
    """Drop rows whose text is already contained in a wider row.

    The site renders a second, narrower copy of each row for small screens.
    """
    kept: list[list[str]] = []
    for r in sorted(rows, key=len, reverse=True):
        sig = norm(" ".join(r))
        if sig and not any(sig in norm(" ".join(k)) for k in kept):
            kept.append(r)
    return kept


def find_rows(rows: list[list[str]], needle: str) -> list[list[str]]:
    return dedupe([r for r in rows if any(norm(needle) in norm(c) for c in r)])


def our_rows(rows: list[list[str]]) -> list[list[str]]:
    return find_rows(rows, TEAM) or find_rows(rows, "SURF GUAYNABO")


def header_row(rows: list[list[str]], data_row: list[str]) -> list[str]:
    """The last row above `data_row`, of equal width, carrying no digits."""
    try:
        cut = rows.index(data_row)
    except ValueError:
        cut = len(rows)
    cands = [r for r in rows[:cut]
             if len(r) == len(data_row) and not any(re.search(r"\d", c) for c in r)]
    return cands[-1] if cands else []


def parse_table(rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """Split scraped rows into a header and its data rows."""
    header = next(
        (r for r in rows if len(r) >= 3 and not any(re.search(r"\d", c) for c in r)),
        [],
    )
    width = len(header) if header else max((len(r) for r in rows), default=0)
    data = dedupe([r for r in rows
                   if len(r) == width and r != header
                   and any(re.search(r"\d", c) for c in r)])
    data.sort(key=lambda r: rows.index(r) if r in rows else 0)
    return header, data


# "A vs B", however the site writes the separator.
VERSUS = re.compile(r"\s+(?:vs\.?|v\.?|contra)\s+", re.I)


def opponent(value: str):
    """The other team, from a cell holding both. None if it isn't one."""
    parts = [p.strip() for p in VERSUS.split(value or "") if p.strip()]
    if len(parts) != 2:
        return None
    ours = [p for p in parts
            if matches(p, TEAM) or "surf guaynabo" in norm(p)]
    if len(ours) != 1:
        return None
    return next(p for p in parts if p is not ours[0])


def fixture_rows(entry: dict) -> list[tuple[str, str]]:
    """Header/value pairs, with the both-teams cell reduced to the opponent."""
    out = []
    for head, value in pairs(entry.get("headers", []), entry.get("row", [])):
        other = opponent(value)
        if other:
            out.append((like(head, "Oponente") if head else "Oponente", other))
        else:
            out.append((head, value))
    return out


def pairs(headers: list[str], row: list[str]) -> list[tuple[str, str]]:
    return [
        (headers[i].strip() if i < len(headers) else "", v.strip())
        for i, v in enumerate(row) if v.strip()
    ]


def parse_date(text: str):
    """Best-effort date from a cell. US order first, then day-first."""
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


def team_name(row: list[str]) -> str:
    """The longest cell that isn't a number: the club name."""
    words = [c for c in row if c.strip() and not re.fullmatch(r"-?\d+", c.strip())]
    return max(words, key=len).strip() if words else ""


def result_summary(res: dict) -> str:
    """One line: our goals first, then theirs, with a win/draw/loss marker.

    The results page's column layout is unknown, so this works it out from the
    row: find the goals, then decide which side we are on by where they sit.
    """
    row = [c.strip() for c in (res.get("row") or [])]
    if not row:
        return ""

    goals = goal_at = None
    for i, cell in enumerate(row):
        m = re.fullmatch(r"(\d{1,2})\s*[-–:xX]\s*(\d{1,2})", cell)
        if m:
            goals, goal_at = (int(m.group(1)), int(m.group(2))), i
            break
    if goals is None:
        nums = [(i, int(c)) for i, c in enumerate(row) if re.fullmatch(r"\d{1,2}", c)]
        # Adjacent numbers, so a stray leading count can't be read as a score.
        pair = next(((a, b) for a, b in zip(nums, nums[1:]) if b[0] == a[0] + 1), None)
        if pair is None:
            if len(nums) < 2:
                return " · ".join(c for c in row if c)  # unparseable, show raw
            pair = (nums[0], nums[1])
        goals, goal_at = (pair[0][1], pair[1][1]), pair[0][0]

    us = next((i for i, c in enumerate(row) if matches(c, TEAM)), None)
    if us is None:
        us = next((i for i, c in enumerate(row) if "surf guaynabo" in norm(c)), None)
    if us is None:
        return " · ".join(c for c in row if c)

    # The home side is written before the score, the away side after it.
    ours, theirs = goals if us < goal_at else (goals[1], goals[0])
    mark = "✅" if ours > theirs else ("❌" if ours < theirs else "🟡")
    return f"{ours}-{theirs} {mark}"


# ------------------------------------------------------------------- scraping


async def get_fixtures(page) -> list[dict]:
    """Our team's row for every jornada, with the filters it was read under."""
    log("Loading schedule page")
    await page.goto(SCHEDULE_URL, wait_until="domcontentloaded")
    await settle(page, 2500)

    log("  controls before any selection:")
    for line in (await list_controls(page))[:80]:
        log(f"       {line}")

    await choose(page, TOURNAMENT)

    tabs = await jornada_tabs(page)
    if tabs:
        skipped = [t["text"] for t in tabs if t["disabled"]]
        if skipped:
            log(f"  ignoring disabled jornadas: {', '.join(skipped)}")
        labels = [t["text"] for t in tabs if not t["disabled"]]
    else:
        labels = await option_labels(page, "jornada")
    labels = labels[:MAX_JORNADAS]
    if not labels:
        log("  !! found no jornada options")
        await dump(page, "schedule")
        return []
    log(f"  {len(labels)} jornadas to walk")

    out, bad, seen = [], 0, {}
    for n, label in enumerate(labels, 1):
        # Selecting a jornada collapses the picker, so reload to get it back.
        if n > 1:
            await page.goto(SCHEDULE_URL, wait_until="domcontentloaded")
            await settle(page, 1800)
            await choose(page, TOURNAMENT, quiet=True)

        # Division and group first: selecting them resets the jornada.
        division = await choose(page, DIVISION, quiet=True)
        group = await choose(page, GROUP, quiet=True)
        await settle(page, 900)

        if not await select_jornada(page, label):
            log(f"  [{n}/{len(labels)}] {label}: !! view never switched, skipping")
            bad += 1
            if n == 2:
                await describe(page, "jornada")
            continue

        # A silent fallback would show another group and look perfectly normal,
        # so record whether the filters really took.
        verified = bool(division and group)
        if not verified:
            bad += 1

        rows = await read_cells(page)
        hits = our_rows(rows)

        # A click that changes nothing still "succeeds", so compare the result
        # against what earlier jornadas returned.
        sig = norm(" ".join(hits[0])) if hits else ""
        if sig and sig in seen:
            log(f"  [{n}/{len(labels)}] {label}: !! identical to {seen[sig]},"
                f" skipping rather than showing a duplicate")
            bad += 1
            continue
        if sig:
            seen[sig] = label

        out.append({
            "jornada": label,
            "headers": header_row(rows, hits[0]) if hits else [],
            "row": hits[0] if hits else [],
            "division": division or "",
            "group": group or "",
            "verified": verified,
            "result": {},
        })
        log(f"  [{n}/{len(labels)}] {label} | {division or 'DIVISION NOT SET'}"
            f" | {group or 'GROUP NOT SET'} | {'match' if hits else 'no match'}")

    if bad:
        log(f"  !! {bad} of {len(labels)} jornadas failed to select or verify")
    if len(out) < len(labels):
        log(f"  !! walked {len(out)} of {len(labels)} jornadas")
    await dump(page, "schedule")
    return out


async def get_results(page, wanted: set) -> dict:
    """Our team's result row for each jornada already played."""
    if not wanted:
        return {}
    log("Loading results page")
    await page.goto(RESULTS_URL, wait_until="domcontentloaded")
    await settle(page, 2500)
    await choose(page, TOURNAMENT)

    tabs = await jornada_tabs(page)
    found = ([t["text"] for t in tabs if not t["disabled"]] if tabs
             else await option_labels(page, "jornada"))
    labels = [l for l in found if l in wanted]
    if not labels:
        log("  !! no matching jornadas on the results page")
        await dump(page, "results")
        return {}
    log(f"  {len(labels)} played jornadas to check")

    out, seen_results = {}, {}
    for n, label in enumerate(labels, 1):
        if n > 1:
            await page.goto(RESULTS_URL, wait_until="domcontentloaded")
            await settle(page, 1800)
            await choose(page, TOURNAMENT, quiet=True)

        await choose(page, DIVISION, quiet=True)
        await choose(page, GROUP, quiet=True)
        await settle(page, 900)

        if not await select_jornada(page, label):
            log(f"  [{n}/{len(labels)}] {label}: !! view never switched, skipping")
            continue

        rows = await read_nodes(page)
        if not rows:
            rows = await read_cells(page)
        hits = our_rows(rows)
        sig = norm(" ".join(hits[0])) if hits else ""
        if sig and sig in seen_results:
            log(f"  [{n}/{len(labels)}] {label}: !! identical to"
                f" {seen_results[sig]}, skipping")
            continue
        if hits:
            seen_results[sig] = label
            out[label] = {"row": hits[0]}
            log(f"  [{n}/{len(labels)}] {label}: {' | '.join(hits[0])}")
        else:
            log(f"  [{n}/{len(labels)}] {label}: no result row")

    await dump(page, "results")
    return out


async def get_standings(page) -> dict:
    """Every group's table, so the page can page through them."""
    log("Loading standings page")
    await page.goto(STANDINGS_URL, wait_until="domcontentloaded")
    await settle(page, 2500)

    context = [await choose(page, TOURNAMENT) or TOURNAMENT,
               await choose(page, DIVISION) or DIVISION]
    await settle(page, 900)

    groups = await option_labels(page, "grupo") or [GROUP]
    log(f"  {len(groups)} groups to walk")

    tables = []
    for g in groups:
        await choose(page, DIVISION, quiet=True)
        if not await choose(page, g, quiet=True):
            continue
        await settle(page, 900)
        headers, rows = parse_table(await read_cells(page))
        if rows:
            tables.append({"group": g, "headers": headers, "rows": rows})
            log(f"  {g}: {len(rows)} teams")

    await dump(page, "standings")
    if not tables:
        log("  !! no standings tables found")
        return {}
    return {"context": context, "tables": tables}


async def scrape():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 1600}, locale="es-PR"
        )
        page = await ctx.new_page()
        try:
            fixtures = await get_fixtures(page)

            # No point asking for a score that cannot exist yet.
            today = datetime.now(AST).date()
            played = {f["jornada"] for f in fixtures
                      if f["row"] and (fixture_date(f) or today) <= today}
            results = await get_results(page, played)
            for f in fixtures:
                f["result"] = results.get(f["jornada"], {})

            return fixtures, await get_standings(page)
        finally:
            await browser.close()


# ----------------------------------------------------------------- share text


def fixture_text(entry: dict, t: dict, label: str = "", conv=None, val=None) -> str:
    """One jornada, short enough to read in a chat bubble."""
    conv = conv or (lambda x: x)
    val = val or conv
    lines = [label or conv(entry["jornada"])]
    if not entry.get("verified", True):
        lines.append(f"({t['unverified']})")
    if not entry.get("row"):
        return "\n".join(lines + [t["none"]])

    for head, value in fixture_rows(entry):
        head, value = conv(head), val(value)
        lines.append(f"{head}: {value}" if head else value)
    summary = result_summary(entry.get("result") or {})
    if summary:
        lines.append(f"{t['result']}: {summary}")
    return "\n".join(lines)


def standings_text(table: dict, t: dict, conv=None) -> str:
    """Rank and club name only. No column alignment to survive the trip."""
    conv = conv or (lambda x: x)
    if not table:
        return f"{t['table']}\n{t['notable']}"
    out = [f"{t['table']} ({conv(table['group'])})"]
    for n, r in enumerate(table["rows"], 1):
        rank = r[0].strip() if r and re.fullmatch(r"\d+", r[0].strip()) else str(n)
        out.append(f"{rank}. {team_name(r)}")
    return "\n".join(out)


# ----------------------------------------------------------------- page build

CSS = """
  :root {
    --bg:#f4f6f8; --fg:#10202e; --muted:#5d7080; --card:#fff;
    --line:#eef1f4; --mine:#eaf1fd; --chip:#eef1f4; --chiphi:#dfe5ea;
    --chipfg:#10202e; --btn:#10202e; --wa:#1f9d5b; --sms:#2f6fed;
    --warnfg:#8a4b00; --warnbg:#fdf1e0; --good:#15794a; --bad:#b4331f;
    --shadow:0 1px 3px rgba(16,32,46,.10);
  }
  :root[data-theme=dark],
  :root:not([data-theme=light]) {
    color-scheme: light dark;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme=light]) {
      --bg:#0b1b2b; --fg:#e8eef4; --muted:#90a6b8; --card:#14293c;
      --line:#1e3752; --mine:#1d3f63; --chip:#1e3752; --chiphi:#2a4a6b;
      --chipfg:#e8eef4; --btn:#2f6fed; --warnfg:#ffcf93; --warnbg:#3a2a12;
      --good:#5bd39a; --bad:#ff8f7a; --shadow:none;
    }
  }
  :root[data-theme=dark] {
    --bg:#0b1b2b; --fg:#e8eef4; --muted:#90a6b8; --card:#14293c;
    --line:#1e3752; --mine:#1d3f63; --chip:#1e3752; --chiphi:#2a4a6b;
    --chipfg:#e8eef4; --btn:#2f6fed; --warnfg:#ffcf93; --warnbg:#3a2a12;
    --good:#5bd39a; --bad:#ff8f7a; --shadow:none;
  }

  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--bg); color:var(--fg);
    font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
    padding:24px 16px 48px;
    padding-left:max(16px, env(safe-area-inset-left));
    padding-right:max(16px, env(safe-area-inset-right));
    padding-top:max(24px, env(safe-area-inset-top));
    padding-bottom:max(48px, env(safe-area-inset-bottom));
    -webkit-text-size-adjust:100%;
  }
  main { max-width:560px; margin:0 auto; }

  .top { display:flex; align-items:center; gap:8px; }
  h1 { flex:1; min-width:0; font-size:22px; margin:0; letter-spacing:-.01em; }
  .subtitle, h2, .sharelabel {
    font-size:12px; letter-spacing:.08em; text-transform:uppercase;
    color:var(--muted); font-weight:600;
  }
  .subtitle { margin:0; }
  .stamp { color:var(--muted); font-size:12.5px; line-height:1.4; margin:2px 0 0; }
  .iconbtn {
    flex:0 0 auto; width:38px; height:38px; padding:0; margin:0; border:0;
    border-radius:10px; background:var(--chip); color:var(--chipfg);
    cursor:pointer; -webkit-appearance:none; appearance:none;
    display:flex; align-items:center; justify-content:center;
    font-family:inherit; font-size:12px; font-weight:700; letter-spacing:.04em;
  }
  .iconbtn svg { display:block; width:18px; height:18px; }

  section {
    background:var(--card); border-radius:14px; padding:14px 16px;
    margin-bottom:16px; box-shadow:var(--shadow);
  }
  section:first-of-type { margin-top:20px; }
  h2 { margin:0 0 4px; }
  .sub { color:var(--muted); font-size:12.5px; line-height:1.4; margin:0 0 12px; }

  .nav { display:flex; align-items:center; gap:10px; margin-bottom:12px; }
  .nav button {
    flex:0 0 40px; height:40px; padding:0; border:0; border-radius:10px;
    background:var(--chip); color:var(--chipfg); font-size:20px;
    cursor:pointer; -webkit-appearance:none; appearance:none;
    display:flex; align-items:center; justify-content:center;
  }
  .nav button:disabled { opacity:.32; cursor:default; }
  .nav .label { flex:1; text-align:center; font-weight:600; font-size:14px; }

  .row { display:flex; gap:12px; padding:8px 0; border-top:1px solid var(--line); }
  .row:first-child { border-top:0; }
  .k {
    flex:0 0 36%; color:var(--muted); font-size:12px; font-weight:600;
    text-transform:uppercase; letter-spacing:.04em; padding-top:2px;
  }
  .v { flex:1; font-size:14px; font-variant-numeric:tabular-nums; }
  .score { font-weight:700; font-size:15px; }
  .empty { color:var(--muted); margin:8px 0; }
  .warn {
    color:var(--warnfg); background:var(--warnbg); border-radius:8px;
    padding:8px 10px; font-size:13px; margin:0 0 10px;
  }

  .scroll { overflow-x:auto; -webkit-overflow-scrolling:touch; }
  table { border-collapse:collapse; width:100%; font-size:14px; }
  th, td {
    padding:8px 7px; text-align:right; white-space:nowrap;
    font-variant-numeric:tabular-nums; border-top:1px solid var(--line);
  }
  th {
    color:var(--muted); font-weight:600; font-size:12px; border-top:0;
    text-transform:uppercase; letter-spacing:.04em;
  }
  th:nth-child(2), td:nth-child(2) {
    text-align:left; white-space:normal; min-width:150px;
  }
  tr.mine td { background:var(--mine); font-weight:600; }
  .pos { color:var(--good); }
  .neg { color:var(--bad); }

  #card, #gtable { animation:fade .18s ease; }
  @keyframes fade { from { opacity:0; } to { opacity:1; } }
  @media (prefers-reduced-motion: reduce) {
    #card, #gtable { animation:none; }
    .iconbtn, .nav button, .actions a, footer a { transition:none; }
  }

  .sharelabel { margin:22px 0 10px; }
  .actions { display:flex; gap:8px; margin-bottom:16px; }
  .actions a {
    flex:1 1 0; min-width:0; text-align:center; text-decoration:none;
    background:var(--btn); color:#fff; border-radius:12px; padding:14px 6px;
    font-size:15px; font-weight:500; white-space:nowrap; overflow:hidden;
    text-overflow:ellipsis;
  }
  .actions a:active { opacity:.75; }
  .actions .wa { background:var(--wa); }
  .actions .sms { background:var(--sms); }

  footer {
    text-align:center; font-size:12.5px; line-height:1.5;
    color:var(--muted); margin-top:26px;
  }
  footer p { margin:0; }
  footer .subtitle { letter-spacing:.07em; }
  footer a { color:var(--muted); display:inline-block; margin-top:8px; }

  .iconbtn, .nav button, .actions a, footer a { transition:all .15s ease; }
  @media (hover: hover) {
    .iconbtn:hover, .nav button:not(:disabled):hover { background:var(--chiphi); }
    .actions a:hover { filter:brightness(1.12); }
    footer a:hover { color:var(--fg); }
    tr.mine:hover td, tbody tr:hover td { background:var(--line); }
    tr.mine:hover td { background:var(--mine); }
  }
"""

JS = """
const D = __DATA__;
let i = D.start, g = D.gStart, lang = D.lang;
// Track what was last painted so only the section that changed re-animates.
let lastI = null, lastG = null, lastLang = null;

const $ = id => document.getElementById(id);
const card = $('card'), jlabel = $('jlabel'), prev = $('prev'), next = $('next');
const gtable = $('gtable'), glabel = $('glabel');
const gprev = $('gprev'), gnext = $('gnext');
const share = $('share'), wa = $('wa'), sms = $('sms');
const theme = $('theme'), langBtn = $('lang');

// iOS and Android disagree on the sms: separator.
const SEP = /iPhone|iPad|iPod|Macintosh/.test(navigator.userAgent) ? '&' : '?';

// Inline SVG rather than sun and moon glyphs, whose metrics vary by font.
const ICON = {
  sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
     + ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
     + '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4'
     + 'M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
      + ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>'
};

function store(key, value) { try { localStorage.setItem(key, value); } catch (e) {} }
function recall(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

function isDark() {
  const set = document.documentElement.getAttribute('data-theme');
  if (set) return set === 'dark';
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

function replay(el) {
  el.style.animation = 'none';
  void el.offsetWidth;
  el.style.animation = '';
}

function render() {
  const L = D[lang];

  $('matchTitle').textContent = L.matchTitle;
  $('tableTitle').textContent = L.tableTitle;
  $('fxSub').textContent = L.fxSub;
  $('stSub').textContent = L.stSub;
  $('shareVia').textContent = L.shareVia;
  $('stamp').textContent = L.stamp;
  wa.textContent = L.btnWa;
  sms.textContent = L.btnSms;
  share.textContent = L.btnMail;

  const first = lastLang === null;
  const langChanged = lang !== lastLang;

  if (langChanged || i !== lastI) {
    jlabel.textContent = L.labels[i] || '';
    card.innerHTML = L.cards[i] || '';
    if (!first) replay(card);
  }
  prev.disabled = i <= 0;
  next.disabled = i >= L.labels.length - 1;

  if (langChanged || g !== lastG) {
    glabel.textContent = L.gLabels[g] || '';
    gtable.innerHTML = L.gTables[g] || '';
    if (!first) replay(gtable);
  }
  gprev.disabled = g <= 0;
  gnext.disabled = g >= L.gLabels.length - 1;

  lastI = i; lastG = g; lastLang = lang;

  let body = L.head + '\\n' + L.texts[i] + '\\n\\n' + L.gTexts[g] + '\\n';
  if (D.url) body += '\\n' + D.url + '\\n';
  const subject = L.labels[i] ? D.nick + ' - ' + L.labels[i] : D.nick;
  share.href = 'mailto:?subject=' + encodeURIComponent(subject)
             + '&body=' + encodeURIComponent(body);
  wa.href = 'https://wa.me/?text=' + encodeURIComponent(body);
  sms.href = 'sms:' + SEP + 'body=' + encodeURIComponent(body);

  document.documentElement.setAttribute('lang', lang);
  langBtn.textContent = lang === 'es' ? 'EN' : 'ES';
  langBtn.setAttribute('aria-label',
    lang === 'es' ? 'Switch to English' : 'Cambiar a espanol');
  theme.innerHTML = isDark() ? ICON.sun : ICON.moon;
}

const savedTheme = recall('theme');
if (savedTheme) document.documentElement.setAttribute('data-theme', savedTheme);
const savedLang = recall('lang');
if (savedLang === 'es' || savedLang === 'en') lang = savedLang;

prev.onclick = () => { if (i > 0) { i--; render(); } };
next.onclick = () => { if (i < D[lang].labels.length - 1) { i++; render(); } };
gprev.onclick = () => { if (g > 0) { g--; render(); } };
gnext.onclick = () => { if (g < D[lang].gLabels.length - 1) { g++; render(); } };

theme.onclick = () => {
  const mode = isDark() ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', mode);
  store('theme', mode);
  render();
};
langBtn.onclick = () => {
  lang = lang === 'es' ? 'en' : 'es';
  store('lang', lang);
  render();
};

render();
"""


def upcoming_index(fixtures: list):
    """The earliest fixture dated today or later."""
    today = datetime.now(AST).date()
    return next(
        (i for i, f in enumerate(fixtures)
         if f["row"] and (fixture_date(f) or today - timedelta(days=1)) >= today),
        None,
    )


def our_group(standings: dict) -> dict:
    """The group table containing our team, else the first one."""
    tables = standings.get("tables", [])
    for tb in tables:
        if any(any(matches(c, TEAM) for c in r) for r in tb["rows"]):
            return tb
    return tables[0] if tables else {}


def build_variant(fixtures: list, standings: dict, lang: str) -> dict:
    """Everything the page shows, rendered in one language."""
    t = ES if lang == "es" else EN
    conv = (lambda x: x) if lang == "es" else to_en   # headers, labels
    val = value_conv(lang)                            # scraped cell values

    upcoming = upcoming_index(fixtures)
    labels = [
        f"{conv(f['jornada'])} ({t['next']})" if i == upcoming else conv(f["jornada"])
        for i, f in enumerate(fixtures)
    ]

    cards = []
    for f in fixtures:
        body = "" if f["verified"] else f"<p class=warn>{esc(t['unverified'])}</p>"
        if f["row"]:
            body += "".join(
                f"<div class=row><span class=k>{esc(conv(h))}</span>"
                f"<span class=v>{esc(val(v))}</span></div>"
                for h, v in fixture_rows(f)
            )
            summary = result_summary(f["result"])
            if summary:
                body += (f"<div class=row><span class=k>{esc(t['result'])}</span>"
                         f"<span class='v score'>{esc(summary)}</span></div>")
        else:
            body += f"<p class=empty>{esc(t['none'])}</p>"
        cards.append(body)

    gLabels, gTables, gTexts = [], [], []
    for tb in standings.get("tables", []):
        head = "".join(f"<th>{esc(conv(h))}</th>" for h in tb["headers"])
        # Sign the goal-difference column, which reads faster than +/- alone.
        diff_at = next(
            (i for i, h in enumerate(tb["headers"]) if norm(h) in ("dif", "gd")),
            None,
        )

        def cell(text, i):
            body = esc(val(text))
            if i == diff_at:
                n = text.strip().lstrip("+")
                if re.fullmatch(r"-?\d+", n):
                    if int(n) > 0:
                        return f"<td><span class=pos>{body}</span></td>"
                    if int(n) < 0:
                        return f"<td><span class=neg>{body}</span></td>"
            return f"<td>{body}</td>"

        rows = "".join(
            f"<tr{' class=mine' if any(matches(c, TEAM) for c in r) else ''}>"
            + "".join(cell(c, i) for i, c in enumerate(r)) + "</tr>"
            for r in tb["rows"]
        )
        gLabels.append(conv(tb["group"]))
        gTables.append(f"<div class=scroll><table><thead><tr>{head}</tr></thead>"
                       f"<tbody>{rows}</tbody></table></div>")
        gTexts.append(standings_text(tb, t, conv))
    if not gLabels:
        gLabels = [""]
        gTables = [f"<p class=empty>{esc(t['notable'])}</p>"]
        gTexts = [f"{t['table']}\n{t['notable']}"]

    seen = next((f for f in fixtures if f["verified"]), {})
    context = [c for c in standings.get("context", []) if c]
    when = stamp(lang)

    return {
        "labels": labels,
        "cards": cards,
        "texts": [fixture_text(f, t, labels[i], conv, val)
                  for i, f in enumerate(fixtures)],
        "gLabels": gLabels,
        "gTables": gTables,
        "gTexts": gTexts,
        "fxSub": " · ".join(x for x in (conv(seen.get("division") or DIVISION),
                                        conv(seen.get("group") or GROUP)) if x),
        "stSub": conv(context[1] if len(context) > 1 else DIVISION),
        "matchTitle": t["match"],
        "tableTitle": t["table"],
        "shareVia": t["shareVia"],
        "btnWa": t["wa"],
        "btnSms": t["sms"],
        "btnMail": t["share"],
        "stamp": when,
        "head": f"{NICK}\n",
    }


def write_page(fixtures: list, standings: dict, fingerprint: str) -> None:
    default = "es" if LANG.startswith("es") else "en"

    start = upcoming_index(fixtures)
    if start is None:
        start = max((i for i, f in enumerate(fixtures) if f["row"]),
                    default=len(fixtures) - 1)

    gStart = 0
    for n, tb in enumerate(standings.get("tables", [])):
        if any(any(matches(c, TEAM) for c in r) for r in tb["rows"]):
            gStart = n  # open on the group our team plays in

    data = {
        "es": build_variant(fixtures, standings, "es"),
        "en": build_variant(fixtures, standings, "en"),
        "lang": default,
        "start": start,
        "gStart": gStart,
        "url": page_url(),
        "nick": NICK,
        "subtitle": SUBTITLE,
    }
    script = JS.replace("__DATA__", json.dumps(data, ensure_ascii=False))

    # First paint comes from the HTML, not from JavaScript, so there is no
    # blank flash on a slow connection. render() then overwrites it identically.
    D = data[default]
    url = data["url"]
    body = f"{D['head']}\n{D['texts'][start]}\n\n{D['gTexts'][gStart]}\n"
    if url:
        body += f"\n{url}\n"
    subject = f"{NICK} - {D['labels'][start]}" if D["labels"] else NICK
    mailto = "mailto:?subject=" + quote(subject) + "&body=" + quote(body)
    whats = "https://wa.me/?text=" + quote(body)
    text_link = "sms:?body=" + quote(body)

    blurb = " · ".join(
        x for x in D["texts"][start].split("\n")[:4] if x.strip()
    )[:180]
    icon = f"{url}/icon.png" if url else "icon.png"

    PAGE.write_text(f"""<!doctype html>
<html lang="{default}" data-digest="{fingerprint}">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="{esc(NICK)}">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<link rel="apple-touch-icon" href="icon.png">
<link rel="icon" href="icon.png">
<meta name="theme-color" content="#f4f6f8" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0b1b2b" media="(prefers-color-scheme: dark)">
<meta name="description" content="{esc(blurb)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{esc(SUBTITLE)}">
<meta property="og:title" content="{esc(NICK)}">
<meta property="og:description" content="{esc(blurb)}">
<meta property="og:image" content="{esc(icon)}">
<meta property="og:image:width" content="180">
<meta property="og:image:height" content="180">
{f'<meta property="og:url" content="{esc(url)}">' if url else ''}
<meta name="twitter:card" content="summary">
<title>{esc(NICK)}</title>
<style>{CSS}</style>
<main>
  <div class=top>
    <h1>{esc(NICK)}</h1>
    <button id=lang class=iconbtn>{'EN' if default == 'es' else 'ES'}</button>
    <button id=theme class=iconbtn aria-label="theme"></button>
  </div>

  <section>
    <h2 id=matchTitle>{esc(D['matchTitle'])}</h2>
    <p class=sub id=fxSub>{esc(D['fxSub'])}</p>
    <div class=nav>
      <button id=prev aria-label="previous"{' disabled' if start <= 0 else ''}>&lsaquo;</button>
      <span class=label id=jlabel>{esc(D['labels'][start] if D['labels'] else '')}</span>
      <button id=next aria-label="next"{' disabled' if start >= len(D['labels']) - 1 else ''}>&rsaquo;</button>
    </div>
    <div id=card>{D['cards'][start] if D['cards'] else ''}</div>
  </section>

  <section>
    <h2 id=tableTitle>{esc(D['tableTitle'])}</h2>
    <p class=sub id=stSub>{esc(D['stSub'])}</p>
    <div class=nav>
      <button id=gprev aria-label="previous group"{' disabled' if gStart <= 0 else ''}>&lsaquo;</button>
      <span class=label id=glabel>{esc(D['gLabels'][gStart])}</span>
      <button id=gnext aria-label="next group"{' disabled' if gStart >= len(D['gLabels']) - 1 else ''}>&rsaquo;</button>
    </div>
    <div id=gtable>{D['gTables'][gStart]}</div>
  </section>

  <p class=sharelabel id=shareVia>{esc(D['shareVia'])}</p>
  <div class=actions>
    <a class=wa id=wa href="{esc(whats)}">{esc(D['btnWa'])}</a>
    <a class=sms id=sms href="{esc(text_link)}">{esc(D['btnSms'])}</a>
    <a id=share href="{esc(mailto)}">{esc(D['btnMail'])}</a>
  </div>

  <footer>
    <p class=subtitle>{esc(SUBTITLE)}</p>
    <p class=stamp id=stamp>{esc(D['stamp'])}</p>
    <a href="https://ystpr.com/itinerario">ystpr.com</a>
  </footer>
</main>
<script>{script}</script>
""", encoding="utf-8")
    log("Wrote index.html")


# -------------------------------------------------------------------- notify


def snapshot(fixtures: list, standings: dict) -> dict:
    """The two things worth emailing about."""
    table = our_group(standings)
    return {
        "jornadas": [f["jornada"] for f in fixtures],
        "group": table.get("group", ""),
        "ranking": [team_name(r) for r in table.get("rows", [])],
    }


def load_state() -> dict:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def send_email(subject: str, body: str) -> bool:
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    to = [a.strip() for a in os.environ.get("EMAIL_TO", "").split(",") if a.strip()]
    if not (user and password and to):
        log("  email not configured, skipping")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    try:
        with smtplib.SMTP(os.environ.get("SMTP_HOST", "smtp.gmail.com"),
                          int(os.environ.get("SMTP_PORT", 587)), timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
    except Exception as e:
        log(f"  !! email failed: {e}")
        return False
    log(f"  emailed {len(to)} recipient(s): {subject}")
    return True


def notify(fixtures: list, standings: dict) -> None:
    """Email when a jornada appears or our group's order changes.

    Sent in the build-time language, since email has no toggle to click.
    """
    t = ES if LANG.startswith("es") else EN
    conv = (lambda x: x) if LANG.startswith("es") else to_en
    val = value_conv(LANG)

    now, before = snapshot(fixtures, standings), load_state()
    STATE.write_text(json.dumps(now, indent=2, ensure_ascii=False), encoding="utf-8")

    if not before:
        log("First run, recording state without emailing")
        return

    new = [j for j in now["jornadas"] if j not in before.get("jornadas", [])]
    moved = (bool(now["ranking"]) and bool(before.get("ranking"))
             and now["ranking"] != before["ranking"])
    if not new and not moved:
        log("No new jornadas and no ranking change")
        return

    subject = " / ".join(x for x in ((t["mailNew"] if new else ""),
                                     (t["mailRank"] if moved else "")) if x)
    lines = [NICK, stamp(), ""]
    if new:
        lines += [f"{t['newJornadas']}: {', '.join(conv(j) for j in new)}", ""]

    i = upcoming_index(fixtures)
    if i is not None:
        label = f"{conv(fixtures[i]['jornada'])} ({t['next']})"
        lines += [fixture_text(fixtures[i], t, label, conv, val), ""]
    if moved:
        lines += [standings_text(our_group(standings), t, conv), ""]
    url = page_url()
    if url:
        lines.append(url)

    send_email(f"{NICK} - {subject}", "\n".join(lines))


# ---------------------------------------------------------------------- main


def digest(fixtures: list, standings: dict) -> str:
    """Fingerprint of the data only. Deliberately excludes the timestamp."""
    blob = json.dumps({"f": fixtures, "s": standings, "n": NICK,
                       "l": LANG, "sub": SUBTITLE},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def unchanged(fingerprint: str) -> bool:
    if not PAGE.exists():
        return False
    try:
        return f'data-digest="{fingerprint}"' in PAGE.read_text(encoding="utf-8")
    except Exception:
        return False


def main() -> None:
    fixtures, standings = asyncio.run(scrape())
    if not fixtures and not standings:
        log("Nothing extracted. Check the debug/ artifacts.")
        sys.exit(1)

    # Check before writing, since write_page overwrites the fingerprint.
    fingerprint = digest(fixtures, standings)
    changed = not unchanged(fingerprint)

    # The page is rebuilt every run so the Actualizado line is always current,
    # even when nothing else moved.
    write_page(fixtures, standings, fingerprint)
    log("Data changed." if changed else "Timestamp only, no data change.")

    notify(fixtures, standings)

    # Lets the workflow label the commit.
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"data_changed={'true' if changed else 'false'}\n")


if __name__ == "__main__":
    main()
