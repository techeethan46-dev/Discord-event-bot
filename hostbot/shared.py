"""Shared helpers used everywhere: time parsing, event lookup, permissions, notices, logging, event banner embed, per-event log + waitlist."""

import asyncio
import discord
import re
import time

from hostbot.core import (
    BOT_OWNER_IDS, SAFE_MENTIONS, _RATE_LIMIT_TASKS, _bg_tasks, bot, db,
    disabled_guild_response, get_config, is_trusted, save_db,
)
from hostbot import core

def parse_time(t_str):
    """'1h30m', '45m', '90s', '2d' or a bare number (minutes) -> seconds.
    Returns None when the text can't be understood, so callers can say so instead of guessing."""
    s = str(t_str or "").lower().replace(" ", "")
    if s in ("", "now", "0"):
        return 0
    if not re.fullmatch(r"(?:\d+[dhms]?)+", s):
        return None
    mult = {"d": 86400, "h": 3600, "m": 60, "s": 1, "": 60}
    return sum(int(n) * mult[u] for n, u in re.findall(r"(\d+)([dhms]?)", s))

def find_event(msg_id):
    for key, info in db["events"].items():
        if msg_id in info.get("message_ids", []):
            return key, info
    return None, None

def find_active_event(name, guild_id):
    """Find a running (not concluded) event in this server by its name. Returns (key, info) or (None, None)."""
    low = str(name or "").lower().strip()
    info = db["events"].get(low)
    if info and not info.get("concluded") and event_in_guild(info, guild_id):
        return low, info
    for k, v in db["events"].items():
        if (not v.get("concluded") and event_in_guild(v, guild_id)
                and str(v.get("display_name", "")).lower().strip() == low):
            return k, v
    return None, None

def new_event_key(name):
    """A key that is never already taken, so old/concluded events (and their ratings) are never overwritten."""
    base = name.lower().strip()
    if base not in db["events"]:
        return base
    n = int(time.time())
    while f"{base}~{n}" in db["events"]:
        n += 1
    return f"{base}~{n}"

def event_in_guild(info, guild_id):
    # Old events have no guild_id saved, so they stay visible everywhere (as before).
    return info.get("guild_id") in (None, guild_id)

def is_authorized_manager(member):
    if is_trusted(member):
        return True
    perms = getattr(member, "guild_permissions", None)
    if perms and perms.administrator:
        return True
    guild = getattr(member, "guild", None)
    if guild:
        host_role = get_config(guild.id).get("host_role")
        if host_role and any(r.id == host_role for r in getattr(member, "roles", [])):
            return True
        trial_role = get_config(guild.id).get("trial_role")
        if trial_role and any(r.id == trial_role for r in getattr(member, "roles", [])):
            return True
    return False

def is_server_admin(member):
    """Server admins, plus the bot owners in ANY server (so you can run /setup etc. everywhere)."""
    return member.id in BOT_OWNER_IDS or member.guild_permissions.administrator

def has_host_role(member, guild_id):
    """Return True only when the member has this server's configured Host role."""
    host_role_id = get_config(guild_id).get("host_role")
    return bool(host_role_id) and any(r.id == host_role_id for r in getattr(member, "roles", []))

def is_vip(member, guild_id):
    """Booster, VIP role, or admin: may hold Superpower nations."""
    if member is None:
        return False
    if member.premium_since or member.guild_permissions.administrator:
        return True
    sp_role = get_config(guild_id).get("superpower_role")
    return bool(sp_role) and any(r.id == sp_role for r in member.roles)


async def emergency_rate_limit_shutdown(source="Discord API"):
    """Trip once when Discord returns a 429 and stop our own activity immediately."""
    if core.RATE_LIMIT_TRIGGERED:
        return
    core.RATE_LIMIT_TRIGGERED = True
    print(f"🚨 DISCORD 429 DETECTED ({source}) — Host Assistant entering safe hold mode.")
    current = asyncio.current_task()
    for task in list(_bg_tasks) + list(_RATE_LIMIT_TASKS):
        if task is not None and task is not current and not task.done():
            task.cancel()
    if core.ai_worker_task is not None and core.ai_worker_task is not current and not core.ai_worker_task.done():
        core.ai_worker_task.cancel()
    try:
        await bot.close()
    except Exception as e:
        print("Rate-limit shutdown close error:", e)

_notice_last = {}

async def send_notice(msg, title, text, ttl=10, key="notice", every=60):
    """A small branded notice that deletes itself. (Plain message replies can't be ephemeral;
    only slash commands and buttons can.) Each person sees the same notice at most once per `every` seconds."""
    now = time.time()
    k = (msg.author.id, key)
    if now - _notice_last.get(k, 0) < every:
        return None
    if len(_notice_last) > 500:
        _notice_last.clear()
    _notice_last[k] = now
    emb = discord.Embed(title=title, description=text, color=0x2B2D31)
    emb.set_footer(text=BRAND)
    return await safe_reply(msg, embed=emb, delete_after=ttl)

async def safe_reply(msg, content=None, **kwargs):
    """Reply without ever raising (a dead worker = a dead AI chat) and without live pings."""
    kwargs.setdefault("allowed_mentions", SAFE_MENTIONS)
    try:
        return await msg.reply(content, **kwargs)
    except discord.HTTPException as e:
        if getattr(e, "status", None) == 429:
            await emergency_rate_limit_shutdown("safe_reply")
            return None
        try:
            return await msg.channel.send(content, **kwargs)
        except discord.HTTPException as e2:
            if getattr(e2, "status", None) == 429:
                await emergency_rate_limit_shutdown("safe_reply fallback")
            else:
                print("safe_reply failed:", e2)
            return None

async def refresh_event_message(info, guild=None, remove_view=False):
    ch = bot.get_channel(info.get("channel_id"))
    ids = info.get("message_ids") or []
    if not ch or not ids:
        return False
    try:
        msg = await ch.fetch_message(ids[0])
        kwargs = {"embeds": build_embed(info["display_name"], info, guild or ch.guild)}
        if remove_view:
            kwargs["view"] = None
        await msg.edit(**kwargs)
        return True
    except discord.HTTPException as e:
        print("refresh_event_message failed:", e)
        return False

async def add_event_role(guild, uid):
    r_id = get_config(guild.id).get("event_role")
    role = guild.get_role(r_id) if r_id else None
    mem = guild.get_member(uid)
    if role and mem:
        try: await mem.add_roles(role)
        except discord.HTTPException: pass

async def remove_event_role(guild, uid):
    r_id = get_config(guild.id).get("event_role")
    role = guild.get_role(r_id) if r_id else None
    mem = guild.get_member(uid)
    if role and mem:
        try: await mem.remove_roles(role)
        except discord.HTTPException: pass

_dm_last = {}

async def dm_owners(text, key=None, cooldown=300):
    """DM the bot owners. Repeats of the same alert are throttled so an outage can't spam them."""
    now = time.time()
    k = key or text[:40]
    if now - _dm_last.get(k, 0) < cooldown:
        return
    if len(_dm_last) > 200:
        _dm_last.clear()
    _dm_last[k] = now
    for owner_id in BOT_OWNER_IDS:
        try:
            owner = await bot.fetch_user(owner_id)
            await owner.send(text[:1900])
        except Exception as e:
            print("Failed to DM owner:", e)

async def log_action(guild, title, desc, color=0x3498DB, channel_key=None):
    # AI Admin actions have their own setup channel.  Everything else keeps using
    # the normal event log channel, so existing logging is unchanged.
    log_id = get_config(guild.id).get(channel_key or "log_channel")
    if not log_id:
        # Preserve the old fallback if an AI log channel was not configured.
        if channel_key == "ai_log_channel":
            log_id = get_config(guild.id).get("log_channel")
        if not log_id:
            return
    channel = guild.get_channel(log_id)
    if channel:
        emb = discord.Embed(description=f"## 📝 {title}\n{DIV}\n{desc}", color=color)
        try: 
            await channel.send(embed=emb)
        except discord.HTTPException: 
            pass

# One solid, straight divider. (~~ strikethrough over blank Braille characters draws a continuous line;
# the box-drawing dashes it replaces rendered as broken segments on phones.)
DIV = "~~" + "\u2800" * 25 + "~~"
SECTION_SEP = f"\n{DIV}\n"


def add_footer_divider(embed, minimum_length=280):
    """Add the shared divider before footer metadata on medium/long embeds."""
    description = embed.description or ""
    fields_length = sum(len(field.name or "") + len(field.value or "") for field in embed.fields)
    if len(description) + fields_length >= minimum_length and description and not description.endswith(DIV):
        embed.description = description.rstrip() + f"\n{DIV}"
    return embed
BRAND = "AbyssalNoob1 is the Best"
EMBED_CHUNK = 2600  # keeps two embeds safely under Discord's 6000-character message cap
_PART = "\x00PART\x00"

def build_embed(name, info, guild=None):
    start, end = info["start_time"], info["end_time"]
    host, cohost = info["host_id"], info.get("cohost_id")
    res = info["reservations"]
    wait = info.get("waitlist", [])
    sps = info.get("superpowers", [])
    helpers = info.get("helpers", [])
    max_p = info.get("max_players", 0)
    now = int(time.time())

    sp_set = {s.title() for s in sps}
    sp_lines, reg_lines = [], []
    for country, u in res.items():
        uid = u["discord_id"]
        line = f"• **{country}** · `{u['roblox']}` · <@{uid}>"
        (sp_lines if country.title() in sp_set else reg_lines).append(line)

    if info.get("concluded"):
        emoji, color, status = "🔒", 0xE74C3C, "🔒 Concluded"
    elif now >= end:
        emoji, color, status = "⚔️", 0xF1C40F, f"⚔️ Live · reservations closed <t:{end}:R>"
    elif now < start:
        emoji, color, status = "🕒", 0x3498DB, f"🕒 Opens <t:{start}:R>"
    else:
        emoji, color, status = "🚩", 0x2ECC71, f"🟢 Open · closes <t:{end}:R>"

    # Headings (not the embed title) so the name and status render large.
    h_str = f"<@{host}>" + (f" & <@{cohost}>" if cohost else "")
    head = (f"# {emoji} {name}{_PART}\n## {status}{SECTION_SEP}"
            f"### 🗓️ Schedule\n• **Starts:** <t:{start}:f>\n• **Ends:** <t:{end}:f>\n• **Host:** {h_str}")
    if info.get("trial"):
        head += "\n• **🎓 Trial event:** players can leave feedback on this trial host"
    if helpers:
        head += "\n• **Helpers:** " + ", ".join(f"<@{i}>" for i in helpers[:15])
    head += SECTION_SEP

    sp_sec = ""
    if sps:
        unclaimed = [s for s in sps if s.title() not in res]
        body = "\n".join(sp_lines)
        if unclaimed:
            body += ("\n" if body else "") + "*Available:* " + ", ".join(f"**{s}**" for s in unclaimed)
        sp_sec = f"### ⭐ Superpowers\n{body}{SECTION_SEP}"

    wait_sec = ""
    if wait:
        w_lines = [f"• **{w['country']}** · <@{w['discord_id']}>" for w in wait[:5]]
        more = f"\n*+{len(wait) - 5} more*" if len(wait) > 5 else ""
        wait_sec = f"### ⏳ Waitlist ({len(wait)})\n" + "\n".join(w_lines) + more

    chunks = []
    cur = head + sp_sec + f"### 📋 Reservations ({len(reg_lines)})\n"
    if not reg_lines:
        cur += "*No reservations yet*"
    shown = 0
    for ln in reg_lines:
        if len(cur) + len(ln) + 1 > EMBED_CHUNK:
            if chunks:  # max two embeds per message
                break
            chunks.append(cur.rstrip("\n"))
            cur = f"# {emoji} {name}{_PART}\n### 📋 Reservations (continued)\n"
        cur += ln + "\n"
        shown += 1
    if shown < len(reg_lines):
        cur += f"*…and {len(reg_lines) - shown} more*\n"
    cur = cur.rstrip("\n")
    if wait_sec:
        if len(cur) + len(wait_sec) + 2 > EMBED_CHUNK and not chunks:
            chunks.append(cur)
            cur = f"# {emoji} {name}{_PART}\n" + wait_sec
        else:
            cur += "\n\n" + wait_sec
    chunks.append(cur + SECTION_SEP.rstrip("\n"))  # closing line above the footer
    chunks = [c[:EMBED_CHUNK + 250] for c in chunks]

    n = len(chunks)
    embs = []
    for i, d in enumerate(chunks):
        d = d.replace(_PART, f" · Part {i + 1}/{n}" if n > 1 else "")
        embs.append(discord.Embed(color=color, description=d))
    add_footer_divider(embs[-1])
    embs[-1].set_footer(text=f"{len(res)}{f' / {max_p}' if max_p > 0 else ''} reservations · updated live · {BRAND}")
    return embs

# ==========================================
# PER-EVENT LOG EMBED (one live embed per event; the category dropdown + page buttons edit it in place)
# ==========================================
LOG_CATS = {
    "reservation": ("📋", "Reservations"),
    "waitlist": ("⏳", "Waitlist"),
    "swap": ("🔄", "Swaps"),
    "host": ("🛠️", "Host actions"),
}
LOG_OPTIONS = [("all", "All activity", "📜"), ("reservation", "Reservations", "📋"),
               ("waitlist", "Waitlist", "⏳"), ("swap", "Swaps", "🔄"), ("host", "Host actions", "🛠️")]
LOG_BLUE = 0x3498DB
LOG_PAGE_SIZE = 5

def _fmt_ts(t):
    # Time only for recent entries, date + time once they're older than about 20 hours.
    return f"<t:{t}:d> <t:{t}:t>" if time.time() - t > 72000 else f"<t:{t}:t>"

def _log_line(e):
    emoji = LOG_CATS.get(e["c"], ("•", ""))[0]
    return f"{emoji} {_fmt_ts(e['t'])} · {e['m']}"

def log_state(info):
    v = info.get("log_view") or {}
    cat = v.get("cat", "all")
    if cat != "all" and cat not in LOG_CATS:
        cat = "all"
    rows = list(reversed([e for e in info.get("logs", []) if cat == "all" or e["c"] == cat]))
    pages = max(1, -(-len(rows) // LOG_PAGE_SIZE))
    try:
        page = int(v.get("page", 0))
    except (TypeError, ValueError):
        page = 0
    return cat, max(0, min(page, pages - 1)), pages, rows

def build_log_embed(info):
    cat, page, pages, rows = log_state(info)
    logs = info.get("logs", [])
    counts = {c: sum(1 for e in logs if e["c"] == c) for c in LOG_CATS}
    host = f"👑 **Host** · <@{info['host_id']}>" + (f" & <@{info['cohost_id']}>" if info.get("cohost_id") else "")
    stats = "\n".join(f"{LOG_CATS[c][0]} **{LOG_CATS[c][1]}** · {counts[c]}" for c in LOG_CATS)
    sec = "🕒 Latest activity" if cat == "all" else f"{LOG_CATS[cat][0]} {LOG_CATS[cat][1]}"
    body = "\n".join(_log_line(e) for e in rows[page * LOG_PAGE_SIZE:(page + 1) * LOG_PAGE_SIZE]) or "*Nothing here yet*"
    desc = (f"# 📜 Event Log · {info['display_name']}\n{DIV}\n"
            f"### 📊 Overview\n{host}\n{stats}\n{DIV}\n"
            f"### {sec}\n{body}")
    if info.get("helpers"):
        desc += f"\n{DIV}\n### 🤝 Helpers\n" + ", ".join(f"<@{i}>" for i in info["helpers"][:20])
    desc += f"\n{DIV}"
    emb = discord.Embed(color=LOG_BLUE, description=desc[:4000])
    state = "🔒 Concluded" if info.get("concluded") else "🟢 Live"
    add_footer_divider(emb)
    emb.set_footer(text=f"{len(logs)} entries · {state} · Page {page + 1}/{pages}")
    return emb

def find_event_by_log(msg_id):
    for info in db["events"].values():
        if (info.get("log_msg") or {}).get("id") == msg_id:
            return info
    return None

class LogCatSelect(discord.ui.Select):
    def __init__(self, cur="all"):
        opts = [discord.SelectOption(label=l, value=v, emoji=e, default=(v == cur)) for v, l, e in LOG_OPTIONS]
        super().__init__(custom_id="evlog_select", placeholder="Browse logs by category", options=opts, row=0)

    async def callback(self, interaction):
        info = find_event_by_log(interaction.message.id)
        if not info:
            await interaction.response.send_message("This event log is no longer available.", ephemeral=True)
            return
        info["log_view"] = {"cat": self.values[0], "page": 0}
        save_db(db)
        await interaction.response.edit_message(embed=build_log_embed(info), view=make_log_view(info))

class LogPageButton(discord.ui.Button):
    def __init__(self, direction, disabled=True):
        super().__init__(label="◀ Prev" if direction < 0 else "Next ▶", style=discord.ButtonStyle.secondary,
                         custom_id="evlog_prev" if direction < 0 else "evlog_next", disabled=disabled, row=1)
        self.direction = direction

    async def callback(self, interaction):
        info = find_event_by_log(interaction.message.id)
        if not info:
            await interaction.response.send_message("This event log is no longer available.", ephemeral=True)
            return
        cat, page, pages, _ = log_state(info)
        info["log_view"] = {"cat": cat, "page": max(0, min(pages - 1, page + self.direction))}
        save_db(db)
        await interaction.response.edit_message(embed=build_log_embed(info), view=make_log_view(info))

class GuardedView(discord.ui.View):
    """Base view that disables every button/select in disabled servers."""
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return not await disabled_guild_response(interaction)


class EventLogView(GuardedView):
    def __init__(self, cat="all", page=0, pages=1):
        super().__init__(timeout=None)
        self.add_item(LogCatSelect(cat))
        self.add_item(LogPageButton(-1, disabled=page <= 0))
        self.add_item(LogPageButton(+1, disabled=page >= pages - 1))

def make_log_view(info):
    cat, page, pages, _ = log_state(info)
    return EventLogView(cat, page, pages)

async def refresh_log_message(info):
    lm = info.get("log_msg")
    if not lm:
        return False
    ch = bot.get_channel(lm["ch"])
    if not ch:
        return False
    try:
        m = await ch.fetch_message(lm["id"])
        await m.edit(embed=build_log_embed(info), view=make_log_view(info))
        return True
    except discord.NotFound:
        info.pop("log_msg", None)  # message was deleted: respawn on the next entry
        save_db(db)
        return False
    except discord.HTTPException as e:
        print("refresh_log_message failed:", e)
        return False

async def spawn_event_log(guild, info):
    log_id = get_config(guild.id).get("log_channel")
    ch = guild.get_channel(log_id) if log_id else None
    if not ch:
        return False
    try:
        m = await ch.send(embed=build_log_embed(info), view=make_log_view(info))
        info["log_msg"] = {"ch": ch.id, "id": m.id}
        save_db(db)
        return True
    except discord.HTTPException as e:
        print("spawn_event_log failed:", e)
        return False

async def event_log(guild, info, cat, text):
    """Record an entry on the event and update (or create) its log embed."""
    logs = info.setdefault("logs", [])
    logs.append({"t": int(time.time()), "c": cat, "m": str(text)[:300]})
    if len(logs) > 500:
        del logs[:-500]
    save_db(db)
    if info.get("log_msg"):
        await refresh_log_message(info)
    if not info.get("log_msg"):
        await spawn_event_log(guild, info)

async def process_waitlist(info, opened, guild, channel):
    wait = info.get("waitlist", [])
    if not wait: 
        return

    res = info.get("reservations", {})
    max_p = info.get("max_players", 0)
    if max_p > 0 and len(res) >= max_p: 
        return

    banned = db["blacklists"].get(str(guild.id), [])
    sp_list = [s.title() for s in info.get("superpowers", [])]
    holders = {u["discord_id"] for u in res.values()}

    cands = []
    for w in list(wait):
        uid = w["discord_id"]
        c_name = w["country"].title()

        # Drop banned users and anyone who already holds a spot (prevents double spots).
        if uid in banned or uid in holders:
            wait.remove(w)
            continue

        if c_name in res:
            continue

        if c_name in sp_list and not is_vip(guild.get_member(uid), guild.id):
            continue

        cands.append(w)

    if not cands: 
        return

    promo = next((i for i in cands if i["country"].title() == opened.title()), cands[0])
    wait.remove(promo)

    info["reservations"][promo["country"].title()] = {"roblox": promo["roblox"], "discord_id": promo["discord_id"]}
    info["waitlist_fills"] = int(info.get("waitlist_fills", 0)) + 1
    await event_log(guild, info, "waitlist", f"<@{promo['discord_id']}> got **{promo['country'].title()}** from the waitlist")

    await add_event_role(guild, promo["discord_id"])

    if channel:
        try: 
            await channel.send(f"🔔 <@{promo['discord_id']}> **{promo['country'].title()}** opened up and you got it!")
        except discord.HTTPException: 
            pass
