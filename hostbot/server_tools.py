"""Server tools: the /setup wizard and /talk."""

import datetime
import discord
import time
from discord import app_commands

from hostbot.core import ALLOWED_USER_IDS, BOT_OWNER_IDS, bot, db, get_config, is_trusted, save_db
from hostbot.hosts import ACHIEVEMENT_DEFS, ensure_achievement_roles
from hostbot.shared import (
    DIV, GuardedView, add_footer_divider, emergency_rate_limit_shutdown, has_host_role,
    is_server_admin,
)
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
    {"key": "trial_channel", "kind": "channel", "icon": "⚖️", "title": "Trial review channel", "required": False,
     "desc": "The public trial announcement and voting card are posted here. If you skip it, they go to the stats channel."},
    {"key": "trial_log_channel", "kind": "channel", "icon": "📜", "title": "Trial Logs channel", "required": False,
     "desc": "Detailed trial vote records and written reasons are posted here. Members who can view this channel can read its Trial Logs."},
    {"key": "achievement_roles", "kind": "achievement", "icon": "🏆", "title": "Achievement roles", "required": False,
     "desc": "Create and sync every Host Assistant achievement role. Existing roles are reused, so running setup again will not duplicate them."},
]
SETUP_COLOR = 0x5865F2

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

def _setup_value(guild, st, conf):
    if st["kind"] == "achievement":
        ready, total = _achievement_roles_ready(guild)
        if ready == total and total > 0:
            return f"✅ **{ready}/{total} roles ready**"
        return f"⚠️ **{ready}/{total} roles ready**"
    v = conf.get(st["key"])
    if not v:
        return "⚠️ *not set*" if st["required"] else "➖ *not set*"
    if st["kind"] == "role":
        return f"✅ <@&{v}>" if guild.get_role(v) else "⚠️ *role was deleted*"
    return f"✅ <#{v}>" if guild.get_channel(v) else "⚠️ *channel was deleted*"

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
    return out or ["✅ Everything looks good"]

class SetupAchievementRolesButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🏆 Create / Sync Achievement Roles", style=discord.ButtonStyle.primary, row=0)

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

    def embed(self):
        conf = get_config(self.guild.id)
        t = self.total
        if self.step < t:
            st = SETUP_STEPS[self.step]
            bar = "▰" * (self.step + 1) + "▱" * (t - self.step - 1)
            desc = f"# ⚙️ Setup · Step {self.step + 1} of {t}\n{bar}\n{DIV}\n"
            if self.note:
                desc += f"{self.note}\n\n"
            desc += f"## {st['icon']} {st['title']}\n{st['desc']}\n\n**Current:** {_setup_value(self.guild, st, conf)}\n{DIV}"
            emb = discord.Embed(color=SETUP_COLOR, description=desc)
            add_footer_divider(emb)
            emb.set_footer(text="Pick below · it saves instantly and moves to the next step")
            return emb

        roles = "\n".join(f"{s['icon']} **{s['title']}** · {_setup_value(self.guild, s, conf)}"
                          for s in SETUP_STEPS if s["kind"] == "role")
        chans = "\n".join(f"{s['icon']} **{s['title']}** · {_setup_value(self.guild, s, conf)}"
                          for s in SETUP_STEPS if s["kind"] == "channel")
        ready, total = _achievement_roles_ready(self.guild)
        checks = "\n".join(setup_checks(self.guild, conf))
        security = (
            f"🤖 **AI access:** VIP + Staff + Administrators + Bot Owners\n"
            f"🖼️ **Image vision:** Double Booster role only\n"
            f"⚖️ **Trial decisions:** Reviewed and decided manually by your team\n"
            f"💬 **/talk audit:** {'Configured' if conf.get('talk_log_channel') else 'Optional / not configured'}"
        )
        desc = (f"# ⚙️ Setup · Overview\n{'▰' * t}\n{DIV}\n"
                f"### 🎭 Roles\n{roles}\n{DIV}\n"
                f"### 🤖 AI & Security\n{security}\n{DIV}\n"
                f"### 📡 Channels\n{chans}\n{DIV}\n"
                f"### 🏆 Achievement Roles\n🏆 **Achievement roles** · **{ready}/{total} ready**\n{DIV}\n"
                f"### 🔍 Checks\n{checks}\n{DIV}")
        if self.note:
            desc = self.note + "\n\n" + desc
        emb = discord.Embed(color=SETUP_COLOR, description=desc[:4000])
        add_footer_divider(emb)
        emb.set_footer(text="Jump to any step with the menu · press Finish when you're done")
        return emb

    async def interaction_check(self, interaction):
        if not await super().interaction_check(interaction):
            return False
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel belongs to someone else. Run /setup yourself.", ephemeral=True)
            return False
        return True

    async def go(self, interaction, step=None, note=""):
        if step is not None:
            self.step = max(0, min(self.total, step))
        self.note = note
        self.rebuild()
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=self.embed(), view=self)
        else:
            await interaction.response.edit_message(embed=self.embed(), view=self)

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
    LABELS = {"back": ("◀ Back", discord.ButtonStyle.secondary), "next": ("Next ▶", discord.ButtonStyle.primary),
              "clear": ("Clear", discord.ButtonStyle.secondary), "finish": ("✅ Finish", discord.ButtonStyle.success)}

    def __init__(self, kind, disabled=False, skip=False):
        label, style = self.LABELS[kind]
        if kind == "next" and skip:
            label, style = "Skip ▶", discord.ButtonStyle.secondary
        super().__init__(label=label, style=style, disabled=disabled, row=1)
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
            emb = discord.Embed(color=0x2ECC71, description=(
                f"# ✅ Setup saved\n{DIV}\n{done} of {view.total} options configured.\n"
                "Run **/setup** any time to change something."))
            await interaction.response.edit_message(embed=emb, view=None)
            view.stop()

class SetupJump(discord.ui.Select):
    def __init__(self):
        opts = [discord.SelectOption(label=s["title"], value=str(i), emoji=s["icon"]) for i, s in enumerate(SETUP_STEPS)]
        super().__init__(placeholder="Jump to a step...", options=opts, row=0)

    async def callback(self, interaction):
        await self.view.go(interaction, step=int(self.values[0]))

@bot.tree.command(name="setup", description="Step-by-step setup dashboard")
@app_commands.guild_only()
async def setup_cmd(interaction: discord.Interaction):
    if not is_server_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    wizard = SetupWizard(interaction.guild, interaction.user.id)
    await interaction.response.send_message(embed=wizard.embed(), view=wizard, ephemeral=True)
# ==========================================
# /talk — controlled bot voice message
# ==========================================
TALK_COOLDOWN = 4
_talk_cooldowns = {}

async def log_talk_usage(guild, author, channel, message):
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
    add_footer_divider(embed)
    embed.set_footer(text=f"Host Assistant · {guild.name}")
    try:
        await log_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
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
@app_commands.describe(message="Exactly what Host Assistant should say")
async def talk_cmd(interaction: discord.Interaction, message: str):
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

    # Defer invisibly, then send the actual content as a normal bot message.
    # The slash command itself is not echoed, so the channel only sees the bot's message.
    await interaction.response.defer(ephemeral=True)
    try:
        await interaction.channel.send(
            message,
            allowed_mentions=discord.AllowedMentions.none()
        )
        await log_talk_usage(interaction.guild, interaction.user, interaction.channel, message)
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
