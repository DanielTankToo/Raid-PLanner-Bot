"""
Raid Planner - a lightweight Discord bot for WoW raid scheduling.

Features
  * Raid night poll  - guild picks the days/times they can raid from dropdowns (/raidnightpoll)
  * Raid nights      - officers schedule a raid; members sign up as Tank / Healer / DPS
  * Role limits      - optional caps per role; extra signups go to the bench automatically
  * Reminders        - pings everyone signed up N minutes before the raid starts

Everything is driven by buttons and pop-up forms. Officers post the Raid Night Poll once with
/raidnightpoll; the pinned panel (PANEL_CHANNEL_ID, or /raidpanel) is for scheduling raids.

Buttons keep working after a restart: every button carries its poll/raid id in its
custom_id and is handled in on_interaction, with all state in SQLite (raids.db).
"""

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

try:  # optional: read settings from a .env file
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

# --------------------------------------------------------------------------- config

TOKEN = os.getenv("DISCORD_TOKEN", "")
OFFICER_ROLE = os.getenv("OFFICER_ROLE", "Officer")
TIMEZONE = os.getenv("TIMEZONE", "America/Los_Angeles")
TZ = ZoneInfo(TIMEZONE)
REMINDER_MINUTES = int(os.getenv("REMINDER_MINUTES", "30"))  # 0 = no reminders
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Kept next to bot.py so it never mixes with another bot's files on the same Pi
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "raids.db"))
GUILD_ID = os.getenv("GUILD_ID")  # optional: makes /raidpanel show up instantly
# Optional: channel (forum or text) where the bot keeps the panel automatically - no /raidpanel needed
PANEL_CHANNEL_ID = int(os.getenv("PANEL_CHANNEL_ID") or 0)
FORUM_ARCHIVE_MINUTES = 10080  # forum posts stay active 7 days (Discord's max) before going quiet

DEFAULT_NIGHTS = "Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday, Monday"
DEFAULT_SLOTS = "7-9 PM, 9-11 PM"
# Start times offered in the Schedule Raid dropdown (max 25). Blank = every 30 min, 4:00 PM-11:30 PM
RAID_TIMES = os.getenv("RAID_TIMES", "")
DATE_CHOICES = 25  # how many upcoming days the date dropdown shows (Discord max 25)
MAX_POLL_DAYS = 20   # one button per day (Discord allows 25 buttons, one is "Close poll")
MAX_POLL_SLOTS = 25  # Discord's limit for options in one dropdown

ROLES = {
    "tank": ("Tank", "🛡️"),
    "healer": ("Healer", "💚"),
    "dps": ("DPS", "⚔️"),
}

COLOR_POLL = 0x5865F2
COLOR_OPEN = 0x2ECC71
COLOR_LOCKED = 0xE67E22
COLOR_CANCELLED = 0x7F8C8D
COLOR_PANEL = 0xC9A227

log = logging.getLogger("raid-planner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# --------------------------------------------------------------------------- database

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS polls (
        id INTEGER PRIMARY KEY,
        guild_id INTEGER, channel_id INTEGER, message_id INTEGER,
        title TEXT, options TEXT, closed INTEGER DEFAULT 0, created_by INTEGER
    );
    CREATE TABLE IF NOT EXISTS poll_picks (
        poll_id INTEGER, day INTEGER, slot INTEGER, user_id INTEGER, name TEXT,
        PRIMARY KEY (poll_id, day, slot, user_id)
    );
    CREATE TABLE IF NOT EXISTS raids (
        id INTEGER PRIMARY KEY,
        guild_id INTEGER, channel_id INTEGER, message_id INTEGER,
        name TEXT, when_text TEXT, starts_at INTEGER,
        cap_tank INTEGER DEFAULT 0, cap_healer INTEGER DEFAULT 0, cap_dps INTEGER DEFAULT 0,
        notes TEXT, status TEXT DEFAULT 'open', reminded INTEGER DEFAULT 0, created_by INTEGER
    );
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS signups (
        raid_id INTEGER, user_id INTEGER, name TEXT, role TEXT, joined_at REAL,
        PRIMARY KEY (raid_id, user_id)
    );
    """
)
# Upgrade databases made by the first version (polls had no time slots)
if "slots" not in [r[1] for r in db.execute("PRAGMA table_info(polls)")]:
    db.execute("ALTER TABLE polls ADD COLUMN slots TEXT DEFAULT ''")
db.commit()


def get_setting(key):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key, value):
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    db.commit()


def get_poll(poll_id):
    return db.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()


def get_raid(raid_id):
    return db.execute("SELECT * FROM raids WHERE id = ?", (raid_id,)).fetchone()


# --------------------------------------------------------------------------- helpers

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_time(time_s: str):
    """'8:00 PM' / '8pm' / '20:00' -> datetime.time, or None."""
    ts = time_s.strip().upper().replace(".", "")
    for fmt in ("%I:%M %p", "%I:%M%p", "%I %p", "%I%p", "%H:%M", "%H"):
        try:
            return datetime.strptime(ts, fmt).time()
        except ValueError:
            pass
    return None


def fmt_time(t) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def time_choices():
    """[(label, 'HHMM')] for the start-time dropdown."""
    times = [parse_time(s) for s in RAID_TIMES.split(",") if s.strip()] if RAID_TIMES else []
    times = [t for t in times if t]
    if not times:
        times = [datetime(2000, 1, 1, h, m).time() for h in range(16, 24) for m in (0, 30)]
    seen, out = set(), []
    for t in times:
        key = t.strftime("%H%M")
        if key not in seen:
            seen.add(key)
            out.append((fmt_time(t), key))
    return out[:25]


def parse_when(date_s: str, time_s: str):
    """Turn '<date>' + '<time>' into a unix timestamp in the guild's timezone.

    Dates:  Friday / fri, 11/21, 11/21/2026, 2026-11-21
    Times:  8:00 PM, 8pm, 8 PM, 20:00
    Returns None if it can't be read.
    """
    now = datetime.now(TZ)
    s = date_s.strip()
    low = s.lower()
    day = None
    by_weekday = False

    for i, w in enumerate(WEEKDAYS):
        if low.startswith(w):
            day = (now + timedelta(days=(i - now.weekday()) % 7)).date()
            by_weekday = True
            break
    if day is None:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
            try:
                day = datetime.strptime(s, fmt).date()
                break
            except ValueError:
                pass
    if day is None:  # 11/21 with no year -> next time that date comes round
        try:
            day = datetime.strptime(f"{s}/{now.year}", "%m/%d/%Y").date()
            if day < now.date():
                day = day.replace(year=now.year + 1)
        except ValueError:
            return None

    t = parse_time(time_s)
    if t is None:
        return None

    dt = datetime.combine(day, t, tzinfo=TZ)
    if by_weekday and dt < now:  # "Friday" said on Friday night after start -> next week
        dt += timedelta(days=7)
    return int(dt.timestamp())


def parse_caps(text: str):
    """'2/5/13' -> (2, 5, 13). Blank or unreadable -> (0, 0, 0) meaning no limits."""
    parts = [p.strip() for p in text.replace(",", "/").replace(" ", "/").split("/") if p.strip()]
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return (0, 0, 0)
    return tuple(int(p) for p in parts)


def is_officer(member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    if perms and (perms.manage_guild or perms.administrator):
        return True
    return any(r.name == OFFICER_ROLE for r in getattr(member, "roles", []))


def split_roster(raid, signups):
    """Return {role: (main_list, bench_list)} ordered by signup time."""
    caps = {"tank": raid["cap_tank"], "healer": raid["cap_healer"], "dps": raid["cap_dps"]}
    out = {}
    for role in ROLES:
        people = [s for s in signups if s["role"] == role]
        cap = caps[role] or 0
        out[role] = (people[:cap], people[cap:]) if cap else (people, [])
    return out


def parse_list(text: str, limit: int):
    """'a, b, a,, c' -> ['a', 'b', 'c'] (commas or new lines, no duplicates)."""
    items = []
    for item in str(text).replace("\n", ",").split(","):
        item = item.strip().replace("|", "/")[:80]
        if item and item not in items:
            items.append(item)
    return items[:limit]


def poll_days(poll):
    return poll["options"].split("|")


def poll_slots(poll):
    return poll["slots"].split("|") if poll["slots"] else ["Available"]


def raid_signups(raid_id):
    return db.execute(
        "SELECT * FROM signups WHERE raid_id = ? ORDER BY joined_at", (raid_id,)
    ).fetchall()


# --------------------------------------------------------------------------- embeds & views


def poll_embed(poll) -> discord.Embed:
    days, slots = poll_days(poll), poll_slots(poll)
    picks = db.execute(
        "SELECT day, slot, name FROM poll_picks WHERE poll_id = ? ORDER BY rowid", (poll["id"],)
    ).fetchall()
    grid = {}
    for p in picks:
        grid.setdefault((p["day"], p["slot"]), []).append(p["name"])
    voters = db.execute(
        "SELECT COUNT(DISTINCT user_id) FROM poll_picks WHERE poll_id = ?", (poll["id"],)
    ).fetchone()[0]
    top = max((len(n) for n in grid.values()), default=0)
    closed = bool(poll["closed"])

    def day_fields(show_names: bool):
        fields = []
        for d, day in enumerate(days):
            lines = []
            for s, slot in enumerate(slots):
                names = grid.get((d, s), [])
                n = len(names)
                crown = " 🏆" if closed and top and n == top else ""
                line = f"`{slot}` **{n}**{crown}"
                if show_names and names:
                    line += " · " + ", ".join(names[:10]) + (f" +{n - 10}" if n > 10 else "")
                lines.append(line)
            value = "\n".join(lines)
            fields.append((day, value[:1020] + "…" if len(value) > 1024 else value))
        return fields

    fields = day_fields(True)
    if sum(len(n) + len(v) for n, v in fields) > 5000:  # Discord caps an embed at 6000 chars
        fields = day_fields(False)

    if closed:
        desc = f"**{poll['title']}**\nPoll closed."
    else:
        desc = f"**{poll['title']}**\nClick a day below, then pick every time slot you can make in the dropdown."
    embed = discord.Embed(title="📊 Raid Night Poll", description=desc, color=COLOR_POLL)
    for name, value in fields:
        embed.add_field(name=name, value=value, inline=True)

    if closed:
        best = [f"{days[d]} {slots[s]}" for (d, s), n in grid.items()
                if top and len(n) == top and d < len(days) and s < len(slots)]
        result = ", ".join(best[:3]) + (f" +{len(best) - 3} more" if len(best) > 3 else "")
        embed.set_footer(text=f"{voters} voted · Best: {result or 'no votes'}" + (f" ({top})" if top else ""))
    else:
        embed.set_footer(text=f"{voters} voted · You can change your times any time until the poll closes")
    return embed


def poll_view(poll) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    closed = bool(poll["closed"])
    for i, day in enumerate(poll_days(poll)):
        view.add_item(
            discord.ui.Button(
                label=day[:80],
                emoji="🗓️",
                style=discord.ButtonStyle.secondary,
                custom_id=f"poll:{poll['id']}:{i}",
                disabled=closed,
            )
        )
    view.add_item(
        discord.ui.Button(
            label="Close poll",
            emoji="🔒",
            style=discord.ButtonStyle.danger,
            custom_id=f"pollclose:{poll['id']}",
            disabled=closed,
        )
    )
    return view


def slot_view(poll, day: int, user_id: int) -> discord.ui.View:
    """Private dropdown of time slots for one day, with the user's current picks ticked."""
    mine = {r["slot"] for r in db.execute(
        "SELECT slot FROM poll_picks WHERE poll_id = ? AND day = ? AND user_id = ?",
        (poll["id"], day, user_id),
    )}
    slots = poll_slots(poll)
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Select(
            custom_id=f"pollslot:{poll['id']}:{day}",
            placeholder="Pick the times you can make (clear all = can't make it)",
            min_values=0,
            max_values=len(slots),
            options=[
                discord.SelectOption(label=s[:100], value=str(i), default=i in mine)
                for i, s in enumerate(slots)
            ],
        )
    )
    return view


def raid_embed(raid) -> discord.Embed:
    signups = raid_signups(raid["id"])
    roster = split_roster(raid, signups)
    status = raid["status"]

    title = f"⚔️ {raid['name']}"
    if status == "cancelled":
        title = f"~~{raid['name']}~~ — CANCELLED"
    color = {"open": COLOR_OPEN, "locked": COLOR_LOCKED}.get(status, COLOR_CANCELLED)

    if raid["starts_at"]:
        when = f"<t:{raid['starts_at']}:F> (<t:{raid['starts_at']}:R>)"
    else:
        when = raid["when_text"] or "TBD"
    desc = f"🗓️ {when}"
    if raid["notes"]:
        desc += f"\n\n{raid['notes']}"

    embed = discord.Embed(title=title, description=desc, color=color)
    confirmed = 0
    for role, (label, emoji) in ROLES.items():
        main, bench = roster[role]
        confirmed += len(main)
        cap = raid[f"cap_{role}"]
        header = f"{emoji} {label} ({len(main)}/{cap})" if cap else f"{emoji} {label} ({len(main)})"
        value = "\n".join(f"• {s['name']}" for s in main) or "—"
        if bench:
            value += "\n*Bench:* " + ", ".join(s["name"] for s in bench)
        if len(value) > 1024:
            value = value[:1020] + "…"
        embed.add_field(name=header, value=value, inline=True)

    benched = len(signups) - confirmed
    footer = f"{confirmed} signed up"
    if benched:
        footer += f" · {benched} on bench"
    footer += {
        "open": " · Pick your role below",
        "locked": " · Signups locked",
        "cancelled": "",
    }.get(status, "")
    embed.set_footer(text=footer)
    return embed


def raid_view(raid) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    rid = raid["id"]
    status = raid["status"]
    signups_closed = status != "open"

    for role, (label, emoji) in ROLES.items():
        view.add_item(
            discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.primary,
                custom_id=f"raid:{rid}:{role}",
                disabled=signups_closed,
                row=0,
            )
        )
    view.add_item(
        discord.ui.Button(
            label="Leave",
            style=discord.ButtonStyle.secondary,
            custom_id=f"raid:{rid}:leave",
            disabled=signups_closed,
            row=0,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Unlock" if status == "locked" else "Lock",
            emoji="🔓" if status == "locked" else "🔒",
            style=discord.ButtonStyle.secondary,
            custom_id=f"raidlock:{rid}",
            disabled=status == "cancelled",
            row=1,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Cancel raid",
            emoji="🗑️",
            style=discord.ButtonStyle.danger,
            custom_id=f"raidcancel:{rid}",
            disabled=status == "cancelled",
            row=1,
        )
    )
    return view


def panel_embed() -> discord.Embed:
    return discord.Embed(
        title="🏰 Raid Planner",
        description=(
            "**⚔️ Schedule Raid** — post a raid night with Tank/Healer/DPS signups *(officers)*\n"
            "**📋 Upcoming Raids** — see what's on the calendar"
        ),
        color=COLOR_PANEL,
    )


def panel_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Schedule Raid", emoji="⚔️", style=discord.ButtonStyle.success, custom_id="panel:raid"))
    view.add_item(discord.ui.Button(label="Upcoming Raids", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="panel:list"))
    return view


# --------------------------------------------------------------------------- pop-up forms


class PollModal(discord.ui.Modal, title="New Raid Night Poll"):
    poll_title = discord.ui.TextInput(
        label="Question", default="Which nights can you raid this week?", max_length=200
    )
    nights = discord.ui.TextInput(
        label="Days (comma separated)",
        default=DEFAULT_NIGHTS,
        style=discord.TextStyle.paragraph,
        max_length=600,
    )
    time_slots = discord.ui.TextInput(
        label="Time slots for the dropdown (comma separated)",
        default=DEFAULT_SLOTS,
        placeholder="e.g. 6-8 PM, 7-9 PM, 9-11 PM",
        style=discord.TextStyle.paragraph,
        max_length=600,
    )

    async def on_submit(self, interaction: discord.Interaction):
        days = parse_list(self.nights.value, MAX_POLL_DAYS)
        slots = parse_list(self.time_slots.value, MAX_POLL_SLOTS)
        if not days or not slots:
            await interaction.response.send_message(
                "A poll needs at least 1 day and 1 time slot.", ephemeral=True
            )
            return

        cur = db.execute(
            """INSERT INTO polls (guild_id, channel_id, title, options, slots, created_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (interaction.guild_id, interaction.channel_id, str(self.poll_title.value),
             "|".join(days), "|".join(slots), interaction.user.id),
        )
        db.commit()
        poll = get_poll(cur.lastrowid)
        channel_id, msg = await post_board(
            interaction, "📊 Raid Night Poll", poll_embed(poll), poll_view(poll), "Poll"
        )
        db.execute("UPDATE polls SET channel_id = ?, message_id = ? WHERE id = ?", (channel_id, msg.id, poll["id"]))
        db.commit()
        await interaction.response.send_message(f"Poll posted ✅ [Jump to it]({msg.jump_url})", ephemeral=True)


def schedule_picker(date_key: str = "", time_key: str = ""):
    """Private message with a date dropdown, a time dropdown and a Next button.

    The current picks ride along in each component's custom_id, so nothing is kept in
    memory and the picker still works if the bot restarts.
    date_key = 'YYYYMMDD', time_key = 'HHMM' (or '' when not picked yet).
    """
    today = datetime.now(TZ).date()
    date_opts = []
    for i in range(DATE_CHOICES):
        d = today + timedelta(days=i)
        key = d.strftime("%Y%m%d")
        hint = "Today" if i == 0 else "Tomorrow" if i == 1 else f"In {i} days"
        date_opts.append(discord.SelectOption(
            label=d.strftime("%A, %b ") + str(d.day), value=key, description=hint,
            emoji="📅", default=key == date_key,
        ))
    time_opts = [
        discord.SelectOption(label=label, value=key, emoji="🕗", default=key == time_key)
        for label, key in time_choices()
    ]
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Select(
        custom_id=f"rsched:date:{time_key or '-'}", placeholder="📅 Pick a date", options=date_opts, row=0,
    ))
    view.add_item(discord.ui.Select(
        custom_id=f"rsched:time:{date_key or '-'}", placeholder=f"🕗 Pick a start time ({TIMEZONE})",
        options=time_opts, row=1,
    ))
    ready = bool(date_key and time_key)
    view.add_item(discord.ui.Button(
        label="Next: raid details", emoji="➡️", style=discord.ButtonStyle.success,
        custom_id=f"rsched:next:{date_key or '-'}:{time_key or '-'}", disabled=not ready, row=2,
    ))

    content = "**Schedule a raid**: pick the date and start time, then click **Next**."
    when = picked_datetime(date_key, time_key)
    if when:
        content += f"\nPicked: **<t:{int(when.timestamp())}:F>**"
        if when < datetime.now(TZ):
            content += "  ⚠️ that time has already passed"
    return content, view


def picked_datetime(date_key: str, time_key: str):
    if len(date_key or "") != 8 or len(time_key or "") != 4:
        return None
    try:
        return datetime.strptime(date_key + time_key, "%Y%m%d%H%M").replace(tzinfo=TZ)
    except (ValueError, TypeError):
        return None


class RaidModal(discord.ui.Modal, title="Raid details"):
    raid_name = discord.ui.TextInput(label="Raid", placeholder="e.g. Molten Core", max_length=100)
    caps = discord.ui.TextInput(
        label="Role limits Tank/Healer/DPS (optional)",
        placeholder="e.g. 4/8/28 — leave blank for no limits",
        required=False,
        max_length=20,
    )
    notes = discord.ui.TextInput(
        label="Notes (optional)",
        placeholder="Bring fire resist, consumes, etc.",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=800,
    )

    def __init__(self, when: datetime):
        super().__init__()
        self.when = when

    async def on_submit(self, interaction: discord.Interaction):
        starts_at = int(self.when.timestamp())
        tank, healer, dps = parse_caps(str(self.caps.value or ""))
        when_text = self.when.strftime("%A, %b %d ") + fmt_time(self.when.time())

        cur = db.execute(
            """INSERT INTO raids (guild_id, channel_id, name, when_text, starts_at,
                   cap_tank, cap_healer, cap_dps, notes, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (interaction.guild_id, interaction.channel_id, str(self.raid_name.value), when_text,
             starts_at, tank, healer, dps, str(self.notes.value or ""), interaction.user.id),
        )
        db.commit()
        raid = get_raid(cur.lastrowid)
        title = f"⚔️ {raid['name']} · " + self.when.strftime("%a %b ") + str(self.when.day) + " " + fmt_time(self.when.time())
        channel_id, msg = await post_board(interaction, title, raid_embed(raid), raid_view(raid), "Raid")
        db.execute("UPDATE raids SET channel_id = ?, message_id = ? WHERE id = ?", (channel_id, msg.id, raid["id"]))
        db.commit()
        await interaction.response.send_message(f"Raid posted ✅ [Jump to it]({msg.jump_url})", ephemeral=True)


# --------------------------------------------------------------------------- forum support


def forum_of(channel):
    """The forum a channel belongs to (if it's a forum post), else None."""
    if isinstance(channel, discord.ForumChannel):
        return channel
    if isinstance(channel, discord.Thread) and isinstance(channel.parent, discord.ForumChannel):
        return channel.parent
    return None


async def post_board(interaction, title, embed, view, tag_name):
    """Post a poll/raid. In a forum it gets its own post (tagged if the tag exists);
    in a normal text channel it's posted in that channel. Returns (channel_id, message)."""
    forum = forum_of(interaction.channel)
    if forum is None:
        msg = await interaction.channel.send(embed=embed, view=view)
        return interaction.channel.id, msg
    kwargs = dict(name=title[:100], embed=embed, view=view, auto_archive_duration=FORUM_ARCHIVE_MINUTES)
    tags = [t for t in forum.available_tags if t.name.lower() == tag_name.lower()]
    if tags:
        kwargs["applied_tags"] = tags[:1]
    thread, msg = await forum.create_thread(**kwargs)
    return thread.id, msg


async def wake_thread(channel):
    """Forum posts archive after a week of quiet; reopen so buttons and edits keep working."""
    if isinstance(channel, discord.Thread) and channel.archived:
        try:
            await channel.edit(archived=False)
        except discord.HTTPException:
            log.warning("Couldn't reopen thread %s (bot needs Manage Threads)", channel.id)


async def ensure_panel():
    """Make sure the panel exists in PANEL_CHANNEL_ID; create (and pin) it if it's missing."""
    if not PANEL_CHANNEL_ID:
        return
    ch_id, msg_id = get_setting("panel_channel"), get_setting("panel_message")
    if ch_id and msg_id and get_setting("panel_for") == str(PANEL_CHANNEL_ID):
        try:
            ch = client.get_channel(int(ch_id)) or await client.fetch_channel(int(ch_id))
            await wake_thread(ch)
            msg = await ch.fetch_message(int(msg_id))
            await msg.edit(embed=panel_embed(), view=panel_view())  # refresh in case buttons changed
            return
        except discord.NotFound:
            log.info("Panel was deleted, making a new one")
        except discord.HTTPException:
            log.exception("Couldn't check the panel")
            return

    try:
        target = client.get_channel(PANEL_CHANNEL_ID) or await client.fetch_channel(PANEL_CHANNEL_ID)
        if isinstance(target, discord.ForumChannel):
            thread, msg = await target.create_thread(
                name="🏰 Raid Planner", embed=panel_embed(), view=panel_view(),
                auto_archive_duration=FORUM_ARCHIVE_MINUTES,
            )
            try:
                await thread.edit(pinned=True)
            except discord.HTTPException:
                log.warning("Couldn't pin the panel post (bot needs Manage Threads)")
            ch_id = thread.id
        else:
            msg = await target.send(embed=panel_embed(), view=panel_view())
            try:
                await msg.pin()
            except discord.HTTPException:
                log.warning("Couldn't pin the panel (bot needs Manage Messages)")
            ch_id = target.id
    except discord.HTTPException:
        log.exception("Couldn't create the panel in channel %s - check PANEL_CHANNEL_ID and permissions", PANEL_CHANNEL_ID)
        return
    set_setting("panel_channel", ch_id)
    set_setting("panel_message", msg.id)
    set_setting("panel_for", PANEL_CHANNEL_ID)
    log.info("Panel posted in %s", ch_id)


@tasks.loop(hours=6)
async def keep_panel_awake():
    ch_id = get_setting("panel_channel")
    if ch_id:
        ch = client.get_channel(int(ch_id))
        if ch is None:
            try:
                ch = await client.fetch_channel(int(ch_id))
            except discord.HTTPException:
                return
        await wake_thread(ch)


# --------------------------------------------------------------------------- bot


class RaidBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        if REMINDER_MINUTES > 0:
            reminder_loop.start()


client = RaidBot()


@client.tree.command(name="raidpanel", description="Post the Raid Planner panel in this channel (officers)")
@app_commands.guild_only()
async def raidpanel(interaction: discord.Interaction):
    if not is_officer(interaction.user):
        await interaction.response.send_message(f"Only officers (role **{OFFICER_ROLE}**) can do that.", ephemeral=True)
        return
    await interaction.channel.send(embed=panel_embed(), view=panel_view())
    await interaction.response.send_message("Panel posted ✅", ephemeral=True)


@client.tree.command(name="raidnightpoll", description="Post a Raid Night Poll (officers)")
@app_commands.guild_only()
async def raidnightpoll(interaction: discord.Interaction):
    if not is_officer(interaction.user):
        await interaction.response.send_message(f"Only officers (role **{OFFICER_ROLE}**) can do that.", ephemeral=True)
        return
    await interaction.response.send_modal(PollModal())


@client.event
async def on_ready():
    log.info("Logged in as %s (%s)", client.user, client.user.id)
    if not getattr(client, "panel_checked", False):  # on_ready can fire again after reconnects
        client.panel_checked = True
        await ensure_panel()
        if PANEL_CHANNEL_ID and not keep_panel_awake.is_running():
            keep_panel_awake.start()


@client.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
    custom_id = (interaction.data or {}).get("custom_id", "")
    await wake_thread(interaction.channel)
    kind, _, rest = custom_id.partition(":")
    try:
        if kind == "panel":
            await handle_panel(interaction, rest)
        elif kind == "poll":
            poll_id, day = rest.split(":")
            await handle_poll_day(interaction, int(poll_id), int(day))
        elif kind == "pollslot":
            poll_id, day = rest.split(":")
            await handle_poll_slots(interaction, int(poll_id), int(day))
        elif kind == "pollclose":
            await handle_poll_close(interaction, int(rest))
        elif kind == "raid":
            raid_id, action = rest.split(":")
            await handle_raid_signup(interaction, int(raid_id), action)
        elif kind == "rsched":
            await handle_schedule_picker(interaction, rest)
        elif kind == "raidlock":
            await handle_raid_lock(interaction, int(rest))
        elif kind == "raidcancel":
            await handle_raid_cancel(interaction, int(rest))
    except Exception:
        log.exception("Error handling %s", custom_id)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("Something went wrong — try again.", ephemeral=True)
        except discord.HTTPException:
            pass


async def deny_if_not_officer(interaction) -> bool:
    if is_officer(interaction.user):
        return False
    await interaction.response.send_message(f"Only officers (role **{OFFICER_ROLE}**) can do that.", ephemeral=True)
    return True


async def handle_panel(interaction, action):
    if action == "list":
        now = int(time.time())
        raids = db.execute(
            """SELECT * FROM raids WHERE guild_id = ? AND status != 'cancelled'
               AND (starts_at IS NULL OR starts_at > ?) ORDER BY starts_at IS NULL, starts_at LIMIT 10""",
            (interaction.guild_id, now - 3 * 3600),
        ).fetchall()
        if not raids:
            await interaction.response.send_message("No upcoming raids.", ephemeral=True)
            return
        lines = []
        for r in raids:
            when = f"<t:{r['starts_at']}:f>" if r["starts_at"] else r["when_text"]
            count = db.execute("SELECT COUNT(*) FROM signups WHERE raid_id = ?", (r["id"],)).fetchone()[0]
            link = f"https://discord.com/channels/{r['guild_id']}/{r['channel_id']}/{r['message_id']}"
            lines.append(f"**[{r['name']}]({link})** — {when} · {count} signed up")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)
        return

    if await deny_if_not_officer(interaction):
        return
    if action == "poll":
        await interaction.response.send_modal(PollModal())
    elif action == "raid":
        content, view = schedule_picker()
        await interaction.response.send_message(content, view=view, ephemeral=True)


async def handle_poll_day(interaction, poll_id, day):
    """A day button was clicked: show that day's time-slot dropdown privately."""
    poll = get_poll(poll_id)
    if not poll or poll["closed"]:
        await interaction.response.send_message("This poll is closed.", ephemeral=True)
        return
    days = poll_days(poll)
    if day >= len(days):
        return
    await interaction.response.send_message(
        f"🗓️ **{days[day]}**: which times can you make?",
        view=slot_view(poll, day, interaction.user.id),
        ephemeral=True,
    )


async def handle_poll_slots(interaction, poll_id, day):
    """Times were picked in the dropdown: save them and refresh the poll."""
    poll = get_poll(poll_id)
    if not poll or poll["closed"]:
        await interaction.response.edit_message(content="This poll is closed.", view=None)
        return
    days, slots = poll_days(poll), poll_slots(poll)
    if day >= len(days):
        return
    uid = interaction.user.id
    picked = sorted({int(v) for v in (interaction.data or {}).get("values", []) if int(v) < len(slots)})

    db.execute("DELETE FROM poll_picks WHERE poll_id = ? AND day = ? AND user_id = ?", (poll_id, day, uid))
    db.executemany(
        "INSERT INTO poll_picks (poll_id, day, slot, user_id, name) VALUES (?, ?, ?, ?, ?)",
        [(poll_id, day, s, uid, interaction.user.display_name) for s in picked],
    )
    db.commit()

    summary = ", ".join(slots[s] for s in picked) if picked else "can't make it"
    await interaction.response.edit_message(
        content=f"✅ **{days[day]}**: {summary}\nChange your picks below, or click another day on the poll.",
        view=slot_view(poll, day, uid),
    )

    try:
        channel = client.get_channel(poll["channel_id"]) or await client.fetch_channel(poll["channel_id"])
        await channel.get_partial_message(poll["message_id"]).edit(
            embed=poll_embed(poll), view=poll_view(poll)
        )
    except discord.HTTPException:
        log.exception("Couldn't refresh poll %s", poll_id)


async def handle_schedule_picker(interaction, rest):
    """Date/time dropdowns and the Next button of the Schedule Raid picker."""
    if await deny_if_not_officer(interaction):
        return
    parts = rest.split(":")
    action = parts[0]
    unset = lambda v: "" if v == "-" else v
    values = (interaction.data or {}).get("values", [])

    if action == "date":
        content, view = schedule_picker(values[0] if values else "", unset(parts[1]))
        await interaction.response.edit_message(content=content, view=view)
    elif action == "time":
        content, view = schedule_picker(unset(parts[1]), values[0] if values else "")
        await interaction.response.edit_message(content=content, view=view)
    elif action == "next":
        when = picked_datetime(unset(parts[1]), unset(parts[2]))
        if not when:
            await interaction.response.send_message("Pick a date and a start time first.", ephemeral=True)
            return
        await interaction.response.send_modal(RaidModal(when))


async def handle_poll_close(interaction, poll_id):
    if await deny_if_not_officer(interaction):
        return
    db.execute("UPDATE polls SET closed = 1 WHERE id = ?", (poll_id,))
    db.commit()
    poll = get_poll(poll_id)
    await interaction.response.edit_message(embed=poll_embed(poll), view=poll_view(poll))


async def handle_raid_signup(interaction, raid_id, action):
    raid = get_raid(raid_id)
    if not raid or raid["status"] != "open":
        await interaction.response.send_message("Signups are closed for this raid.", ephemeral=True)
        return
    uid = interaction.user.id

    if action == "leave":
        db.execute("DELETE FROM signups WHERE raid_id = ? AND user_id = ?", (raid_id, uid))
    elif action in ROLES:
        current = db.execute(
            "SELECT role FROM signups WHERE raid_id = ? AND user_id = ?", (raid_id, uid)
        ).fetchone()
        if current and current["role"] == action:
            await interaction.response.send_message(
                f"You're already signed up as **{ROLES[action][0]}**. Use **Leave** to drop out.", ephemeral=True
            )
            return
        db.execute(
            """INSERT INTO signups (raid_id, user_id, name, role, joined_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (raid_id, user_id) DO UPDATE SET
                   role = excluded.role, name = excluded.name, joined_at = excluded.joined_at""",
            (raid_id, uid, interaction.user.display_name, action, time.time()),
        )
    else:
        return
    db.commit()
    await interaction.response.edit_message(embed=raid_embed(raid), view=raid_view(raid))

    if action in ROLES:
        main, bench = split_roster(raid, raid_signups(raid_id))[action]
        if any(s["user_id"] == uid for s in bench):
            await interaction.followup.send(
                f"**{ROLES[action][0]}** is full, so you're on the bench — you'll move up if someone leaves.",
                ephemeral=True,
            )


async def handle_raid_lock(interaction, raid_id):
    if await deny_if_not_officer(interaction):
        return
    raid = get_raid(raid_id)
    if not raid or raid["status"] == "cancelled":
        await interaction.response.send_message("That raid was cancelled.", ephemeral=True)
        return
    new_status = "open" if raid["status"] == "locked" else "locked"
    db.execute("UPDATE raids SET status = ? WHERE id = ?", (new_status, raid_id))
    db.commit()
    raid = get_raid(raid_id)
    await interaction.response.edit_message(embed=raid_embed(raid), view=raid_view(raid))


async def handle_raid_cancel(interaction, raid_id):
    if await deny_if_not_officer(interaction):
        return
    db.execute("UPDATE raids SET status = 'cancelled' WHERE id = ?", (raid_id,))
    db.commit()
    raid = get_raid(raid_id)
    await interaction.response.edit_message(embed=raid_embed(raid), view=raid_view(raid))


# --------------------------------------------------------------------------- reminders


@tasks.loop(minutes=1)
async def reminder_loop():
    now = int(time.time())
    due = db.execute(
        """SELECT * FROM raids WHERE status != 'cancelled' AND reminded = 0
           AND starts_at IS NOT NULL AND starts_at <= ? AND starts_at > ?""",
        (now + REMINDER_MINUTES * 60, now - 15 * 60),
    ).fetchall()
    for raid in due:
        db.execute("UPDATE raids SET reminded = 1 WHERE id = ?", (raid["id"],))
        db.commit()
        signups = raid_signups(raid["id"])
        if not signups:
            continue
        try:
            channel = client.get_channel(raid["channel_id"]) or await client.fetch_channel(raid["channel_id"])
            mentions = " ".join(f"<@{s['user_id']}>" for s in signups)
            await channel.send(
                f"⏰ **{raid['name']}** starts <t:{raid['starts_at']}:R>! {mentions}",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except discord.HTTPException:
            log.exception("Couldn't send reminder for raid %s", raid["id"])


@reminder_loop.before_loop
async def before_reminders():
    await client.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your environment or .env file.")
    client.run(TOKEN, log_handler=None)
