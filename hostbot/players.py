"""Player profiles: /player shows what someone has played, their streaks, favourite nation and host."""

import collections
import datetime
import discord
import time
from discord import app_commands

from hostbot.core import bot
from hostbot.routines import counted_events, event_players
from hostbot.shared import BRAND, add_footer_divider


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


def build_player_profile_embed(guild, member):
    uid = member.id
    events = sorted((e for e in counted_events(guild.id) if uid in event_players(e)),
                    key=lambda e: e.get("concluded_at", 0))
    helped = sum(1 for e in counted_events(guild.id) if uid in {int(i) for i in e.get("helpers", [])})

    emb = discord.Embed(title=f"🎮 {member.display_name} · Player Profile", color=0x57F287)
    if member.display_avatar:
        emb.set_thumbnail(url=member.display_avatar.url)

    if not events:
        emb.description = f"<@{uid}>\n\nNo events played yet. Claim a spot in the next one!"
        add_footer_divider(emb)
        emb.set_footer(text=BRAND)
        return emb

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
    emb.description = f"<@{uid}>\n\nPlaying since <t:{first}:D> · last game <t:{events[-1].get('concluded_at', 0)}:R>"
    emb.add_field(name="🎪 Events Played", value=f"**{len(events)}**", inline=True)
    emb.add_field(name="📅 Last 30 Days", value=f"**{recent}**", inline=True)
    emb.add_field(name="🤝 Times Helped", value=f"**{helped}**", inline=True)
    emb.add_field(name="🔥 Current Streak", value=f"**{current} day{'s' if current != 1 else ''}**", inline=True)
    emb.add_field(name="🏅 Best Streak", value=f"**{best} day{'s' if best != 1 else ''}**", inline=True)
    emb.add_field(name="⭐ Superpower Games", value=f"**{superpower_games}**", inline=True)
    if nations:
        (nation, n), = nations.most_common(1)
        emb.add_field(name="🌍 Favourite Nation", value=f"**{nation}** · {n}×", inline=True)
    if hosts:
        (host, n), = hosts.most_common(1)
        emb.add_field(name="🎤 Favourite Host", value=f"<@{host}> · {n} events", inline=True)
    add_footer_divider(emb)
    emb.set_footer(text=BRAND)
    return emb


@bot.tree.command(name="player", description="View a player's event profile")
@app_commands.guild_only()
@app_commands.describe(member="Whose profile to show (leave empty for your own)")
async def player_profile_cmd(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    await interaction.response.send_message(embed=build_player_profile_embed(interaction.guild, member),
                                            ephemeral=True)
