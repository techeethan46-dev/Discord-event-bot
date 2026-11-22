"""Server tools: the /setup wizard and /talk."""

import datetime
import discord
import re
import time
from discord import app_commands

from hostbot.core import ALLOWED_USER_IDS, BOT_OWNER_IDS, bot, db, get_config, is_trusted, save_db
from hostbot.hosts import ACHIEVEMENT_DEFS, ensure_achievement_roles
from hostbot.shared import (
    DIV, GuardedView, add_footer_divider, emergency_rate_limit_shutdown, has_host_role,
    is_server_admin, log_action, v2,
)
from hostbot.ui import COLORS, card, divider, footnote, msg, notice, progress, rows, section, text, thumb
from hostbot import core

# ==========================================
# /setup: modern step-by-step dashboard
# ==========================================
SETUP_STEPS = [
    {"key": "host_role", "kind": "role", "icon": "🎤", "title": "Host role", "required": False,
     "desc": "Members with this role can create events and open the Host panel. Admins can always do both."},
    {"key": "trial_role", "kind": "role", "icon": "🎓", "title": "Trial Host role", "required": False,
     "desc": "Given to new hosts while they're on trial. They can host their trial event, then the players vote on whether they stay."},
    {"key": "event_role", "kind": "role", "icon": "🎟️", "title": "Event role", "required": False,
     "desc": "Given to players while they hold a spot, and removed when they leave or the event ends."},
    {"key": "superpower_role", "kind": "role", "icon": "⭐", "title": "VIP role", "required": False,
     "desc": "Members with this role can use Host Assistant AI. It also grants Superpower access."},
    {"key": "double_booster_role", "kind": "role", "icon": "🖼️", "title": "Double Booster role", "required": False,
     "desc": "If your server has a Double Booster role, set it here. Only members with this role can send images for Host Assistant to analyze."},
    {"key": "staff_role", "kind": "role", "icon": "🛡️", "title": "Staff role", "required": False,
     "desc": "Members with this role can use normal AI chat and image vision. AI moderation (timeouts, roles and so on) only works if they also have the matching Discord permission. Server admins and bot owners are always allowed."},
    {"key": "event_manager_role", "kind": "role", "icon": "🗂️", "title": "Event Manager role", "required": False,
     "desc": "Optional role setting for your server's Event Managers. Trial outcomes are decided manually by your team; the bot only records votes."},
    {"key": "log_channel", "kind": "channel", "icon": "📜", "title": "Event log channel", "required": True,
     "desc": "Every event gets one live log embed here that you can browse by category."},
    {"key": "ai_log_channel", "kind": "channel", "icon": "🤖", "title": "AI admin log channel", "required": False,
     "desc": "Every action the AI admin runs is logged here in red. If you skip it, those logs go to the event log channel."},
    {"key": "talk_log_channel", "kind": "channel", "icon": "💬", "title": "/talk log channel", "required": False,
     "desc": "Every successful /talk message is recorded here in a clean audit embed with the author, channel and exact message."},
    {"key": "swap_channel", "kind": "channel", "icon": "🔄", "title": "Swap channel", "required": True,
     "desc": "Swap requests are posted here so the other player can accept or decline."},
    {"key": "stats_channel", "kind": "channel", "icon": "📊", "title": "Stats channel", "required": True,
     "desc": "Event results and the rate-your-host button are posted here when an event is concluded."},
    {"key": "weekly_summary_channel", "kind": "channel", "icon": "📅", "title": "Weekly summary channel", "required": False,
     "desc": "Every Monday (UTC) a recap of last week's events, players, top host and best-rated event is posted here. If you skip it, no weekly summary is posted."},
    {"key": "trial_channel", "kind": "channel", "icon": "⚖️", "title": "Trial review channel", "required": False,
     "desc": "The public trial announcement and voting card are posted here. If you skip it, they go to the stats channel."},
    {"key": "trial_log_channel", "kind": "channel", "icon": "📜", "title": "Trial Logs channel", "required": False,
     "desc": "Detailed trial vote records and written reasons are posted here. Members who can view this channel can read its Trial Logs."},
    {"key": "achievement_roles", "kind": "achievement", "icon": "🏆", "title": "Achievement roles", "required": False,
     "desc": "Create and sync every Host Assistant achievement role. Existing roles are reused, so running setup again will not duplicate them."},
]
SETUP_COLOR = 0x5865F2

# Steps are grouped into sections, each with its own colour and icon (like Hard's /setup).
_SECTION_OF = {"role": "Roles", "channel": "Channels", "achievement": "Achievements"}
for _st in SETUP_STEPS:
    _st["section"] = _SECTION_OF[_st["kind"]]
SECTION_COLORS = {"Roles": COLORS["brand"], "Channels": COLORS["info"], "Achievements": COLORS["ok"]}
SECTION_ICONS = {"Roles": "🎭", "Channels": "📡", "Achievements": "🏆"}

def _achievement_roles_ready(guild):
    conf = get_config(guild.id)
    role_map = conf.get("achievement_roles", {})
    ready = 0
    for aid, data in ACHIEVEMENT_DEFS.items():
        role = None
        rid = role_map.get(aid)
        if rid:
            try:
                role = guild.get_role(int(rid))
            except (TypeError, ValueError):
                role = None
        if role is None:
            role = discord.utils.find(lambda r: r.name == f"{data['emoji']} {data['name']}", guild.roles)
        if role:
            ready += 1
    return ready, len(ACHIEVEMENT_DEFS)

def _setup_step_configured(guild, st, conf):
    if st["kind"] == "achievement":
        ready, total = _achievement_roles_ready(guild)
        return ready == total and total > 0
    return bool(conf.get(st["key"]))

def _setup_state(guild, st, conf):
    """(icon, value, required): value is None when nothing is set."""
    if st["kind"] == "achievement":
        ready, total = _achievement_roles_ready(guild)
        return ("✅" if ready == total and total > 0 else "⚠️"), f"**{ready}/{total}** roles ready", False
    v = conf.get(st["key"])
    if not v:
        return ("⚠️" if st["required"] else "➖"), None, st["required"]
    if st["kind"] == "role":
        return ("✅", f"<@&{v}>", False) if guild.get_role(v) else ("⚠️", "*role was deleted*", False)
    return ("✅", f"<#{v}>", False) if guild.get_channel(v) else ("⚠️", "*channel was deleted*", False)

def _setup_current(guild, st, conf):
    icon, value, required = _setup_state(guild, st, conf)
    if value is None:
        return f"{icon} Not set" + (" · required" if required else "")
    return f"{icon} {value}"

def _setup_line(guild, st, conf):
    icon, value, required = _setup_state(guild, st, conf)
    if value is None:
        value = "*required, not set*" if required else "*not set*"
    return f"{icon} **{st['title']}** · {value}"

def setup_checks(guild, conf):
    me = guild.me
    out = []
    for st in SETUP_STEPS:
        if st["kind"] == "achievement":
            ready, total = _achievement_roles_ready(guild)
            if ready < total:
                out.append(f"⚠️ **Achievement roles** are incomplete ({ready}/{total})")
            continue
        v = conf.get(st["key"])
        if not v:
            if st["required"]:
                out.append(f"⚠️ **{st['title']}** isn't set")
            continue
        if st["kind"] == "role":
            r = guild.get_role(v)
            if r and st["key"] in {"event_role", "achievement_roles", "trial_role"} and r >= me.top_role:
                out.append(f"⚠️ **{st['title']}** is above my role, so I may not be able to manage it")
        else:
            ch = guild.get_channel(v)
            if ch:
                p = ch.permissions_for(me)
                if not (p.view_channel and p.send_messages and p.embed_links):
                    out.append(f"⚠️ I can't post embeds in {ch.mention} (need Send Messages + Embed Links)")
    if conf.get("trial_channel") and not conf.get("trial_log_channel"):
        out.append("⚠️ **Trial Logs channel** isn't set; detailed vote records and reasons won't be posted until you configure it")
    gp = me.guild_permissions
    if not gp.manage_roles:
        out.append("⚠️ I need **Manage Roles** (event role + achievement roles + AI admin)")
    if not gp.manage_messages:
        out.append("⚠️ I need **Manage Messages** (cooldown cleanup + purge)")
    if not gp.manage_nicknames:
        out.append("⚠️ I need **Manage Nicknames** (the [AFK] tag in names)")
    return out or ["✅ Everything looks good"]

class SetupAchievementRolesButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Create / sync achievement roles", emoji="🏆", style=discord.ButtonStyle.success, row=0)

    async def callback(self, interaction):
        view = self.view
        await interaction.response.defer()
        created, existing = await ensure_achievement_roles(interaction.guild)
        ready, total = _achievement_roles_ready(interaction.guild)
        if created:
            note = f"🏆 **Achievement roles synced.** Created **{len(created)}** new role(s); **{len(existing)}** already existed. **{ready}/{total}** are ready."
        else:
            note = f"🏆 **Achievement roles already existed.** No duplicates created. **{ready}/{total}** are ready."
        await view.go(interaction, step=view.step + 1, note=note)

class SetupWizard(GuardedView):
    def __init__(self, guild, owner_id, step=0, note=""):
        super().__init__(timeout=900)
        self.guild, self.owner_id, self.step, self.note = guild, owner_id, step, note
        self.rebuild()

    @property
    def total(self):
        return len(SETUP_STEPS)

    def rebuild(self):
        self.clear_items()
        conf = get_config(self.guild.id)
        if self.step < self.total:
            st = SETUP_STEPS[self.step]
            if st["kind"] == "role":
                self.add_item(SetupRolePick(st))
            elif st["kind"] == "channel":
                self.add_item(SetupChannelPick(st))
            elif st["kind"] == "achievement":
                self.add_item(SetupAchievementRolesButton())
            self.add_item(SetupNav("back", disabled=self.step == 0))
            self.add_item(SetupNav("next", skip=not _setup_step_configured(self.guild, st, conf)))
            self.add_item(SetupNav("clear", disabled=(st["kind"] == "achievement" or not _setup_step_configured(self.guild, st, conf))))
        else:
            self.add_item(SetupJump())
            self.add_item(SetupNav("back"))
            self.add_item(SetupNav("finish"))

    def cards(self):
        """The screen as cards: one step, or the summary with a separate checks card."""
        conf = get_config(self.guild.id)
        t = self.total
        if self.step < t:
            st = SETUP_STEPS[self.step]
            foot = ("Existing roles are reused, so running this again never creates duplicates"
                    if st["kind"] == "achievement" else "Pick below · it saves instantly and moves to the next step")
            return [card(SECTION_COLORS[st["section"]],
                         text(f"-# ⚙️ SETUP · {st['section'].upper()} · STEP {self.step + 1} OF {t}",
                              progress(self.step + 1, t)),
                         divider(),
                         text(f"## {st['icon']} {st['title']}", st["desc"]),
                         divider(line=False),
                         text(f"**Current** · {_setup_current(self.guild, st, conf)}"),
                         text(self.note) if self.note else None,
                         divider(),
                         rows(self),
                         footnote(foot))]

        done = sum(1 for s_ in SETUP_STEPS if _setup_step_configured(self.guild, s_, conf))
        by_section = {}
        for st in SETUP_STEPS:
            by_section.setdefault(st["section"], []).append(_setup_line(self.guild, st, conf))
        security = [
            "🤖 **AI access** · VIP + Staff + Administrators + Bot Owners",
            "🖼️ **Image vision** · Double Booster role only",
            "⚖️ **Trial decisions** · reviewed and decided manually by your team",
            f"💬 **/talk audit** · {'configured' if conf.get('talk_log_channel') else 'optional, not configured'}",
        ]
        parts = []
        for name, lines in by_section.items():
            parts.append([divider(), text(f"### {SECTION_ICONS[name]} {name}", lines)])
            if name == "Roles":
                parts.append([divider(), text("### 🤖 AI & Security", security)])
        icon = self.guild.icon.url if self.guild.icon else None
        summary = card(COLORS["brand"],
                       section(["-# ⚙️ SETUP · SUMMARY", f"## {self.guild.name}",
                                f"{progress(done, t)}  **{done}/{t}** set"], thumb(icon)),
                       text(self.note) if self.note else None,
                       parts,
                       divider(),
                       rows(self),
                       footnote("Change anything with the menu · press Finish when you're done"))
        problems = [c for c in setup_checks(self.guild, conf) if c.startswith("⚠️")]
        health = card(COLORS["warn"] if problems else COLORS["ok"],
                      text(f"### 🔍 {len(problems)} thing{'s' if len(problems) != 1 else ''} to fix" if problems
                           else "### 🔍 Checks",
                           problems or ["✅ Everything looks good! The bot has all the access it needs."]))
        return [summary, health]

    async def interaction_check(self, interaction):
        if not await super().interaction_check(interaction):
            return False
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                **notice("bad", "This panel belongs to someone else. Run `/setup` yourself."), ephemeral=True)
            return False
        return True

    async def go(self, interaction, step=None, note=""):
        if step is not None:
            self.step = max(0, min(self.total, step))
        self.note = note
        self.rebuild()
        payload = msg(*self.cards(), view=self, edit=True)
        if interaction.response.is_done():
            await interaction.edit_original_response(**payload)
        else:
            await interaction.response.edit_message(**payload)

def _setup_saved(st, extra=""):
    return f"✅ **{st['title']}** saved." + (f"\n{extra}" if extra else "")

class SetupRolePick(discord.ui.RoleSelect):
    def __init__(self, st):
        super().__init__(placeholder=f"Select the {st['title'].lower()}...", min_values=1, max_values=1, row=0)
        self.st = st

    async def callback(self, interaction):
        view, role = self.view, self.values[0]
        guild = view.guild
        if role.is_default() or role.managed:
            await view.go(interaction, note="❌ That role can't be used (it's @everyone or managed by a bot). Pick another.")
            return
        get_config(guild.id)[self.st["key"]] = role.id
        save_db(db)
        extra = ""
        if self.st["key"] == "event_role" and role >= guild.me.top_role:
            extra = "⚠️ This role is above my role, so I won't be able to hand it out. Move my role higher."
        elif self.st["key"] == "double_booster_role":
            extra = "🖼️ Image analysis is now restricted to members with this role."
        elif self.st["key"] == "staff_role":
            extra = "🛡️ Staff can use AI chat + image vision. AI moderation needs the matching Discord permission."
        elif self.st["key"] == "superpower_role":
            extra = "🤖 AI access is now available to this VIP role."
        await view.go(interaction, step=view.step + 1, note=_setup_saved(self.st, extra))

class SetupChannelPick(discord.ui.ChannelSelect):
    def __init__(self, st):
        super().__init__(placeholder=f"Select the {st['title'].lower()}...", min_values=1, max_values=1, row=0,
                         channel_types=[discord.ChannelType.text, discord.ChannelType.news])
        self.st = st

    async def callback(self, interaction):
        view, picked = self.view, self.values[0]
        guild = view.guild
        get_config(guild.id)[self.st["key"]] = picked.id
        save_db(db)
        extra = ""
        ch = guild.get_channel(picked.id)
        if ch:
            p = ch.permissions_for(guild.me)
            if not (p.view_channel and p.send_messages and p.embed_links):
                extra = f"⚠️ I can't post embeds in {ch.mention} yet. Give me View Channel, Send Messages and Embed Links there."
        await view.go(interaction, step=view.step + 1, note=_setup_saved(self.st, extra))

class SetupNav(discord.ui.Button):
    LABELS = {"back": ("Back", "◀️", discord.ButtonStyle.secondary), "next": ("Next", "▶️", discord.ButtonStyle.primary),
              "clear": ("Clear", "🧹", discord.ButtonStyle.secondary), "finish": ("Finish", "✅", discord.ButtonStyle.success)}

    def __init__(self, kind, disabled=False, skip=False):
        label, emoji, style = self.LABELS[kind]
        if kind == "next" and skip:
            label, style = "Skip", discord.ButtonStyle.secondary
        super().__init__(label=label, emoji=emoji, style=style, disabled=disabled, row=1)
        self.kind = kind

    async def callback(self, interaction):
        view = self.view
        if self.kind == "back":
            await view.go(interaction, step=view.step - 1)
        elif self.kind == "next":
            await view.go(interaction, step=view.step + 1)
        elif self.kind == "clear":
            st = SETUP_STEPS[view.step]
            get_config(view.guild.id).pop(st["key"], None)
            save_db(db)
            await view.go(interaction, note=f"🗑️ **{st['title']}** cleared.")
        else:
            conf = get_config(view.guild.id)
            done = sum(1 for s in SETUP_STEPS if _setup_step_configured(view.guild, s, conf))
            await interaction.response.edit_message(**msg(card(
                COLORS["ok"],
                text("## 🎉 You're all set!", f"Your settings are saved. **{done} of {view.total}** options are configured."),
                divider(),
                footnote("Run /setup again any time to change things")), edit=True))
            view.stop()

class SetupJump(discord.ui.Select):
    def __init__(self):
        opts = [discord.SelectOption(label=s["title"], description=s["section"], value=str(i), emoji=s["icon"])
                for i, s in enumerate(SETUP_STEPS)]
        super().__init__(placeholder="✏️ Change something…", options=opts, row=0)

    async def callback(self, interaction):
        await self.view.go(interaction, step=int(self.values[0]))

@bot.tree.command(name="setup", description="Step-by-step setup dashboard")
@app_commands.guild_only()
async def setup_cmd(interaction: discord.Interaction):
    if not is_server_admin(interaction.user):
        await interaction.response.send_message(
            **notice("bad", "You need to be a server admin to change the setup.", title="Not allowed"), ephemeral=True)
        return
    conf = get_config(interaction.guild_id)
    # Start at the summary if the server was set up before, otherwise at step 1.
    start = len(SETUP_STEPS) if any(conf.get(st["key"]) for st in SETUP_STEPS) else 0
    wizard = SetupWizard(interaction.guild, interaction.user.id, step=start)
    await interaction.response.send_message(**msg(*wizard.cards(), view=wizard), ephemeral=True)
# ==========================================
# /talk — controlled bot voice message
# ==========================================
TALK_COOLDOWN = 4
_talk_cooldowns = {}

# Ping protection for /talk with mentions on: more than 10 pings in 2 minutes = muted for 20 minutes.
TALK_PING_LIMIT = 10
TALK_PING_WINDOW = 120
TALK_PING_MUTE_MINUTES = 20
_talk_pings = {}  # user id -> timestamps, one per ping sent


def talk_pings(message, member, channel):
    """Work out who a /talk message may ping. @everyone/@here are never allowed; roles only if
    they're mentionable or the author could ping them themselves."""
    guild = channel.guild
    user_ids = [int(i) for i in re.findall(r"<@!?(\d+)>", message)]
    can_ping_any_role = channel.permissions_for(member).mention_everyone
    roles = []
    for rid in re.findall(r"<@&(\d+)>", message):
        role = guild.get_role(int(rid))
        if role and (role.mentionable or can_ping_any_role):
            roles.append(role)
    users = [discord.Object(id=i) for i in dict.fromkeys(user_ids)]
    allowed = discord.AllowedMentions(everyone=False, users=users, roles=list(dict.fromkeys(roles)))
    return len(user_ids) + len(roles), allowed


async def punish_ping_spam(interaction):
    """Mute the author for 20 minutes and announce it, in the same style as the AI's
    'no permission to mute' message."""
    member, channel = interaction.user, interaction.channel
    try:
        await member.timeout(datetime.timedelta(minutes=TALK_PING_MUTE_MINUTES),
                             reason=f"More than {TALK_PING_LIMIT} pings in {TALK_PING_WINDOW // 60} min with /talk")
    except discord.HTTPException:
        return False
    emb = discord.Embed(color=0xE74C3C, description=(
        f"## ❌ Couldn't do that\n{DIV}\n"
        f"🔇 You pinged people more than {TALK_PING_LIMIT} times in {TALK_PING_WINDOW // 60} minutes, "
        f"so I muted **you** for {TALK_PING_MUTE_MINUTES} min."))
    add_footer_divider(emb)
    emb.set_footer(text=f"Requested by {member.display_name}")
    try:
        await channel.send(**v2(member.mention, embed=emb),
                           allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False))
    except discord.HTTPException:
        pass
    await log_action(interaction.guild, "/talk ping spam",
                     f"{member.mention} was muted for {TALK_PING_MUTE_MINUTES} min in {channel.mention} "
                     f"for pinging more than {TALK_PING_LIMIT} times in {TALK_PING_WINDOW // 60} min.",
                     color=0x5865F2, channel_key="talk_log_channel")
    return True

async def log_talk_usage(guild, author, channel, message, pings=False):
    """Write a polished audit card for every successful /talk message."""
    log_id = get_config(guild.id).get("talk_log_channel")
    if not log_id:
        return
    log_channel = guild.get_channel(log_id)
    if not log_channel:
        return
    safe_message = discord.utils.escape_mentions(str(message))
    embed = discord.Embed(
        title="💬 /talk · Message Sent",
        description=f"{DIV}\n### 📝 Message\n> {safe_message.replace(chr(10), chr(10) + '> ')[:1800]}",
        color=0x5865F2,
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    if author.display_avatar:
        embed.set_author(name=str(author), icon_url=author.display_avatar.url)
    else:
        embed.set_author(name=str(author))
    embed.add_field(name="👤 Author", value=f"{author.mention}\n`{author.id}`", inline=True)
    embed.add_field(name="📍 Channel", value=f"{channel.mention}\n`{channel.id}`", inline=True)
    embed.add_field(name="🔔 Pings", value="On" if pings else "Off", inline=True)
    add_footer_divider(embed)
    embed.set_footer(text=f"Host Assistant · {guild.name}")
    try:
        await log_channel.send(**v2(embed=embed), allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        print("/talk log failed:", e)


def can_use_talk(member, guild_id):
    """Host, configured VIP, admins, trusted users, or bot owners can use /talk."""
    if member is None:
        return False
    if member.id in BOT_OWNER_IDS or member.id in ALLOWED_USER_IDS:
        return True
    if is_trusted(member):
        return True
    perms = getattr(member, "guild_permissions", None)
    if perms and perms.administrator:
        return True
    if has_host_role(member, guild_id):
        return True
    vip_role = get_config(guild_id).get("superpower_role")
    return bool(vip_role) and any(r.id == vip_role for r in getattr(member, "roles", []))

@bot.tree.command(name="talk", description="Make Host Assistant send a message as the bot")
@app_commands.guild_only()
@app_commands.describe(message="Exactly what Host Assistant should say",
                       mentions="Turn on to actually ping the people and roles you mention (off by default)")
async def talk_cmd(interaction: discord.Interaction, message: str, mentions: bool = False):
    if core.RATE_LIMIT_TRIGGERED:
        await interaction.response.send_message("⚠️ Host Assistant is temporarily paused after a Discord rate limit.", ephemeral=True)
        return
    if not can_use_talk(interaction.user, interaction.guild_id):
        await interaction.response.send_message(
            "🔒 You need the Host role, VIP role, Administrator permission, or trusted access to use /talk.",
            ephemeral=True
        )
        return

    message = message.strip()
    if not message:
        await interaction.response.send_message("❌ Give me something to say.", ephemeral=True)
        return
    if len(message) > 2000:
        await interaction.response.send_message("❌ That message is too long. Discord messages are limited to 2,000 characters.", ephemeral=True)
        return

    now = time.time()
    last = _talk_cooldowns.get(interaction.user.id, 0)
    if now - last < TALK_COOLDOWN:
        remaining = max(1, int(TALK_COOLDOWN - (now - last)))
        await interaction.response.send_message(f"⏳ Slow down a little. Try again in {remaining}s.", ephemeral=True)
        return
    _talk_cooldowns[interaction.user.id] = now

    allowed = discord.AllowedMentions.none()
    if mentions:
        count, allowed = talk_pings(message, interaction.user, interaction.channel)
        recent = [t for t in _talk_pings.get(interaction.user.id, []) if now - t < TALK_PING_WINDOW]
        if len(recent) + count > TALK_PING_LIMIT:
            _talk_pings.pop(interaction.user.id, None)
            muted = await punish_ping_spam(interaction)
            await interaction.response.send_message(
                f"🔇 Too many pings, so the message wasn't sent and you were muted for {TALK_PING_MUTE_MINUTES} min."
                if muted else f"❌ Too many pings ({TALK_PING_LIMIT} per {TALK_PING_WINDOW // 60} min). The message wasn't sent.",
                ephemeral=True)
            return
        _talk_pings[interaction.user.id] = recent + [now] * count

    # Defer invisibly, then send the actual content as a normal bot message.
    # The slash command itself is not echoed, so the channel only sees the bot's message.
    await interaction.response.defer(ephemeral=True)
    try:
        await interaction.channel.send(message, allowed_mentions=allowed)
        await log_talk_usage(interaction.guild, interaction.user, interaction.channel, message, pings=mentions)
        await interaction.edit_original_response(content="✨ Sent successfully.")
    except discord.HTTPException as e:
        if getattr(e, "status", None) == 429:
            await emergency_rate_limit_shutdown("/talk")
        try:
            await interaction.edit_original_response(content="❌ Discord rejected the message. Host Assistant has been put into safe mode if this was a 429.")
        except Exception:
            pass
    except Exception as e:
        print("/talk error:", e)
        try:
            await interaction.edit_original_response(content="❌ I couldn't send that message.")
        except Exception:
            pass
