"""Background timers, setup_hook, on_ready, on_guild_join, !sync."""

import asyncio
import discord
import time
from discord.ext import commands
from discord.ext import tasks

from hostbot.ai import process_ai_queue
from hostbot.core import (
    _bg_tasks, ai_history, bot, db, enforce_existing_guilds_safely, get_config, save_db,
    schedule_whitelist_enforcement, swap_cooldowns, user_cooldowns,
)
from hostbot.events import EventButtonsView, RateBtnView
from hostbot.hosts import TrialLogView, TrialVoteView, close_due_trials
from hostbot.routines import cleanup_old_events, post_weekly_summaries, send_due_reminders
from hostbot.server_tools import _talk_cooldowns, _talk_pings
from hostbot.shared import EventLogView, is_server_admin, refresh_event_message
from hostbot import core

# Last phase ("open"/"live") we rendered per event, so we only edit when it flips.
event_phase = {}
event_refresh_fails = {}

async def clear_participant_roles(info):
    """After the rating hour: take the Event Participant role from the people who played,
    unless they are in another event that is still running."""
    guild = bot.get_guild(int(info.get("guild_id") or 0))
    if not guild:
        return
    r_id = get_config(guild.id).get("event_role")
    role = guild.get_role(r_id) if r_id else None
    if not role:
        return
    still_playing = set()
    for e in db["events"].values():
        if e is not info and not e.get("concluded") and e.get("guild_id") == guild.id:
            still_playing |= {int(u["discord_id"]) for u in e.get("reservations", {}).values()}
    for uid in info.get("participants", []):
        if int(uid) in still_playing:
            continue
        mem = guild.get_member(int(uid))
        if mem and role in mem.roles:
            try:
                await mem.remove_roles(role, reason="Rating window over")
            except discord.HTTPException:
                pass
            await asyncio.sleep(0.3)


@tasks.loop(seconds=30)
async def refresh_timers():
    now = int(time.time())

    for key, info in list(db["events"].items()):
        try:
            if info.get("concluded"):
                # Events concluded after this update carry role_cleared=False until the hour is up.
                if info.get("role_cleared") is False and now >= info.get("role_clear_at", 0):
                    await clear_participant_roles(info)
                    info["role_cleared"] = True
                    save_db(db)
                event_phase.pop(key, None)
                event_refresh_fails.pop(key, None)
                continue

            phase = "live" if now >= info["end_time"] else "open"
            if event_phase.get(key) == phase:
                continue  # Countdown timestamps update client-side; nothing to edit.

            if await refresh_event_message(info):
                event_phase[key] = phase
                event_refresh_fails.pop(key, None)
            else:
                # Message deleted or channel gone: give up after a few tries instead of retrying forever.
                event_refresh_fails[key] = event_refresh_fails.get(key, 0) + 1
                if event_refresh_fails[key] >= 3:
                    event_phase[key] = phase
        except Exception as e:
            print("refresh_timers error:", e)

    try:
        await close_due_trials()
    except Exception as e:
        print("close_due_trials error:", e)

    for job in (send_due_reminders, post_weekly_summaries):
        try:
            await job()
        except Exception as e:
            print(f"{job.__name__} error:", e)
    try:
        cleanup_old_events()
    except Exception as e:
        print("cleanup_old_events error:", e)

    # Housekeeping so these dicts don't grow forever.
    cutoff = time.time() - 3600
    for d in (user_cooldowns, swap_cooldowns):
        for k in [k for k, v in d.items() if v < cutoff]:
            del d[k]
    while len(ai_history) > 500:
        ai_history.pop(next(iter(ai_history)))
    for uid in [uid for uid, ts in _talk_cooldowns.items() if ts < cutoff]:
        _talk_cooldowns.pop(uid, None)
    for uid in [uid for uid, ts in _talk_pings.items() if not ts or ts[-1] < cutoff]:
        _talk_pings.pop(uid, None)

@refresh_timers.before_loop
async def before_refresh_timers():
    await bot.wait_until_ready()

async def setup_hook():
    bot.add_view(EventButtonsView())
    bot.add_view(EventLogView())
    bot.add_view(RateBtnView())
    bot.add_view(TrialVoteView())
    bot.add_view(TrialLogView())
    refresh_timers.start()
    core.ai_worker_task = asyncio.create_task(process_ai_queue())

bot.setup_hook = setup_hook

@bot.event
async def on_guild_join(guild):
    # Discord necessarily fires this after the bot joins, so this is the earliest
    # possible enforcement point. Non-whitelisted guilds are cleaned up and left immediately.
    schedule_whitelist_enforcement(guild)


_ready_once = False

@bot.event
async def on_ready():
    global _ready_once
    print(f"Logged in: {bot.user.name}")
    print("Connected guilds:", [(g.name, g.id) for g in bot.guilds])
    # Do NOT mass-leave guilds during on_ready. That can burst Discord DELETE
    # requests and trigger a 429/Cloudflare rate limit. Clean them up slowly in
    # the background while keeping the bot online. New joins are handled immediately.
    if not _ready_once:  # on_ready can fire again after a reconnect
        _ready_once = True
        task = asyncio.create_task(enforce_existing_guilds_safely())
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
    try:
        await bot.change_presence(status=discord.Status.online,
                                  activity=discord.Game(name="events · hosting · /talk"))
    except Exception as e:
        print("presence failed:", e)

@bot.command()
@commands.guild_only()
async def sync(ctx):
    if not is_server_admin(ctx.author):
        await ctx.send("Admins only.", delete_after=10)
        return
    s = await bot.tree.sync()
    await ctx.send(f"Synced {len(s)} commands.", delete_after=10)
