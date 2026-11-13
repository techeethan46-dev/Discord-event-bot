"""Things the background timer runs on its own: event reminders, the weekly summary, and old-event cleanup."""

import datetime
import discord
import random
import time

from hostbot.core import bot, db, get_config, save_db
from hostbot.hosts import event_player_count
from hostbot.shared import BRAND, DIV, add_footer_divider

# ==========================================
# EVENT REMINDERS
# ==========================================
REMINDER_SECONDS = 120  # ping players this long before the event goes live

# One of these is picked at random. {ping} = the people to ping, {name} = event name,
# {when} = a live countdown like "in 2 minutes".
REMINDER_LINES = [
    "{ping} hey guys, the event you reserved for, **{name}**, starts {when}!",
    "{ping} yo, **{name}** starts {when}, get ready 🎮",
    "{ping} heads up! **{name}** kicks off {when}, don't be late",
    "{ping} reminder: you've got a spot in **{name}** and it starts {when} 👀",
    "{ping} almost time! **{name}** begins {when}, see you there",
    "{ping} get your game open, **{name}** starts {when} ⚔️",
    "{ping} **{name}** is about to start ({when}), hop on and get ready",
]


def _reminder_ping(guild, info, channel):
    """Ping the Event Participant role when the bot is allowed to, otherwise ping the players directly."""
    rid = get_config(guild.id).get("event_role")
    role = guild.get_role(rid) if rid else None
    if role and (role.mentionable or channel.permissions_for(guild.me).mention_everyone):
        return role.mention, discord.AllowedMentions(roles=[role], users=False, everyone=False)
    ids = list(dict.fromkeys(int(u["discord_id"]) for u in info.get("reservations", {}).values()))[:50]
    users = [discord.Object(id=i) for i in ids]
    return " ".join(f"<@{i}>" for i in ids), discord.AllowedMentions(users=users, roles=False, everyone=False)


async def send_due_reminders():
    now = int(time.time())
    for info in list(db["events"].values()):
        if info.get("concluded") or info.get("reminded") or not info.get("reservations"):
            continue
        live_at = info.get("end_time", 0)
        if not (live_at - REMINDER_SECONDS <= now < live_at) or now < info.get("start_time", 0):
            continue
        info["reminded"] = True  # mark first so a failed send is never retried into a spam loop
        save_db(db)
        guild = bot.get_guild(int(info.get("guild_id") or 0))
        ch = bot.get_channel(info.get("channel_id"))
        if not guild or not ch:
            continue
        ping, allowed = _reminder_ping(guild, info, ch)
        text = random.choice(REMINDER_LINES).format(ping=ping, name=info["display_name"], when=f"<t:{live_at}:R>")
        try:
            # The reminder only matters until the event starts, so it tidies itself up afterwards.
            await ch.send(text, allowed_mentions=allowed, delete_after=REMINDER_SECONDS + 600)
        except discord.HTTPException as e:
            print("event reminder failed:", e)


# ==========================================
# WEEKLY SUMMARY (posted every Monday, UTC, in the event log channel)
# ==========================================
def _week_start(ts):
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date()
    monday = d - datetime.timedelta(days=d.weekday())
    return int(datetime.datetime(monday.year, monday.month, monday.day, tzinfo=datetime.timezone.utc).timestamp())


def counted_events(guild_id):
    """Concluded, non-cancelled events of this server."""
    return [e for e in db["events"].values()
            if e.get("guild_id") == guild_id and e.get("concluded") and not e.get("cancelled")]


def event_players(e):
    ids = e.get("participants")
    if ids is None:
        ids = [u["discord_id"] for u in e.get("reservations", {}).values()]
    return {int(i) for i in ids}


def build_weekly_summary(guild, start, end):
    all_events = counted_events(guild.id)
    week = [e for e in all_events if start <= e.get("concluded_at", 0) < end]
    if not week:
        return None

    players = sum(event_player_count(e) for e in week)
    seen_before = set()
    for e in all_events:
        if e.get("concluded_at", 0) < start:
            seen_before |= event_players(e)
    played = {}
    for e in week:
        for uid in event_players(e):
            played[uid] = played.get(uid, 0) + 1
    new_players = [u for u in played if u not in seen_before]

    biggest = max(week, key=event_player_count)
    hosts = {}
    for e in week:
        row = hosts.setdefault(e.get("host_id"), [0, 0])
        row[0] += 1
        row[1] += event_player_count(e)
    top_host, (top_ev, top_pl) = max(hosts.items(), key=lambda kv: (kv[1][0], kv[1][1]))

    week_ids = {id(e) for e in week}
    keys = {k for k, e in db["events"].items() if id(e) in week_ids}
    by_event = {}
    for h in db.get("hosts", {}).values():
        for r in h.get("rating_history", []):
            if r.get("event_key") in keys:
                try:
                    by_event.setdefault(r["event_key"], []).append(float(r.get("rating", 0)))
                except (TypeError, ValueError):
                    pass
    best = None
    if by_event:
        k, vals = max(by_event.items(), key=lambda kv: (sum(kv[1]) / len(kv[1]), len(kv[1])))
        best = (db["events"][k], sum(vals) / len(vals), len(vals))

    regulars = sorted(((n, u) for u, n in played.items() if n > 1), reverse=True)[:3]

    emb = discord.Embed(color=0x5865F2, description=(
        f"## 📅 Weekly Summary\n"
        f"-# <t:{start}:D> → <t:{end - 1}:D>"))
    emb.add_field(name="🎪 Events", value=f"**{len(week)}**", inline=True)
    emb.add_field(name="👥 Players", value=f"**{players}**", inline=True)
    emb.add_field(name="📈 Avg / Event", value=f"**{players / len(week):.1f}**", inline=True)
    emb.add_field(name="🧍 Different Players", value=f"**{len(played)}**", inline=True)
    emb.add_field(name="🆕 New Players", value=f"**{len(new_players)}**", inline=True)
    emb.add_field(name="🔁 Returning", value=f"**{len(played) - len(new_players)}**", inline=True)
    emb.add_field(name="🏟️ Biggest Event",
                  value=f"**{biggest['display_name']}** · {event_player_count(biggest)} players · <@{biggest['host_id']}>",
                  inline=False)
    emb.add_field(name="🔥 Top Host",
                  value=f"<@{top_host}> · {top_ev} event{'s' if top_ev != 1 else ''} · {top_pl} players", inline=False)
    if best:
        e, avg, n = best
        emb.add_field(name="⭐ Best Rated Event",
                      value=f"**{e['display_name']}** · {avg:.2f}/5 from {n} rating{'s' if n != 1 else ''} · <@{e['host_id']}>",
                      inline=False)
    if regulars:
        emb.add_field(name="🎮 Most Active Players",
                      value="\n".join(f"<@{u}> · {n} events" for n, u in regulars), inline=False)
    add_footer_divider(emb)
    emb.set_footer(text=BRAND)
    return emb


async def post_weekly_summaries():
    now = int(time.time())
    this_week = _week_start(now)
    sent = db.setdefault("weekly_summary", {})
    for guild in bot.guilds:
        gid = str(guild.id)
        if gid not in sent:
            # First run after this feature was added: start with next Monday instead of posting right away.
            sent[gid] = this_week
            save_db(db)
            continue
        if sent[gid] >= this_week:
            continue
        sent[gid] = this_week
        save_db(db)
        log_id = get_config(guild.id).get("log_channel")
        ch = guild.get_channel(log_id) if log_id else None
        emb = build_weekly_summary(guild, this_week - 7 * 86400, this_week)
        if not ch or not emb:
            continue
        try:
            await ch.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            print("weekly summary failed:", e)


# ==========================================
# OLD EVENT CLEANUP (after 90 days, the same range as /analytics)
# ==========================================
CLEANUP_DAYS = 90
# What an old event keeps: just enough for host stats, achievements and player profiles.
_KEEP = {
    "display_name", "start_time", "end_time", "host_id", "cohost_id", "guild_id", "channel_id",
    "superpowers", "max_players", "concluded", "concluded_at", "completed", "cancelled", "trial",
    "player_count", "duration_seconds", "swaps_completed", "waitlist_fills", "helpers", "participants",
    "role_cleared",
}
_last_cleanup = 0


def cleanup_old_events():
    """Slim down concluded events older than 90 days (logs, waitlists, Roblox names are dropped)
    and delete events that were never concluded and ended more than 90 days ago."""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < 3600:  # once an hour is plenty
        return
    _last_cleanup = now
    cutoff = now - CLEANUP_DAYS * 86400
    changed = False
    for key, e in list(db["events"].items()):
        if e.get("concluded"):
            if e.get("archived") or e.get("concluded_at", now) >= cutoff:
                continue
            slim = {k: v for k, v in e.items() if k in _KEEP}
            slim["reservations"] = {c: {"discord_id": u["discord_id"]} for c, u in e.get("reservations", {}).items()}
            slim["role_cleared"] = True
            slim["archived"] = True
            db["events"][key] = slim
            changed = True
        elif e.get("end_time", now) < cutoff:
            del db["events"][key]  # abandoned: never concluded, never counted
            changed = True
    if changed:
        save_db(db)
