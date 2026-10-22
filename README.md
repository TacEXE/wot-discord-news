# WoT News nach Discord

Postet neue Einträge aus dem World-of-Tanks-News-Feed (https://worldoftanks.eu/de/rss/news/)
automatisch in einen Discord-Channel. Läuft alle 4 Stunden über GitHub Actions.

- `rss_to_discord.py` holt den Feed und postet neue Einträge per Webhook.
- `posted.json` merkt sich, was schon gepostet wurde (wird automatisch aktualisiert).
- Die Webhook-Adresse liegt als Repository-Secret `DISCORD_WEBHOOK_URL`, nie im Code.

Manuell starten: Tab **Actions** → **WoT News nach Discord** → **Run workflow**.
