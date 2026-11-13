"""Host features: achievements, /host /me /achievements /analytics, server enable/disable, and the trial-host voting system."""

import asyncio
import collections
import datetime
import discord
import time
from discord import app_commands

from hostbot.core import BOT_OWNER_IDS, _bg_tasks, bot, db, get_config, is_trusted, save_db
from hostbot.shared import (
    BRAND, DIV, GuardedView, add_footer_divider, event_in_guild, is_authorized_manager,
    is_server_admin,
)

# ==========================================
# HOST ANALYTICS / PROFILES / ACHIEVEMENTS
# ==========================================
# Achievement progress is per server so a host can earn the same achievement
# independently in different communities. Roles are also per server.
ACHIEVEMENT_DEFS = {
    # Hosting progression
    "first_steps": {"name": "First Steps", "emoji": "🥉", "tier": "Common", "points": 50},
    "getting_started": {"name": "Getting Started", "emoji": "🥈", "tier": "Common", "points": 75},
    "experienced": {"name": "Experienced", "emoji": "🥇", "tier": "Rare", "points": 150},
    "event_machine": {"name": "Event Machine", "emoji": "💎", "tier": "Epic", "points": 300},
    "event_master": {"name": "Event Master", "emoji": "👑", "tier": "Epic", "points": 500},
    "event_legend": {"name": "Event Legend", "emoji": "🔥", "tier": "Legendary", "points": 1000},

    # Player/community achievements
    "popular": {"name": "Popular", "emoji": "👥", "tier": "Rare", "points": 200},
    "community_builder": {"name": "Community Builder", "emoji": "🏗️", "tier": "Epic", "points": 400},
    "community_idol": {"name": "Community Idol", "emoji": "🌟", "tier": "Legendary", "points": 1000},

    # Rare achievements
    "sold_out": {"name": "Sold Out", "emoji": "🎟️", "tier": "Rare", "points": 200},
    "packed_house": {"name": "Packed House", "emoji": "🏟️", "tier": "Epic", "points": 500},
    "the_marathon": {"name": "The Marathon", "emoji": "🏃", "tier": "Rare", "points": 250},
    "flawless": {"name": "Flawless", "emoji": "✨", "tier": "Legendary", "points": 750},
    "reliable": {"name": "Reliable", "emoji": "🛡️", "tier": "Rare", "points": 300},
    "master_negotiator": {"name": "Master Negotiator", "emoji": "🔄", "tier": "Epic", "points": 500},
    "world_host": {"name": "World Host", "emoji": "🌎", "tier": "Epic", "points": 400},

    # Secret achievements. Requirements are intentionally hidden from members.
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
ACHIEVEMENT_ROLE_COLORS = {
    "Common": 0x95A5A6,
    "Rare": 0x3498DB,
    "Epic": 0x9B59B6,
    "Legendary": 0xF1C40F,
    "Mythic": 0xE74C3C,
}

def check_host(host_id):
    hid = str(host_id)
    if hid not in db["hosts"]:
        db["hosts"][hid] = {
            "total_events": 0,
            "total_players": 0,
            "ratings": [],
            "rating_history": [],
        }
    else:
        data = db["hosts"][hid]
        data.setdefault("total_events", 0)
        data.setdefault("total_players", 0)
        data.setdefault("ratings", [])
        data.setdefault("rating_history", [])
    return hid

def get_achievement_state(guild_id, host_id):
    gid, hid = str(guild_id), str(host_id)
    states = db.setdefault("achievement_progress", {})
    server = states.setdefault(gid, {})
    return server.setdefault(hid, {"unlocked": [], "unlocked_at": {}})

def completed_events_for_host(guild_id, host_id):
    hid = int(host_id)
    gid = int(guild_id)
    return sorted(
        [e for e in db["events"].values()
         if e.get("guild_id") == gid and e.get("host_id") == hid and e.get("concluded") and not e.get("cancelled", False)],
        key=lambda e: e.get("concluded_at", e.get("end_time", 0))
    )

def all_host_events(guild_id, host_id):
    hid, gid = int(host_id), int(guild_id)
    return sorted(
        [e for e in db["events"].values()
         if e.get("guild_id") == gid and e.get("host_id") == hid],
        key=lambda e: e.get("concluded_at", e.get("end_time", e.get("start_time", 0)))
    )

def event_player_count(event):
    return int(event.get("player_count", len(event.get("participants", [])) or len(event.get("reservations", {}))))

def event_was_full(event):
    cap = int(event.get("max_players", 0) or 0)
    return cap > 0 and event_player_count(event) >= cap

def _event_day(e):
    """UTC calendar day an event finished on (stable no matter where the bot is hosted)."""
    return datetime.datetime.fromtimestamp(
        e.get("concluded_at", e.get("end_time", 0)), datetime.timezone.utc).date()

def host_achievement_requirements(guild_id, host_id):
    events = completed_events_for_host(guild_id, host_id)
    counts = len(events)
    players = [event_player_count(e) for e in events]
    full_events = [e for e in events if event_was_full(e)]
    swaps = sum(int(e.get("swaps_completed", 0)) for e in events)
    waitlist_fills = sum(int(e.get("waitlist_fills", 0)) for e in events)
    unique_nations = set()
    for e in events:
        for country in e.get("reservations", {}):
            unique_nations.add(country.title())

    busiest_day = max(collections.Counter(_event_day(e) for e in events).values(), default=0)
    # Cancelled events never show up in `events`, so "reliable" has to look at every settled event.
    settled = [e for e in all_host_events(guild_id, host_id) if e.get("concluded") or e.get("cancelled")]

    # Hosting milestones.
    earned = {
        "first_steps": counts >= 1,
        "getting_started": counts >= 3,
        "experienced": counts >= 7,
        "event_machine": counts >= 15,
        "event_master": counts >= 25,
        "event_legend": counts >= 50,

        # Community achievements use separate events, not total players.
        "popular": sum(1 for p in players if p >= 20) >= 3,
        "community_builder": sum(1 for p in players if p >= 30) >= 2,
        "community_idol": any(p >= 40 for p in players),

        # Rare achievements.
        "sold_out": len(full_events) >= 1,
        "packed_house": len(full_events) >= 5,
        "the_marathon": busiest_day >= 2,
        "flawless": False,  # evaluated from rating history below
        "reliable": len(settled) >= 10 and not any(e.get("cancelled") for e in settled[-10:]),
        "master_negotiator": swaps >= 15,
        "world_host": len(unique_nations) >= 10,

        # Secret achievements.
        "forty_deep": any(p >= 40 for p in players),
        "waitlist_hero": waitlist_fills >= 5,
        "triple_threat": busiest_day >= 3,
        "perfect_run": len(events) >= 10 and all(event_player_count(e) >= 20 for e in events[-10:]),
        "big_game": any(p >= 35 for p in players),
        "veteran_ground": len(events) >= 20,
        "full_house_five": len(full_events) >= 5,
        "waitlist_savior": waitlist_fills >= 10,
    }

    history = db.get("hosts", {}).get(str(host_id), {}).get("rating_history", [])
    valid_event_keys = {k for k, e in db["events"].items() if e.get("guild_id") == int(guild_id)}
    recent = [x for x in history if x.get("event_key") in valid_event_keys]
    recent.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    earned["flawless"] = len(recent) >= 6 and all(float(x.get("rating", 0)) == 5.0 for x in recent[:6])
    return earned

def achievement_role_for(guild, achievement_id):
    conf = get_config(guild.id)
    role_map = conf.setdefault("achievement_roles", {})
    rid = role_map.get(achievement_id)
    return guild.get_role(int(rid)) if rid else None

async def ensure_achievement_roles(guild):
    conf = get_config(guild.id)
    role_map = conf.setdefault("achievement_roles", {})
    created, existing = [], []

    # Reuse roles already recorded in config. If a role was deleted or the config
    # was lost, look for an existing role by its exact achievement name before
    # creating anything. This makes setup safe to run repeatedly.
    for aid, data in ACHIEVEMENT_DEFS.items():
        role_name = f"{data['emoji']} {data['name']}"
        role = None

        rid = role_map.get(aid)
        if rid:
            try:
                role = guild.get_role(int(rid))
            except (TypeError, ValueError):
                role = None

        if role is None:
            role = discord.utils.find(lambda r: r.name == role_name, guild.roles)
            if role is not None:
                role_map[aid] = role.id

        if role is not None:
            existing.append(role.name)
            continue

        try:
            role = await guild.create_role(
                name=role_name,
                color=discord.Color(ACHIEVEMENT_ROLE_COLORS.get(data["tier"], 0x5865F2)),
                mentionable=False,
                reason="Host Assistant achievement role setup"
            )
            role_map[aid] = role.id
            created.append(role.name)
        except discord.HTTPException as e:
            print(f"achievement role create failed for {aid}: {e}")
            break

    save_db(db)
    return created, existing

async def unlock_host_achievements(guild, host_id):
    state = get_achievement_state(guild.id, host_id)
    earned = host_achievement_requirements(guild.id, host_id)
    unlocked = set(state.get("unlocked", []))
    new_ids = [aid for aid in ACHIEVEMENT_ORDER if earned.get(aid) and aid not in unlocked]
    if not new_ids:
        return []

    state.setdefault("unlocked_at", {})
    for aid in new_ids:
        state["unlocked"].append(aid)
        state["unlocked_at"][aid] = int(time.time())

    member = guild.get_member(int(host_id))
    if member:
        for aid in new_ids:
            role = achievement_role_for(guild, aid)
            if role:
                try:
                    await member.add_roles(role, reason=f"Host Assistant achievement: {ACHIEVEMENT_DEFS[aid]['name']}")
                except discord.HTTPException as e:
                    print(f"achievement role assign failed: {aid}: {e}")
    save_db(db)
    return new_ids

async def check_and_announce_achievements(guild, host_id, channel=None):
    new_ids = await unlock_host_achievements(guild, host_id)
    if not new_ids:
        return []
    lines = []
    for aid in new_ids:
        d = ACHIEVEMENT_DEFS[aid]
        lines.append(f"{d['emoji']} **{d['name']}** · +{d['points']} pts")
    if channel:
        try:
            await channel.send(
                embed=discord.Embed(
                    title="🏆 Achievement Unlocked",
                    description=f"<@{host_id}> earned:\n\n" + "\n".join(lines),
                    color=0xFEE75C
                )
            )
        except discord.HTTPException:
            pass
    return new_ids

def _achievement_points(unlocked):
    """Return the total point value of unlocked achievements."""
    return sum(ACHIEVEMENT_DEFS[aid]["points"] for aid in unlocked if aid in ACHIEVEMENT_DEFS)


def host_level_from_points(points):
    """Small community-scale progression system layered on existing achievement points."""
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


def build_host_profile_embed(guild, member):
    """Build the Host Assistant profile embed for a member."""
    stats = db.get("hosts", {}).get(str(member.id), {})  # read-only: viewing must not create records

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

@bot.tree.command(name="host", description="View a host's profile")
@app_commands.guild_only()
async def host_profile_cmd(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    await interaction.response.send_message(embed=build_host_profile_embed(interaction.guild, member), ephemeral=True)

@bot.tree.command(name="me", description="View your Host Assistant profile")
@app_commands.guild_only()
async def me_profile_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_host_profile_embed(interaction.guild, interaction.user), ephemeral=True)

@bot.tree.command(name="achievements", description="View a host's achievement collection")
@app_commands.guild_only()
async def achievements_cmd(interaction: discord.Interaction, member: discord.Member = None):
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
        color=0xFEE75C
    )
    add_footer_divider(emb)
    emb.set_footer(text=f"{len(unlocked)}/{len(ACHIEVEMENT_DEFS)} unlocked · Host Assistant")
    await interaction.response.send_message(embed=emb, ephemeral=True)

@bot.tree.command(name="disable_server", description="Disable Host Assistant in a server")
@app_commands.describe(server_id="The Discord server ID to disable")
async def disable_server_cmd(interaction: discord.Interaction, server_id: str):
    if interaction.user.id not in BOT_OWNER_IDS:
        await interaction.response.send_message("Bot owners only.", ephemeral=True)
        return

    server_id = server_id.strip()
    if not server_id.isdigit():
        await interaction.response.send_message("❌ Invalid server ID. Please provide a numeric Discord server ID.", ephemeral=True)
        return

    disabled = db.setdefault("disabled_guilds", [])
    if server_id not in disabled:
        disabled.append(server_id)
        save_db(db)
        message = f"✅ Host Assistant has been disabled in server `{server_id}`."
    else:
        message = f"ℹ️ Server `{server_id}` is already disabled."

    await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="enable_server", description="Re-enable Host Assistant in a server")
@app_commands.describe(server_id="The Discord server ID to re-enable")
async def enable_server_cmd(interaction: discord.Interaction, server_id: str):
    if interaction.user.id not in BOT_OWNER_IDS:
        await interaction.response.send_message("Bot owners only.", ephemeral=True)
        return

    server_id = server_id.strip()
    if not server_id.isdigit():
        await interaction.response.send_message("❌ Invalid server ID. Please provide a numeric Discord server ID.", ephemeral=True)
        return

    disabled = db.setdefault("disabled_guilds", [])
    if server_id in disabled:
        disabled.remove(server_id)
        save_db(db)
        message = f"✅ Host Assistant has been re-enabled in server `{server_id}`."
    else:
        message = f"ℹ️ Server `{server_id}` is not in the disabled server list."

    await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="analytics", description="View real event and host analytics")
@app_commands.guild_only()
@app_commands.choices(period=[
    app_commands.Choice(name="Last 7 Days", value="7"),
    app_commands.Choice(name="Last 30 Days", value="30"),
    app_commands.Choice(name="Last 90 Days", value="90"),
    app_commands.Choice(name="All Time", value="0"),
])
async def analytics_cmd(interaction: discord.Interaction, period: app_commands.Choice[str] = None):
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

    # Group by host id straight from the event records, so hosts who left the server still count.
    per_host = {}
    for e in events:
        row = per_host.setdefault(e.get("host_id"), [0, 0])
        row[0] += 1
        row[1] += event_player_count(e)
    host_rows = sorted(per_host.items(), key=lambda kv: (kv[1][0], kv[1][1]), reverse=True)

    emb = discord.Embed(title="📊 Host Assistant · Analytics", color=0x5865F2)
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


# ==========================================
# TRIAL HOSTS: "The Trial Review"
# ==========================================
# A new host starts on trial. After their trial event, eligible players submit one final
# Keep / IDK / Sweep vote with a reason. Totals update publicly; detailed reasons live in Trial Logs.
# The bot records feedback only. The server team makes promotion/demotion decisions manually.
TRIAL_VOTE_HOURS = 24      # how long voting stays open
TRIAL_REASON_MIN = 8
TRIAL_CHOICES = {
    "keep":  {"label": "Keep",  "emoji": "✅", "block": "🟩", "color": 0x57F287, "meaning": "promote",
              "ask": "What did they do well?"},
    "idk":   {"label": "IDK",   "emoji": "🤔", "block": "🟨", "color": 0xFEE75C, "meaning": "unsure",
              "ask": "What left you unsure?"},
    "sweep": {"label": "Sweep", "emoji": "🧹", "block": "🟥", "color": 0xED4245, "meaning": "demote",
              "ask": "What needs to change?"},
}

def can_run_trials(member):
    """Admins, bot owners and trusted users run trials. Trial hosts and normal hosts do not."""
    return is_server_admin(member) or is_trusted(member)


def can_review_trials(member):
    """Admins, bot owners, trusted users and members with the Event Manager role."""
    if can_run_trials(member):
        return True
    r_id = get_config(member.guild.id).get("event_manager_role")
    return bool(r_id) and any(r.id == r_id for r in getattr(member, "roles", []))


def trial_state(guild_id, host_id):
    return db["trial_hosts"].get(str(guild_id), {}).get(str(host_id))

def set_trial_state(guild_id, host_id, **fields):
    rec = db["trial_hosts"].setdefault(str(guild_id), {}).setdefault(str(host_id), {})
    rec.update(fields)
    return rec

def trial_channel_for(guild):
    """Public channel where trial announcements and voting are posted."""
    conf = get_config(guild.id)
    cid = conf.get("trial_channel") or conf.get("stats_channel")
    return guild.get_channel(cid) if cid else None


def trial_log_channel_for(guild):
    """Separate restricted-access channel for detailed trial voting logs."""
    cid = get_config(guild.id).get("trial_log_channel")
    return guild.get_channel(cid) if cid else None

def trial_tally(rev):
    counts = {k: 0 for k in TRIAL_CHOICES}
    for v in rev["votes"].values():
        if v.get("v") in counts:
            counts[v["v"]] += 1
    return counts, sum(counts.values())

def trial_open_embed(rev):
    """Public trial card: live vote totals, no automated verdict or decision controls."""
    host = rev["host_id"]
    counts, total = trial_tally(rev)
    is_closed = rev.get("status") == "closed"
    state = "🔒 Voting closed" if is_closed else "🟢 Voting open"
    emb = discord.Embed(color=0x5865F2 if not is_closed else 0x5865F2)
    emb.description = (
        f"## ⚖️ Trial Review\n"
        f"### <@{host}> has finished their trial.\n"
        f"{state} · " + ("Voting period ended" if is_closed else f"Closes <t:{rev['ends_at']}:R>") + f"\n{DIV}\n"
        f"🎓 **Candidate** · <@{host}>\n"
        f"🎪 **Trial event** · {rev['event_name']}\n"
        f"👥 **Players** · {rev['players']} · ⏱️ **Ran** · {rev['hours']}h\n{DIV}\n"
        f"### 📊 Votes · {total} total\n"
        f"✅ **Keep** · {counts['keep']}\n"
        f"🤔 **IDK** · {counts['idk']}\n"
        f"🧹 **Sweep** · {counts['sweep']}\n{DIV}\n"
        f"{'Voting has ended. The team will review the feedback and decide what happens next.' if is_closed else 'Choose one option and explain your vote. Your submission is final.'}"
    )
    if not is_closed:
        emb.add_field(name="🗝️ Who can vote", value="Players from the trial event + staff", inline=True)
    add_footer_divider(emb)
    emb.set_footer(text=f"Vote totals are public · written reasons are in Trial Logs · {BRAND}")
    return emb


# Trial Logs mirror the Event Logs browsing experience and contain detailed written feedback.
TRIAL_LOG_OPTIONS = [("all", "All activity", "📜"),
                     ("keep", "Keep", "✅"), ("idk", "IDK", "🤔"), ("sweep", "Sweep", "🧹")]
TRIAL_LOG_PAGE_SIZE = 5


def trial_log_rows(rev):
    """Return feedback rows, attaching voter IDs without exposing timestamps."""
    rows = rev.get("logs")
    if rows is None:
        rows = [
            {"c": vote.get("v"), "u": str(user_id), "m": vote.get("why", "")}
            for user_id, vote in rev.get("votes", {}).items()
            if vote.get("v") in TRIAL_CHOICES
        ]
        rev["logs"] = rows
    else:
        # Migrate logs created by earlier versions, which stored timestamps but
        # did not retain a voter ID on each display row.
        votes = list(rev.get("votes", {}).items())
        used = set()
        for entry in rows:
            if entry.get("u"):
                continue
            match = None
            for user_id, vote in votes:
                if user_id in used or vote.get("v") not in TRIAL_CHOICES:
                    continue
                if (vote.get("at") == entry.get("t")
                        and vote.get("v") == entry.get("c")
                        and vote.get("why", "") == entry.get("m", "")):
                    match = (user_id, vote)
                    break
            if match is None:
                for user_id, vote in votes:
                    if user_id in used or vote.get("v") not in TRIAL_CHOICES:
                        continue
                    if vote.get("v") == entry.get("c") and vote.get("why", "") == entry.get("m", ""):
                        match = (user_id, vote)
                        break
            if match:
                entry["u"] = str(match[0])
                used.add(match[0])
    return rows


def trial_log_state(rev):
    state = rev.get("log_view") or {}
    category = state.get("cat", "all")
    if category not in {"all", "keep", "idk", "sweep"}:
        category = "all"
    rows = [entry for entry in trial_log_rows(rev)
            if category == "all" or entry.get("c") == category]
    rows.reverse()
    pages = max(1, -(-len(rows) // TRIAL_LOG_PAGE_SIZE))
    try:
        page = int(state.get("page", 0))
    except (TypeError, ValueError):
        page = 0
    page = max(0, min(page, pages - 1))
    return category, page, pages, rows


def trial_log_embed(rev):
    category, page, pages, rows = trial_log_state(rev)
    closed = rev.get("status") == "closed"
    counts, total = trial_tally(rev)
    host = f"🎓 **Candidate** · <@{rev['host_id']}>"
    overview = "\n".join(
        f"{TRIAL_CHOICES[key]['emoji']} **{TRIAL_CHOICES[key]['label']}** · {counts[key]}"
        for key in ("keep", "idk", "sweep"))
    section = ("📝 Latest feedback" if category == "all" else
               f"{TRIAL_CHOICES[category]['emoji']} {TRIAL_CHOICES[category]['label']}")
    page_rows = rows[page * TRIAL_LOG_PAGE_SIZE:(page + 1) * TRIAL_LOG_PAGE_SIZE]
    body = "\n".join(
        f"{TRIAL_CHOICES.get(entry.get('c'), TRIAL_CHOICES['idk'])['emoji']} "
        f"<@{entry.get('u')}> — {entry.get('m', '')}"
        for entry in page_rows
    ) or "*No reasons submitted yet*"
    description = (
        f"## 📜 Trial Log · {rev['event_name']}\n{DIV}\n"
        f"### 📊 Overview\n{host}\n🎪 **Trial event** · {rev['event_name']}\n"
        f"{overview}\n{DIV}\n### {section}\n{body}\n{DIV}"
    )
    embed = discord.Embed(color=0x5865F2, description=description[:4000])
    state_label = ("🔒 Concluded" if closed else
                   "🔓 Closing" if rev.get("status") == "closing" else "🟢 Voting open")
    add_footer_divider(embed)
    embed.set_footer(text=f"{len(trial_log_rows(rev))} entries · {state_label} · Page {page + 1}/{pages}")
    return embed


def find_trial_by_log(message_id):
    for review in db.get("trials", {}).values():
        if (review.get("trial_log_msg") or {}).get("id") == message_id:
            return review
    return None


async def refresh_trial_log(rev):
    log_message = rev.get("trial_log_msg")
    if not log_message:
        return False
    channel = bot.get_channel(log_message.get("ch"))
    if not channel:
        return False
    try:
        message = await channel.fetch_message(log_message["id"])
        await message.edit(embed=trial_log_embed(rev), view=make_trial_log_view(rev))
        return True
    except discord.NotFound:
        rev.pop("trial_log_msg", None)
        save_db(db)
        return False
    except discord.HTTPException as error:
        print("refresh_trial_log failed:", error)
        return False


async def spawn_trial_log(guild, rev):
    # Detailed Trial Logs use their own channel configured in /setup.
    channel = trial_log_channel_for(guild)
    if not channel:
        print("trial log skipped: no dedicated Trial Logs channel configured in /setup")
        return False
    try:
        message = await channel.send(embed=trial_log_embed(rev), view=make_trial_log_view(rev))
        rev["trial_log_msg"] = {"ch": channel.id, "id": message.id}
        save_db(db)
        return True
    except discord.HTTPException as error:
        print("spawn_trial_log failed:", error)
        return False


class TrialLogCategorySelect(discord.ui.Select):
    def __init__(self, current="all"):
        options = [discord.SelectOption(label=label, value=value, emoji=emoji, default=(value == current))
                   for value, label, emoji in TRIAL_LOG_OPTIONS]
        super().__init__(custom_id="triallog_select", placeholder="Browse trial logs by category",
                         options=options, row=0)

    async def callback(self, interaction):
        review = find_trial_by_log(interaction.message.id)
        if not review:
            await interaction.response.send_message("This trial log is no longer available.", ephemeral=True)
            return
        review["log_view"] = {"cat": self.values[0], "page": 0}
        save_db(db)
        await interaction.response.edit_message(embed=trial_log_embed(review), view=make_trial_log_view(review))


class TrialLogPageButton(discord.ui.Button):
    def __init__(self, direction, disabled=False):
        label = "◀ Prev" if direction < 0 else "Next ▶"
        custom_id = "triallog_prev" if direction < 0 else "triallog_next"
        super().__init__(label=label, style=discord.ButtonStyle.secondary, custom_id=custom_id,
                         disabled=disabled, row=1)
        self.direction = direction

    async def callback(self, interaction):
        review = find_trial_by_log(interaction.message.id)
        if not review:
            await interaction.response.send_message("This trial log is no longer available.", ephemeral=True)
            return
        category, page, pages, _ = trial_log_state(review)
        review["log_view"] = {"cat": category, "page": max(0, min(pages - 1, page + self.direction))}
        save_db(db)
        await interaction.response.edit_message(embed=trial_log_embed(review), view=make_trial_log_view(review))


class TrialLogView(GuardedView):
    def __init__(self, category="all", page=0, pages=1):
        super().__init__(timeout=None)
        self.add_item(TrialLogCategorySelect(category))
        self.add_item(TrialLogPageButton(-1, disabled=page <= 0))
        self.add_item(TrialLogPageButton(+1, disabled=page >= pages - 1))


def make_trial_log_view(rev):
    category, page, pages, _ = trial_log_state(rev)
    return TrialLogView(category, page, pages)

async def refresh_trial_message(rev, **kwargs):
    ch = bot.get_channel(rev["channel_id"])
    if not ch:
        return False
    try:
        await ch.get_partial_message(int(rev["message_id"])).edit(**kwargs)
        return True
    except discord.HTTPException as e:
        print("trial message edit failed:", e)
        return False

def close_trial_voting(rev_id):
    """Close voting at the deadline; never calculate or apply a promotion/demotion verdict."""
    rev = db["trials"].get(rev_id)
    if not rev or rev.get("status") == "closed":
        return
    rev["status"] = "closed"
    rev["closed_at"] = int(time.time())
    guild = bot.get_guild(rev["guild_id"])
    if guild:
        # The vote is over: mark the trial finished so the host can be re-appointed later.
        # Roles are still handed out manually; nothing is promoted or demoted here.
        set_trial_state(guild.id, rev["host_id"], status="done", review=rev.get("message_id"))
    save_db(db)
    task = asyncio.create_task(_finish_trial_closure(rev_id))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _finish_trial_closure(rev_id):
    rev = db["trials"].get(rev_id)
    if not rev:
        return
    await refresh_trial_message(rev, embed=trial_open_embed(rev), view=None)
    await refresh_trial_log(rev)


async def close_due_trials():
    now = int(time.time())
    for rid, rev in list(db["trials"].items()):
        if ((rev.get("status") == "open" and rev.get("ends_at", now + 1) <= now)
                or (rev.get("status") == "closing" and now - rev.get("closing_at", 0) > 120)):
            close_trial_voting(rid)


async def maybe_open_trial_review(guild, info):
    """Opens the public feedback vote for a trial host's event. Returns None on success, or a reason text."""
    host_id = info["host_id"]
    st = trial_state(guild.id, host_id)
    if not info.get("trial") or not st or st.get("status") != "trial":
        return "That host isn't waiting for a trial review."
    ch = trial_channel_for(guild)
    if not ch:
        print("trial review skipped: no public trial review/stats channel configured")
        return "No trial review or stats channel is set up. Set one in /setup first."
    now = int(time.time())
    skip = {host_id, info.get("cohost_id")}
    electorate = sorted({int(i) for i in list(info.get("participants", [])) + list(info.get("helpers", []))} - skip)
    rev = {
        "guild_id": guild.id, "channel_id": ch.id, "message_id": 0, "host_id": host_id,
        "event_key": None, "event_name": info["display_name"],
        "players": info.get("player_count", 0),
        "hours": round(info.get("duration_seconds", 0) / 3600, 1),
        "opened_at": now, "ends_at": now + TRIAL_VOTE_HOURS * 3600,
        "status": "open", "electorate": electorate, "votes": {}, "verdict": None, "applied": False,
        "logs": [], "log_view": {"cat": "all", "page": 0},
    }
    try:
        msg = await ch.send(embed=trial_open_embed(rev), view=TrialVoteView())
    except discord.HTTPException as e:
        print("trial review post failed:", e)
        return "I couldn't post the vote there. Check my permissions in the review channel."
    rev["message_id"] = msg.id
    db["trials"][str(msg.id)] = rev
    set_trial_state(guild.id, host_id, status="review", review=msg.id, review_channel=ch.id)
    save_db(db)
    await spawn_trial_log(guild, rev)
    return None

class TrialWhyModal(discord.ui.Modal):
    def __init__(self, rev_id, choice, prev=None):
        c = TRIAL_CHOICES[choice]
        super().__init__(title=f"Why {c['label']}? ({c['meaning']})"[:45])
        self.rev_id, self.choice = rev_id, choice
        self.why = discord.ui.TextInput(
            label=c["ask"], style=discord.TextStyle.paragraph, required=True,
            min_length=TRIAL_REASON_MIN, max_length=400,
            placeholder="Be honest and specific. Staff read every reason.",
            default=prev["why"] if prev and prev.get("v") == choice else None)
        self.add_item(self.why)

    async def on_submit(self, interaction):
        rev = db["trials"].get(self.rev_id)
        if not rev or rev["status"] != "open":
            await interaction.response.send_message("The ballot closed while you were writing.", ephemeral=True)
            return
        why = " ".join(self.why.value.split())
        if len(why) < TRIAL_REASON_MIN:
            await interaction.response.send_message("Give a real reason (a few words at least).", ephemeral=True)
            return
        uid = str(interaction.user.id)
        if uid in rev["votes"]:
            await interaction.response.send_message("Your vote has already been submitted and cannot be changed.", ephemeral=True)
            return
        now = int(time.time())
        rev["votes"][uid] = {"v": self.choice, "why": why, "at": now}
        rev.setdefault("logs", []).append({"c": self.choice, "u": uid, "m": why[:400]})
        save_db(db)
        c = TRIAL_CHOICES[self.choice]
        emb = discord.Embed(color=c["color"],
                            description=f"## ✅ Vote recorded\n{DIV}\n"
                                        f"{c['emoji']} **{c['label']}** · {c['meaning']}\nYour vote has been recorded and is final.")
        add_footer_divider(emb)
        emb.set_footer(text="Your vote is final · your reason is recorded in the Trial Logs")
        await interaction.response.send_message(embed=emb, ephemeral=True)
        await refresh_trial_message(rev, embed=trial_open_embed(rev))
        await refresh_trial_log(rev)

class TrialVoteView(GuardedView):
    def __init__(self):
        super().__init__(timeout=None)

    async def _vote(self, interaction, choice):
        rev_id = str(interaction.message.id)
        rev = db["trials"].get(rev_id)
        if not rev:
            await interaction.response.send_message("This review no longer exists.", ephemeral=True)
            return
        if rev["status"] != "open":
            await interaction.response.send_message("The ballot is closed.", ephemeral=True)
            return
        uid = interaction.user.id
        if str(uid) in rev.get("votes", {}):
            await interaction.response.send_message("You've already voted. Your submission is final.", ephemeral=True)
            return
        if uid in (rev["host_id"],):
            await interaction.response.send_message("You can't vote on your own trial.", ephemeral=True)
            return
        if uid not in rev["electorate"] and not can_run_trials(interaction.user):
            await interaction.response.send_message(
                "Only players from the trial event (and staff) can vote on this one.", ephemeral=True)
            return
        await interaction.response.send_modal(TrialWhyModal(rev_id, choice))

    @discord.ui.button(label="Keep", emoji="✅", style=discord.ButtonStyle.success, custom_id="trial_keep")
    async def keep(self, interaction, btn):
        await self._vote(interaction, "keep")

    @discord.ui.button(label="IDK", emoji="🤔", style=discord.ButtonStyle.secondary, custom_id="trial_idk")
    async def idk(self, interaction, btn):
        await self._vote(interaction, "idk")

    @discord.ui.button(label="Sweep", emoji="🧹", style=discord.ButtonStyle.danger, custom_id="trial_sweep")
    async def sweep(self, interaction, btn):
        await self._vote(interaction, "sweep")

trial_group = app_commands.Group(name="trial", description="Trial host system", guild_only=True)
bot.tree.add_command(trial_group)

@trial_group.command(name="appoint", description="Start a trial for a new host")
async def trial_appoint(interaction: discord.Interaction, member: discord.Member):
    if not can_run_trials(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    role = interaction.guild.get_role(get_config(interaction.guild_id).get("trial_role") or 0)
    if not role:
        await interaction.response.send_message("Set a **Trial Host role** in /setup first.", ephemeral=True)
        return
    if member.bot:
        await interaction.response.send_message("Bots can't be hosts.", ephemeral=True)
        return
    cur = trial_state(interaction.guild_id, member.id)
    if cur and cur.get("status") in ("trial", "review"):
        await interaction.response.send_message(f"{member.mention} is already on trial.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await member.add_roles(role, reason=f"Trial started by {interaction.user}")
    except discord.HTTPException:
        await interaction.followup.send("I couldn't give them the role. Check that my role is above it.", ephemeral=True)
        return
    set_trial_state(interaction.guild_id, member.id, status="trial", since=int(time.time()),
                    review=None, appointed_by=interaction.user.id)
    save_db(db)
    ch = trial_channel_for(interaction.guild)
    if ch:
        emb = discord.Embed(color=0x5865F2, description=(
            f"## 🎓 A new trial begins\n### {member.mention}\n{DIV}\n"
            f"Their first event will be judged by the people who play in it.\n"
            f"✅ **Keep** · 🤔 **IDK** · 🧹 **Sweep**"))
        add_footer_divider(emb)
        emb.set_footer(text=BRAND)
        try:
            await ch.send(embed=emb)
        except discord.HTTPException:
            pass
    await interaction.followup.send(f"🎓 {member.mention} is now on trial. Their next event will be a trial event. When it ends, use `/trial review` to open the vote.", ephemeral=True)


@trial_group.command(name="review", description="Open the vote on a trial host's latest event")
@app_commands.describe(host="The trial host whose event just ended")
async def trial_review(interaction: discord.Interaction, host: discord.Member):
    if not can_review_trials(interaction.user):
        await interaction.response.send_message("Admins and Event Managers only.", ephemeral=True)
        return
    st = trial_state(interaction.guild_id, host.id)
    if st and st.get("status") == "review" and st.get("review") and st.get("review_channel"):
        link = f"https://discord.com/channels/{interaction.guild_id}/{st['review_channel']}/{st['review']}"
        await interaction.response.send_message(f"{host.mention} already has a vote open: {link}", ephemeral=True)
        return
    if not st or st.get("status") != "trial":
        await interaction.response.send_message(f"{host.mention} isn't on trial right now.", ephemeral=True)
        return
    evs = [e for e in db["events"].values()
           if event_in_guild(e, interaction.guild_id) and e.get("host_id") == host.id
           and e.get("trial") and e.get("concluded") and not e.get("cancelled")]
    if not evs:
        await interaction.response.send_message(
            f"{host.mention} hasn't finished a trial event yet. They need to press Conclude Event first.",
            ephemeral=True)
        return
    info = max(evs, key=lambda e: e.get("concluded_at", 0))
    await interaction.response.defer(ephemeral=True)
    err = await maybe_open_trial_review(interaction.guild, info)
    if err:
        await interaction.followup.send(f"❌ {err}", ephemeral=True)
        return
    await interaction.followup.send(
        f"⚖️ Vote opened for {host.mention}'s event **{info['display_name']}**.", ephemeral=True)
