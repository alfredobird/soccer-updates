#!/usr/bin/env python3
"""
Scrapes ystpr.com for one team's next match and league position, then emails
a summary. Sends only when something has changed since the last run.

Run with no delivery credentials set to preview the message instead of sending.
"""

import asyncio
import json
import os
import re
import smtplib
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import requests
from playwright.async_api import async_playwright

# ---------------------------------------------------------------- config

TEAM = os.environ.get("TEAM", "SURF GUAYNABO U10 2016 Black")
TOURNAMENT = os.environ.get("TOURNAMENT", "CHAMPIONSHIP")
DIVISION = os.environ.get("DIVISION", "TC 2026 U10 DIVISION 2 - Fase 1")
GROUP = os.environ.get("GROUP", "Grupo A")

SCHEDULE_URL = "https://ystpr.com/itinerario"
STANDINGS_URL = "https://ystpr.com/posiciones"

NICK = os.environ.get("NICK", "Surf Guaynabo U10 Black")
LANG = os.environ.get("LANG_OUT", "es")  # "es" or "en"

STATE = Path("state.json")
DEBUG = Path("debug")

# ---------------------------------------------------------------- helpers


def norm(s: str) -> str:
    """Lowercase, strip accents, drop all punctuation. For fuzzy matching."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def matches(option: str, wanted: str) -> bool:
    """True when every word of `wanted` appears in `option`.

    Punctuation-blind, so an en dash or an accented DIVISION still matches.
    """
    have = set(norm(option).split())
    return bool(have) and set(norm(wanted).split()) <= have


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


async def settle(page, ms: int = 900) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(ms)


# ---------------------------------------------------------------- selection


async def list_controls(page) -> list[str]:
    """Every option text currently on the page, for diagnostics."""
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
    return found


async def choose(page, wanted: str, latest: bool = False) -> str | None:
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
            log(f"  selected '{text.strip()}'")
            return text.strip()

    triggers = page.get_by_role("combobox")
    for i in range(await triggers.count()):
        try:
            await triggers.nth(i).click(timeout=2500)
        except Exception:
            continue
        await page.wait_for_timeout(500)
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
            log(f"  clicked '{text.strip()}'")
            return text.strip()
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    try:
        bits = page.locator("li, a, button, td, div[role], span")
        texts = await bits.all_inner_texts()
    except Exception:
        texts = []
    hits = [(j, t) for j, t in enumerate(texts) if t.strip() and matches(t, wanted)]
    if hits:
        hits.sort(key=lambda x: (-rank(x[1]), len(x[1])) if latest else (len(x[1]),))
        idx, text = hits[0]
        try:
            await bits.nth(idx).click(timeout=2500)
            await settle(page)
            log(f"  clicked text '{text.strip()[:60]}'")
            return text.strip()
        except Exception:
            pass

    log(f"  !! no control matched '{wanted}'. Options visible right now:")
    for line in await list_controls(page):
        log(f"       {line}")
    return None


# ---------------------------------------------------------------- reading


async def read_table(page, limit: int = 300) -> list[list[str]]:
    """Every table row as a list of cell strings. Structure preserved."""
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
    """The widest row above `data_row` that carries no digits: the headers."""
    try:
        cut = rows.index(data_row)
    except ValueError:
        cut = len(rows)
    candidates = [
        r
        for r in rows[:cut]
        if len(r) == len(data_row) and not any(re.search(r"\d", c) for c in r)
    ]
    return candidates[-1] if candidates else []


def find_rows(rows: list[list[str]], needle: str) -> list[list[str]]:
    """Rows mentioning `needle`, widest first, near-duplicates dropped."""
    hits = [r for r in rows if any(norm(needle) in norm(c) for c in r)]
    hits.sort(key=len, reverse=True)

    kept: list[list[str]] = []
    for r in hits:
        sig = norm(" ".join(r))
        if not any(sig in norm(" ".join(k)) for k in kept):
            kept.append(r)
    return kept


def label_pairs(headers: list[str], row: list[str]) -> list[tuple[str, str]]:
    """Zip headers onto cells, skipping blanks. Falls back to bare values."""
    pairs = []
    for i, value in enumerate(row):
        if not value.strip():
            continue
        head = headers[i].strip() if i < len(headers) else ""
        pairs.append((head, value.strip()))
    return pairs


async def dump(page, name: str) -> None:
    DEBUG.mkdir(exist_ok=True)
    try:
        await page.screenshot(path=str(DEBUG / f"{name}.png"), full_page=True)
        (DEBUG / f"{name}.txt").write_text(await page.inner_text("body"))
    except Exception as e:
        log(f"  (could not save debug output: {e})")


# ---------------------------------------------------------------- scraping


async def get_schedule(page) -> dict:
    log("Loading schedule page")
    await page.goto(SCHEDULE_URL, wait_until="domcontentloaded")
    await settle(page, 2500)

    await choose(page, TOURNAMENT)
    await choose(page, "jornada", latest=True)
    await choose(page, DIVISION)
    await choose(page, GROUP)
    await settle(page, 1500)
    await dump(page, "schedule")

    rows = await read_table(page)
    hits = find_rows(rows, TEAM) or find_rows(rows, "SURF GUAYNABO")
    if not hits:
        return {}
    row = hits[0]
    return {"headers": header_row(rows, row), "row": row}


async def get_standings(page) -> dict:
    log("Loading standings page")
    await page.goto(STANDINGS_URL, wait_until="domcontentloaded")
    await settle(page, 2500)

    await choose(page, TOURNAMENT)
    await choose(page, DIVISION)
    await choose(page, GROUP)
    await settle(page, 1500)
    await dump(page, "standings")

    rows = await read_table(page)
    hits = find_rows(rows, TEAM) or find_rows(rows, "SURF GUAYNABO")
    if not hits:
        return {}
    row = hits[0]
    return {"headers": header_row(rows, row), "row": row}


async def scrape() -> tuple[dict, dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 1600}, locale="es-PR"
        )
        page = await ctx.new_page()
        try:
            return await get_schedule(page), await get_standings(page)
        finally:
            await browser.close()


# ---------------------------------------------------------------- output

ES = {
    "match": "PRÓXIMO PARTIDO",
    "table": "TABLA DE POSICIONES",
    "no_match": "Sin partido publicado todavía.",
    "no_table": "Tabla no disponible.",
}
EN = {
    "match": "NEXT MATCH",
    "table": "STANDINGS",
    "no_match": "No match posted yet.",
    "no_table": "Standings unavailable.",
}


def section(title: str, block: dict, empty: str) -> list[str]:
    lines = [title, "-" * len(title)]
    if not block:
        return lines + [f"  {empty}", ""]

    pairs = label_pairs(block.get("headers", []), block.get("row", []))
    width = max((len(h) for h, _ in pairs if h), default=0)
    for head, value in pairs:
        lines.append(f"  {head.ljust(width)}  {value}" if head else f"  {value}")
    return lines + [""]


MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

# Puerto Rico is UTC-4 year round, so a fixed offset is exact here.
AST = timezone(timedelta(hours=-4))


def stamp() -> str:
    now = datetime.now(AST)
    hour = now.hour % 12 or 12
    if LANG.startswith("es"):
        ampm = "a. m." if now.hour < 12 else "p. m."
        fecha = f"{now.day} de {MESES[now.month - 1]} de {now.year}"
        return f"Actualizado: {fecha}, {hour}:{now.minute:02d} {ampm}"
    return f"As of: {now:%B} {now.day}, {now.year}, {hour}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}"


def build_message(schedule: dict, standings: dict) -> str:
    t = ES if LANG.startswith("es") else EN
    lines = [NICK, "=" * len(NICK), stamp(), ""]
    lines += section(t["match"], schedule, t["no_match"])
    lines += section(t["table"], standings, t["no_table"])
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- delivery


def send_telegram(message: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    for chat_id in os.environ["TELEGRAM_CHAT_ID"].split(","):
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id.strip(), "text": message},
            timeout=30,
        )
        if r.status_code >= 300:
            log(f"Telegram error {r.status_code}: {r.text[:300]}")
            sys.exit(1)
    log("Sent via Telegram")


def send_email(message: str) -> None:
    user = os.environ["SMTP_USER"]
    recipients = [a.strip() for a in os.environ["EMAIL_TO"].split(",") if a.strip()]

    msg = EmailMessage()
    msg["Subject"] = NICK
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    # Monospace keeps the aligned columns aligned in most mail clients.
    msg.set_content(message)
    msg.add_alternative(
        "<pre style=\"font-family:ui-monospace,Menlo,Consolas,monospace;"
        "font-size:14px;line-height:1.5\">"
        + message.replace("&", "&amp;").replace("<", "&lt;")
        + "</pre>",
        subtype="html",
    )

    with smtplib.SMTP(
        os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        int(os.environ.get("SMTP_PORT", 587)),
        timeout=30,
    ) as s:
        s.starttls()
        s.login(user, os.environ["SMTP_PASS"])
        s.send_message(msg)
    log(f"Sent via email to {len(recipients)} recipient(s)")


def send(message: str) -> None:
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        send_telegram(message)
    elif os.environ.get("SMTP_USER") and os.environ.get("EMAIL_TO"):
        send_email(message)
    else:
        log("No delivery method configured, printing instead:")
        print("\n" + message + "\n")


def main() -> None:
    schedule, standings = asyncio.run(scrape())
    if not schedule and not standings:
        log("Nothing extracted. Check the debug/ artifacts.")
        sys.exit(1)

    message = build_message(schedule, standings)
    current = {"schedule": schedule, "standings": standings}

    previous = {}
    if STATE.exists():
        try:
            previous = json.loads(STATE.read_text())
        except Exception:
            pass

    if previous.get("data") == current and "--force" not in sys.argv:
        log("No change since last run, nothing to send")
        print("\n" + message + "\n")
        return

    send(message)
    STATE.write_text(
        json.dumps(
            {"data": current, "sent_at": datetime.now(timezone.utc).isoformat()},
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
