# Raid Planner

A lightweight Discord bot for the Vagabonds guild on WoW Forever:

- **Night polls**: members click every night they can raid, and officers close the poll to show the best nights 🏆
- **Raid nights**: officers schedule a raid, and members sign up as 🛡️ Tank, 💚 Healer or ⚔️ DPS
- **Role limits** (optional): once a role is full, extra signups go to the bench and move up when someone leaves
- **Reminders**: pings everyone signed up 30 minutes before start (you can change the time or turn it off)
- Raid times show in each member's **own local time** in Discord

Everything runs on buttons and pop-up forms. Officers run one command, `/raidpanel`, to post the control panel.

It is a **separate bot** from the Guild Market bot. It has its own folder, Discord application, token, database (`raids.db`) and service, so both run on the same Raspberry Pi without affecting each other. It uses very little memory (well under 100 MB).

---

## 1. Create a second Discord application

1. Go to <https://discord.com/developers/applications> and click **New Application**. Name it something like *Raid Planner*.
2. **Bot** tab: click **Reset Token** and copy the token. *This is a different token from your marketplace bot.*
   No privileged intents are needed, so leave all three toggles off.
3. **OAuth2 → URL Generator**: tick `bot` and `applications.commands`.
   Bot permissions: **View Channels, Send Messages, Embed Links, Read Message History**.
   Open the generated URL and invite the bot to your server.

## 2. Install on the Pi (next to the marketplace bot)

Copy the `raid-planner` folder to the Pi, for example to `/home/pi/raid-planner`, then:

```bash
cd ~/raid-planner
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env          # paste the NEW token, set your officer role name
venv/bin/python bot.py   # test run; press Ctrl+C to stop
```

The bot has its own `venv`, so its packages never conflict with the marketplace bot's.

## 3. Run it 24/7 as its own service

```bash
sudo cp raid-planner.service /etc/systemd/system/
sudo nano /etc/systemd/system/raid-planner.service   # fix username/paths if not "pi"
sudo systemctl daemon-reload
sudo systemctl enable --now raid-planner
```

Useful commands:

```bash
systemctl status raid-planner        # is it running?
journalctl -u raid-planner -f        # live logs
sudo systemctl restart raid-planner  # after updating bot.py
```

Restarting, updating or stopping this service never touches the marketplace bot, and the reverse is also true.

## 4. Use it in Discord

1. In the channel where you want raids posted, an officer types **`/raidpanel`**. If the command doesn't appear, put your server ID in `GUILD_ID` in `.env` and restart the bot.
2. The panel has three buttons:
   - **📊 Night Poll** *(officers)*: a form with the question and the nights (Tue–Mon are filled in by default). Members click every night that works. Clicking a night again removes it. **🔒 Close poll** shows the winners.
   - **⚔️ Schedule Raid** *(officers)*: a form with the raid name, date (`Friday`, `11/21` or `2026-11-21`), start time (`8:00 PM`), optional role limits (`4/8/28` = 4 tanks / 8 healers / 28 DPS) and notes.
   - **📋 Upcoming Raids** *(everyone)*: a private list of upcoming raids with links.
3. On a raid post, members click **Tank / Healer / DPS**. Clicking a different role switches role, and **Leave** drops out.
   Officers can **🔒 Lock** signups (and unlock them) or **🗑️ Cancel** the raid.

Buttons keep working after the bot restarts.

## Settings (`.env`)

| Setting | What it does | Default |
|---|---|---|
| `DISCORD_TOKEN` | This bot's token (not the marketplace one) | required |
| `OFFICER_ROLE` | Role that can create and manage polls and raids (Manage Server also works) | `Officer` |
| `TIMEZONE` | Timezone officers type start times in | `America/Los_Angeles` |
| `REMINDER_MINUTES` | Minutes before start to ping signups; `0` turns reminders off | `30` |
| `GUILD_ID` | Your server ID, so `/raidpanel` appears instantly | blank |

## Files

- `bot.py`: the bot
- `raids.db`: created automatically and holds polls, votes, raids and signups (kept out of git)
- `.env`: your token and settings (kept out of git; **never commit it**)
