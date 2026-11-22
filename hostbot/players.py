"""Player profiles: /player shows what someone has played, their streaks, favourite nation and host."""

import collections
import datetime
import discord
import time
from discord import app_commands

from hostbot.core import bot, db
from hostbot.events import WARN_LIMIT, WARN_WINDOW_DAYS, active_warns
from hostbot.routines import counted_events, event_players
from hostbot.ui import COLORS, card, divider, footnote, msg, notice, section, text, thumb


def _day(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date()


def _streaks(days):
    """(current, best) run of consecutive UTC days with at least one event played."""
    if not days:
        return 0, 0
    days = sorted(set(days))
    best = run = 1
    for a, b in zip(days, days[1:]):
        run = run + 1 if (b - a).days == 1 else 1
        best = max(best, run)
    today = _day(time.time())
    current = run if (today - days[-1]).days <= 1 else 0
    return current, best


def build_player_profile_card(guild, member):
    """The player profile in the same layout as a rank card: avatar, headline stat, then the details."""
    uid = member.id
    events = sorted((e for e in counted_events(guild.id) if uid in event_players(e)),
                    key=lambda e: e.get("concluded_at", 0))
    helped = sum(1 for e in counted_events(guild.id) if uid in {int(i) for i in e.get("helpers", [])})
    warns = len(active_warns(guild.id, uid))
    blacklisted = uid in db["blacklists"].get(str(guild.id), [])
    avatar = member.display_avatar.url if member.display_avatar else None
    warn_line = f"⚠️ **{warns}/{WARN_LIMIT}** warnings · last {WARN_WINDOW_DAYS} days"
    status_line = "⛔ **Blacklisted from events**" if blacklisted else None

    if not events:
        return card(COLORS["brand"],
                    section([f"### {member.display_name}", "**No events played yet**",
                             "-# Claim a spot in the next one!"], thumb(avatar)),
                    divider(),
                    text(warn_line, status_line),
                    footnote("Join events to build your profile and your 🔥 streak"))

    month_ago = time.time() - 30 * 86400
    recent = sum(1 for e in events if e.get("concluded_at", 0) >= month_ago)
    current, best = _streaks([_day(e["concluded_at"]) for e in events if e.get("concluded_at")])

    nations = collections.Counter()
    superpower_games = 0
    for e in events:
        sps = {s.title() for s in e.get("superpowers", [])}
        for country, u in e.get("reservations", {}).items():
            if int(u["discord_id"]) == uid:
                nations[country] += 1
                if country.title() in sps:
                    superpower_games += 1
    hosts = collections.Counter(e.get("host_id") for e in events if e.get("host_id") != uid)
    first = events[0].get("concluded_at", 0)
    last = events[-1].get("concluded_at", 0)

    lines = [
        f"🔥 **{current}** day streak" + (f" · best {best}" if best > current else " · personal best!" if current else ""),
        f"⭐ **{superpower_games}** superpower game{'s' if superpower_games != 1 else ''}"
        f" · 🤝 helped **{helped}** time{'s' if helped != 1 else ''}",
    ]
    if nations:
        (nation, n), = nations.most_common(1)
        lines.append(f"🌍 Favourite nation **{nation}** · {n}×")
    if hosts:
        (host, n), = hosts.most_common(1)
        lines.append(f"🎤 Favourite host <@{host}> · {n} event{'s' if n != 1 else ''}")
    lines += [warn_line, status_line]

    return card(COLORS["brand"],
                section([f"### {member.display_name}",
                         f"**{len(events)} event{'s' if len(events) != 1 else ''} played** · {recent} in the last 30 days",
                         f"-# Playing since <t:{first}:D> · last game <t:{last}:R>"], thumb(avatar)),
                divider(),
                text(lines),
                footnote("Join events to grow your 🔥 streak"))


@bot.tree.command(name="player", description="View a player's event profile")
@app_commands.guild_only()
@app_commands.describe(member="Whose profile to show (leave empty for your own)")
async def player_profile_cmd(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    if member.bot:
        await interaction.response.send_message(**notice("warn", "🤖 Bots don't play events."), ephemeral=True)
        return
    await interaction.response.send_message(**msg(build_player_profile_card(interaction.guild, member)), ephemeral=True)
