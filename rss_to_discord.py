"""Postet neue Einträge eines RSS-Feeds per Webhook in einen Discord-Channel.

Bereits gepostete Einträge werden in posted.json gemerkt, damit nichts doppelt kommt.
Beim allerersten Lauf wird nur der neueste Eintrag gepostet (kein Fluten des Channels).
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

FEED_URL = os.environ.get("FEED_URL", "https://worldoftanks.eu/de/rss/news/")
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
STATE_FILE = Path(__file__).with_name("posted.json")
MAX_POSTS_PER_RUN = 10
MAX_REMEMBERED = 500
EMBED_COLOR = 0xB4141E

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


def fetch_feed():
    """Holt den Feed. Die WoT-Seite hat einen Bot-Schutz, der JavaScript verlangt,
    deshalb öffnen wir sie einmal in einem echten (unsichtbaren) Browser und laden
    den Feed danach mit dessen Cookies."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(locale="de-DE", user_agent=HEADERS["User-Agent"])
        page = ctx.new_page()
        page.goto(FEED_URL, wait_until="load", timeout=60000)
        for _ in range(12):
            if "please wait" not in page.title().lower():
                break
            page.wait_for_timeout(2500)
        data = ctx.request.get(FEED_URL, headers={"Accept": HEADERS["Accept"]}).body()
        browser.close()
    if b"<rss" not in data[:2000] and b"<feed" not in data[:2000]:
        sys.exit(
            "Der Feed hat kein RSS geliefert (vermutlich Bot-Schutz der Webseite).\n"
            f"Anfang der Antwort: {data[:300]!r}"
        )
    return ET.fromstring(data)


def strip_html(text):
    text = re.sub(r"<br\s*/?>|</p>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def first_image(item, description):
    for tag in ("enclosure", "media:content", "media:thumbnail"):
        el = item.find(tag, NS)
        if el is not None and el.get("url"):
            if tag != "enclosure" or el.get("type", "image").startswith("image"):
                return el.get("url")
    match = re.search(r'<img[^>]+src="([^"]+)"', description or "")
    return html.unescape(match.group(1)) if match else None


def parse_entries(root):
    entries = []
    for item in root.iter("item"):  # RSS 2.0
        link = (item.findtext("link") or "").strip()
        description = item.findtext("description") or item.findtext("content:encoded", namespaces=NS) or ""
        published = None
        if item.findtext("pubDate"):
            try:
                published = parsedate_to_datetime(item.findtext("pubDate")).isoformat()
            except (TypeError, ValueError):
                pass
        entries.append({
            "id": (item.findtext("guid") or link or item.findtext("title") or "").strip(),
            "title": strip_html(item.findtext("title")),
            "link": link,
            "summary": strip_html(description),
            "image": first_image(item, description),
            "published": published,
        })
    for entry in root.iter(f"{{{NS['atom']}}}entry"):  # Atom
        link_el = entry.find("atom:link", NS)
        link = link_el.get("href", "") if link_el is not None else ""
        summary = entry.findtext("atom:summary", namespaces=NS) or entry.findtext("atom:content", namespaces=NS) or ""
        entries.append({
            "id": (entry.findtext("atom:id", namespaces=NS) or link).strip(),
            "title": strip_html(entry.findtext("atom:title", namespaces=NS)),
            "link": link,
            "summary": strip_html(summary),
            "image": first_image(entry, summary),
            "published": entry.findtext("atom:updated", namespaces=NS),
        })
    return [e for e in entries if e["id"]]


def post_to_discord(entry):
    summary = entry["summary"]
    if len(summary) > 350:
        summary = summary[:350].rsplit(" ", 1)[0] + " …"
    embed = {
        "title": entry["title"][:256] or "Neue News",
        "url": entry["link"] or None,
        "description": summary or None,
        "color": EMBED_COLOR,
        "footer": {"text": "World of Tanks News"},
    }
    if entry["image"]:
        embed["image"] = {"url": entry["image"]}
    if entry["published"]:
        embed["timestamp"] = entry["published"]
    embed = {k: v for k, v in embed.items() if v is not None}
    body = json.dumps({"embeds": [embed]}).encode()

    for _ in range(5):
        req = urllib.request.Request(
            WEBHOOK_URL, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "wot-discord-news"},
        )
        try:
            urllib.request.urlopen(req, timeout=30).close()
            return
        except urllib.error.HTTPError as err:
            if err.code == 429:  # Discord Rate-Limit: kurz warten und nochmal
                retry = json.loads(err.read() or b"{}").get("retry_after", 2)
                time.sleep(float(retry) + 0.5)
                continue
            sys.exit(f"Discord hat den Post abgelehnt: HTTP {err.code} {err.read()[:300]!r}")
    sys.exit("Discord Rate-Limit: zu viele Versuche.")


def main():
    if not WEBHOOK_URL:
        sys.exit("DISCORD_WEBHOOK_URL fehlt. Bitte als Repository-Secret eintragen.")

    first_run = not STATE_FILE.exists()
    posted = [] if first_run else json.loads(STATE_FILE.read_text())
    seen = set(posted)

    entries = parse_entries(fetch_feed())  # Feeds liefern das Neueste zuerst
    new = [e for e in entries if e["id"] not in seen]
    if first_run:
        to_post = new[:1]
    else:
        to_post = new[:MAX_POSTS_PER_RUN]

    for entry in reversed(to_post):  # älteste zuerst, damit die Reihenfolge stimmt
        post_to_discord(entry)
        print(f"Gepostet: {entry['title']}")
        time.sleep(1)

    # Alles im aktuellen Feed als gesehen merken (beim ersten Lauf auch die nicht geposteten)
    remembered = [e["id"] for e in entries] + [p for p in posted if p not in {e["id"] for e in entries}]
    STATE_FILE.write_text(json.dumps(remembered[:MAX_REMEMBERED], indent=1, ensure_ascii=False) + "\n")
    print(f"{len(to_post)} neue Einträge gepostet, {len(entries)} im Feed.")


if __name__ == "__main__":
    main()
