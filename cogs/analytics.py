# cogs/analytics.py
import discord
from discord import app_commands
from discord.ext import commands
import time
from database import db, save_db, get_config

BOT_OWNER_IDS = [1359263076838539296, 1298335881647620108]
ALLOWED_USER_IDS = [1359263076838539296]
DIV = "~~" + "\u2800" * 25 + "~~"

ACHIEVEMENT_DEFS = {
    "first_steps": {"name": "First Steps", "emoji": "🥉", "tier": "Common", "points": 50},
    "getting_started": {"name": "Getting Started", "emoji": "🥈", "tier": "Common", "points": 75},
    "experienced": {"name": "Experienced", "emoji": "🥇", "tier": "Rare", "points": 150},
    "event_machine": {"name": "Event Machine", "emoji": "💎", "tier": "Epic", "points": 300},
    "event_master": {"name": "Event Master", "emoji": "👑", "tier": "Epic", "points": 500},
    "event_legend": {"name": "Event Legend", "emoji": "🔥", "tier": "Legendary", "points": 1000},
    "popular": {"name": "Popular", "emoji": "👥", "tier": "Rare", "points": 200},
    "community_builder": {"name": "Community Builder", "emoji": "🏗️", "tier": "Epic", "points": 400},
    "community_idol": {"name": "Community Idol", "emoji": "🌟", "tier": "Legendary", "points": 1000},
    "sold_out": {"name": "Sold Out", "emoji": "🎟️", "tier": "Rare", "points": 200},
    "packed_house": {"name": "Packed House", "emoji": "🏟️", "tier": "Epic", "points": 500},
    "the_marathon": {"name": "The Marathon", "emoji": "🏃", "tier": "Rare", "points": 250},
    "flawless": {"name": "Flawless", "emoji": "✨", "tier": "Legendary", "points": 750},
    "reliable": {"name": "Reliable", "emoji": "🛡️", "tier": "Rare", "points": 300},
    "master_negotiator": {"name": "Master Negotiator", "emoji": "🔄", "tier": "Epic", "points": 500},
    "world_host": {"name": "World Host", "emoji": "🌎", "tier": "Epic", "points": 400},
    "forty_deep": {"name": "40 Deep", "emoji": "🗿", "tier": "Mythic", "points": 1500, "secret": True},
    "waitlist_hero": {"name": "Waitlist Hero", "emoji": "⏳", "tier": "Rare", "points": 300, "secret": True},
    "triple_threat": {"name": "Triple Threat", "emoji": "⚡", "tier": "Epic", "points": 600, "secret": True},
    "perfect_run": {"name": "Perfect Run", "emoji": "💠", "tier": "Legendary", "points": 1000, "secret": True},
    "big_game": {"name": "Big Game", "emoji": "🏆", "tier": "Legendary", "points": 900, "secret": True},
    "veteran_ground": {"name": "Veteran Ground", "emoji": "🗺️", "tier": "Rare", "points": 350, "secret": True},
    "full_house_five": {"name": "Five Full Houses", "emoji": "🎪", "tier": "Mythic", "points": 1800, "secret": True},
    "waitlist_savior": {"name": "Waitlist Savior", "emoji": "🛟", "tier": "Legendary", "points": 1200, "secret": True},
}

ACHIEVEMENT_ORDER = list(ACHIEVEMENT_DEFS)

def is_trusted(member):
    if member.id in BOT_OWNER_IDS or member.id in ALLOWED_USER_IDS:
        return True
    guild = getattr(member, "guild", None)
    if guild is None:
        return False
    return member.id in db.get("trusted_users", {}).get(str(guild.id), [])

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

def get_achievement_state(guild_id, host_id):
    gid, hid = str(guild_id), str(host_id)
    states = db.setdefault("achievement_progress", {})
    server = states.setdefault(gid, {})
    return server.setdefault(hid, {"unlocked": [], "unlocked_at": {}})

def _achievement_points(unlocked):
    return sum(ACHIEVEMENT_DEFS[aid]["points"] for aid in unlocked if aid in ACHIEVEMENT_DEFS)

def host_level_from_points(points):
    thresholds = [0, 250, 750, 1500, 2750, 4500, 7000, 10000, 14000, 19000, 25000]
    level = 1
    for i, threshold in enumerate(thresholds, 1):
        if points >= threshold:
            level = i
        else:
            break
    current = thresholds[level - 1]
    nxt = thresholds[level] if level < len(thresholds) else current + 10000
    progress = 1.0 if nxt <= current else max(0.0, min(1.0, (points - current) / (nxt - current)))
    return level, current, nxt, progress

def progress_bar(progress, size=12):
    filled = max(0, min(size, round(progress * size)))
    return "▰" * filled + "▱" * (size - filled)

def add_footer_divider(embed, minimum_length=280):
    description = embed.description or ""
    fields_length = sum(len(field.name or "") + len(field.value or "") for field in embed.fields)
    if len(description) + fields_length >= minimum_length and description and not description.endswith(DIV):
        embed.description = description.rstrip() + f"\n{DIV}"
    return embed

def build_host_profile_embed(guild, member):
    stats = db.get("hosts", {}).get(str(member.id), {})
    total_events = int(stats.get("total_events", 0) or 0)
    total_players = int(stats.get("total_players", 0) or 0)
    ratings = []
    for value in stats.get("ratings", []) or []:
        try:
            ratings.append(float(value))
        except (TypeError, ValueError):
            pass

    average_rating = (sum(ratings) / len(ratings)) if ratings else 0.0
    state = get_achievement_state(guild.id, member.id)
    unlocked = set(state.get("unlocked", []))
    points = _achievement_points(unlocked)
    level, level_floor, level_next, level_progress = host_level_from_points(points)

    embed = discord.Embed(
        title=f"🎖️ {member.display_name} · Host Profile",
        description=(f"<@{member.id}>\n\n"
                     f"**Level {level} Host** · {points:,} achievement points\n"
                     f"`{progress_bar(level_progress)}` **{round(level_progress * 100)}%**\n"
                     f"{points - level_floor:,} / {level_next - level_floor:,} to Level {level + 1}"),
        color=0x5865F2
    )
    if member.display_avatar:
        embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(name="📅 Events Hosted", value=f"**{total_events:,}**", inline=True)
    embed.add_field(name="👥 Players Hosted", value=f"**{total_players:,}**", inline=True)
    embed.add_field(name="⭐ Average Rating", value=f"**{average_rating:.2f}/5**" if ratings else "**No ratings yet**", inline=True)
    embed.add_field(
        name="🏆 Achievements",
        value=f"**{len(unlocked)}/{len(ACHIEVEMENT_DEFS)}** unlocked\n**{points:,}** points",
        inline=True
    )
    embed.add_field(name="📝 Ratings Received", value=f"**{len(ratings):,}**", inline=True)
    embed.add_field(name="🎯 Host Status", value="**Active Host**" if total_events else "**New Host**", inline=True)
    add_footer_divider(embed)
    embed.set_footer(text=f"Host Assistant · {guild.name}")
    return embed

def event_player_count(event):
    return int(event.get("player_count", len(event.get("participants", [])) or len(event.get("reservations", {}))))

def event_was_full(event):
    cap = int(event.get("max_players", 0) or 0)
    return cap > 0 and event_player_count(event) >= cap

class AnalyticsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="host", description="View a host's profile")
    @app_commands.guild_only()
    async def host_profile_cmd(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        await interaction.response.send_message(embed=build_host_profile_embed(interaction.guild, member), ephemeral=True)

    @app_commands.command(name="me", description="View your Host Assistant profile")
    @app_commands.guild_only()
    async def me_profile_cmd(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=build_host_profile_embed(interaction.guild, interaction.user), ephemeral=True)

    @app_commands.command(name="achievements", description="View a host's achievement collection")
    @app_commands.guild_only()
    async def achievements_cmd(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        state = get_achievement_state(interaction.guild_id, member.id)
        unlocked = set(state.get("unlocked", []))
        lines = []
        for aid in ACHIEVEMENT_ORDER:
            d = ACHIEVEMENT_DEFS[aid]
            if aid in unlocked:
                lines.append(f"{d['emoji']} **{d['name']}** · {d['tier']} · +{d['points']} pts")
            elif d.get("secret"):
                lines.append("🔒 **Secret Achievement** · *Undiscovered*")
            else:
                lines.append(f"🔲 **{d['name']}** · {d['tier']}")
        points = _achievement_points(unlocked)
        level, _, _, level_progress = host_level_from_points(points)
        emb = discord.Embed(
            title=f"🏆 {member.display_name} · Achievement Collection",
            description=(f"**Level {level} Host** · {points:,} points\n"
                         f"`{progress_bar(level_progress)}` **{round(level_progress * 100)}%**\n\n"
                         + "\n".join(lines))[:4000],
            color=0xF1C40F
        )
        add_footer_divider(emb)
        emb.set_footer(text=f"{len(unlocked)}/{len(ACHIEVEMENT_DEFS)} unlocked · Host Assistant")
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @app_commands.command(name="analytics", description="View real event and host analytics")
    @app_commands.guild_only()
    @app_commands.choices(period=[
        app_commands.Choice(name="Last 7 Days", value="7"),
        app_commands.Choice(name="Last 30 Days", value="30"),
        app_commands.Choice(name="Last 90 Days", value="90"),
        app_commands.Choice(name="All Time", value="0"),
    ])
    async def analytics_cmd(self, interaction: discord.Interaction, period: app_commands.Choice[str] = None):
        if not is_authorized_manager(interaction.user):
            await interaction.response.send_message("Only hosts and admins can use /analytics.", ephemeral=True)
            return

        days = int(period.value) if period else 30
        cutoff = 0 if days == 0 else time.time() - days * 86400
        period_keys = {k for k, e in db["events"].items()
                       if e.get("guild_id") == interaction.guild_id and e.get("concluded") and e.get("concluded_at", 0) >= cutoff}
        events = [db["events"][k] for k in period_keys]
        total_events = len(events)
        total_players = sum(event_player_count(e) for e in events)
        full = sum(1 for e in events if event_was_full(e))
        ratings = []
        for h in db.get("hosts", {}).values():
            for r in h.get("rating_history", []):
                if r.get("event_key") in period_keys:
                    try:
                        ratings.append(float(r.get("rating", 0)))
                    except (TypeError, ValueError):
                        pass
        avg_rating = sum(ratings) / len(ratings) if ratings else 0.0
        avg_players = total_players / total_events if total_events else 0
        occupancy = [min(100, event_player_count(e) / int(e.get("max_players", 0)) * 100)
                     for e in events if int(e.get("max_players", 0) or 0) > 0]
        avg_occupancy = sum(occupancy) / len(occupancy) if occupancy else 0

        per_host = {}
        for e in events:
            row = per_host.setdefault(e.get("host_id"), [0, 0])
            row[0] += 1
            row[1] += event_player_count(e)
        host_rows = sorted(per_host.items(), key=lambda kv: (kv[1][0], kv[1][1]), reverse=True)

        emb = discord.Embed(title="📊 Host Assistant · Analytics", color=0x3498DB)
        emb.description = f"**Period:** {'All Time' if days == 0 else f'Last {days} Days'}\n{DIV}"
        emb.add_field(name="🎪 Events", value=f"**{total_events}**", inline=True)
        emb.add_field(name="👥 Players", value=f"**{total_players:,}**", inline=True)
        emb.add_field(name="⭐ Avg Rating", value=f"**{avg_rating:.2f}/5**" if ratings else "**N/A**", inline=True)
        emb.add_field(name="📈 Avg Players/Event", value=f"**{avg_players:.1f}**", inline=True)
        emb.add_field(name="🏟️ Full Events", value=f"**{full}**", inline=True)
        emb.add_field(name="📊 Avg Occupancy", value=f"**{avg_occupancy:.1f}%**" if occupancy else "**N/A**", inline=True)
        top = [f"**{i}.** <@{hid}> · {ev} events · {pl} players" for i, (hid, (ev, pl)) in enumerate(host_rows[:5], 1)]
        emb.add_field(name="🔥 Top Hosts", value="\n".join(top) or "No completed events in this period.", inline=False)
        add_footer_divider(emb)
        emb.set_footer(text="Historical analytics are calculated from completed event records.")
        await interaction.response.send_message(embed=emb, ephemeral=True)

async def setup(bot):
    await bot.add_cog(AnalyticsCog(bot))
