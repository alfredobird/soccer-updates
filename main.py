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
DIVISION = os.environ.get("DIVISION", "TC 2016 U10 DIVISION 2 - Fase 1")
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
    """Lowercase, strip accents, collapse whitespace. For fuzzy text matching."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


async def settle(page, ms: int = 900) -> None:
    """Wait for the app to finish reacting to a selection."""
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(ms)


async def choose(page, wanted: str, latest: bool = False) -> str | None:
    """
    Pick an option matching `wanted` from whatever control holds it.

    Tries native <select>, then ARIA comboboxes, then plain clickable text.
    With latest=True, picks the highest-numbered match (used for "jornada").
    """
    pat = re.compile(re.escape(wanted), re.I)

    def rank(text: str) -> int:
        nums = re.findall(r"\d+", text)
        return int(nums[-1]) if nums else -1

    def best(items: list[str]) -> str:
        return max(items, key=rank) if latest else items[0]

    # 1. native <select>
    selects = page.locator("select")
    for i in range(await selects.count()):
        sel = selects.nth(i)
        try:
            opts = await sel.locator("option").all_inner_texts()
        except Exception:
            continue
        hits = [o for o in opts if norm(wanted) in norm(o)]
        if hits:
            pick = best(hits)
            await sel.select_option(label=pick)
            await settle(page)
            log(f"  selected '{pick.strip()}' from a dropdown")
            return pick.strip()

    # 2. custom dropdown: open the trigger, then click the option
    triggers = page.get_by_role("combobox")
    for i in range(await triggers.count()):
        try:
            await triggers.nth(i).click(timeout=2500)
        except Exception:
            continue
        await page.wait_for_timeout(400)
        opts = page.get_by_role("option", name=pat)
        if await opts.count():
            texts = await opts.all_inner_texts()
            pick = best([t for t in texts if t.strip()])
            await page.get_by_role("option", name=re.compile(re.escape(pick.strip()), re.I)).first.click()
            await settle(page)
            log(f"  clicked '{pick.strip()}' in a custom dropdown")
            return pick.strip()
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    # 3. plain clickable text on the page
    loc = page.get_by_text(pat)
    count = await loc.count()
    if count:
        texts = await loc.all_inner_texts()
        # Prefer the shortest match: that's the label itself, not a wrapper div.
        candidates = sorted(
            [(t, i) for i, t in enumerate(texts) if t.strip()],
            key=lambda x: (rank(x[0]) * -1, len(x[0])) if latest else (len(x[0]),),
        )
        if candidates:
            _, idx = candidates[0]
            try:
                await loc.nth(idx).click(timeout=2500)
                await settle(page)
                log(f"  clicked text '{texts[idx].strip()[:60]}'")
                return texts[idx].strip()
            except Exception:
                pass

    log(f"  !! could not find a control for '{wanted}'")
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
