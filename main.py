#!/usr/bin/env python3
"""
Scrapes ystpr.com for one team's next match and standings, then texts a summary.

Sends only when the content has changed since the last run.
Run with no Twilio credentials set to preview the message without sending.
"""

import asyncio
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests
import smtplib
from email.message import EmailMessage
from playwright.async_api import async_playwright

# ---------------------------------------------------------------- config

TEAM = os.environ.get("TEAM", "SURF GUAYNABO U10 2016 Black")
TOURNAMENT = os.environ.get("TOURNAMENT", "CHAMPIONSHIP")
DIVISION = os.environ.get("DIVISION", "TC 2026 U10 DIVISION 2 - Fase 1")
GROUP = os.environ.get("GROUP", "Grupo A")

SCHEDULE_URL = "https://ystpr.com/itinerario"
STANDINGS_URL = "https://ystpr.com/posiciones"

# Short name used in the text message.
NICK = os.environ.get("NICK", "Surf Guaynabo U10 Black")
# "es" or "en"
LANG = os.environ.get("LANG_OUT", "es")

STATE = Path("state.json")
DEBUG = Path("debug")

# ---------------------------------------------------------------- helpers


def norm(s: str) -> str:
    """Lowercase, strip accents, drop all punctuation. For fuzzy text matching."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def matches(option: str, wanted: str) -> bool:
    """True when every word of `wanted` appears in `option`.

    Deliberately punctuation-blind, so 'DIVISION 2 - Fase 1' still matches
    'DIVISION 2 - Fase 1' rendered with an en dash, extra spaces, or an accent.
    """
    have = set(norm(option).split())
    return bool(have) and set(norm(wanted).split()) <= have


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


async def settle(page, ms: int = 900) -> None:
    """Wait for the app to finish reacting to a selection."""
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(ms)


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
    """
    Pick an option matching `wanted` from whatever control holds it.

    Matching ignores punctuation, accents and spacing, so only the words
    have to line up. With latest=True, picks the highest-numbered match.
    """

    def rank(text: str) -> int:
        nums = re.findall(r"\d+", text)
        return int(nums[-1]) if nums else -1

    def best(items: list[tuple[int, str]]) -> tuple[int, str]:
        return max(items, key=lambda x: rank(x[1])) if latest else items[0]

    # 1. native <select>
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
            await sel.select_option(index=idx)  # by index, not by label text
            await settle(page)
            log(f"  selected '{text.strip()}'")
            return text.strip()

    # 2. custom dropdown: open the trigger, then click the option
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

    # 3. plain clickable text on the page
    try:
        body_bits = page.locator("li, a, button, td, div[role], span")
        texts = await body_bits.all_inner_texts()
    except Exception:
        texts = []
    hits = [(j, t) for j, t in enumerate(texts) if t.strip() and matches(t, wanted)]
    if hits:
        # Shortest match is the label itself rather than a wrapper element.
        hits.sort(key=lambda x: (-rank(x[1]), len(x[1])) if latest else (len(x[1]),))
        idx, text = hits[0]
        try:
            await body_bits.nth(idx).click(timeout=2500)
            await settle(page)
            log(f"  clicked text '{text.strip()[:60]}'")
            return text.strip()
        except Exception:
            pass

    log(f"  !! no control matched '{wanted}'. Options visible right now:")
    for line in await list_controls(page):
        log(f"       {line}")
    return None


async def read_rows(page, needle: str | None = None) -> list[str]:
    """Return table rows as flat text, filtered to those containing `needle`."""
    rows: list[str] = []
    try:
        rows = await page.locator("table tr").all_inner_texts()
    except Exception:
        pass
    rows = [" ".join(r.split()) for r in rows if r.strip()]

    if not rows:  # no <table>, fall back to visible page text
        body = await page.inner_text("body")
        rows = [" ".join(l.split()) for l in body.splitlines() if l.strip()]

    if needle:
        rows = [r for r in rows if norm(needle) in norm(r)]
    return rows


async def dump(page, name: str) -> None:
    """Save the rendered page so failures are diagnosable after the fact."""
    DEBUG.mkdir(exist_ok=True)
    try:
        await page.screenshot(path=str(DEBUG / f"{name}.png"), full_page=True)
        (DEBUG / f"{name}.txt").write_text(await page.inner_text("body"))
    except Exception as e:
        log(f"  (could not save debug output: {e})")


# ---------------------------------------------------------------- scraping


async def get_schedule(page) -> list[str]:
    log("Loading schedule page")
    await page.goto(SCHEDULE_URL, wait_until="domcontentloaded")
    await settle(page, 2500)

    await choose(page, TOURNAMENT)
    await choose(page, "jornada", latest=True)
    await choose(page, DIVISION)
    await choose(page, GROUP)
    await settle(page, 1500)

    await dump(page, "schedule")
    rows = await read_rows(page, needle="SURF GUAYNABO")
    # Narrow to our exact team when the group has more than one Surf side.
    exact = [r for r in rows if norm(TEAM) in norm(r)]
    return exact or rows


async def get_standings(page) -> list[str]:
    log("Loading standings page")
    await page.goto(STANDINGS_URL, wait_until="domcontentloaded")
    await settle(page, 2500)

    await choose(page, TOURNAMENT)
    await choose(page, DIVISION)
    await choose(page, GROUP)
    await settle(page, 1500)

    await dump(page, "standings")
    all_rows = await read_rows(page)
    ours = [r for r in all_rows if norm(TEAM) in norm(r)]
    return ours or [r for r in all_rows if norm("SURF GUAYNABO") in norm(r)]


async def scrape() -> tuple[list[str], list[str]]:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(
            viewport={"width": 1400, "height": 1600},
            locale="es-PR",
        )
        page = await ctx.new_page()
        try:
            schedule = await get_schedule(page)
            standings = await get_standings(page)
        finally:
            await browser.close()
    return schedule, standings


# ---------------------------------------------------------------- output


def build_message(schedule: list[str], standings: list[str]) -> str:
    es = LANG.startswith("es")
    t_match = "Próximo partido" if es else "Next match"
    t_table = "Tabla" if es else "Standings"
    t_none = "Sin partido publicado todavía." if es else "No match posted yet."
    t_notable = "Tabla no disponible." if es else "Standings unavailable."

    parts = [f"⚽ {NICK}", "", f"{t_match}:"]
    parts += [f"  {r}" for r in schedule[:3]] or [f"  {t_none}"]
    parts += ["", f"{t_table}:"]
    parts += [f"  {r}" for r in standings[:1]] or [f"  {t_notable}"]
    return "\n".join(parts)


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
    msg.set_content(message)

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
    """Use whichever delivery method has been configured."""
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        send_telegram(message)
    elif os.environ.get("SMTP_USER") and os.environ.get("EMAIL_TO"):
        send_email(message)
    else:
        log("No delivery method configured, printing instead:")
        print("\n" + message + "\n")


def main() -> None:
    schedule, standings = asyncio.run(scrape())
    log(f"Found {len(schedule)} match row(s), {len(standings)} standings row(s)")

    if not schedule and not standings:
        log("Nothing extracted. Check the debug/ artifacts before touching selectors.")
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
