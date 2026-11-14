"""Server protection: anti-nuke (stops someone mass-deleting channels/roles or mass-banning)
and server layout backups with /backup create · list · restore."""

import asyncio
import datetime
import discord
import json
import os
import time
from discord import app_commands

from hostbot.core import BOT_OWNER_IDS, ALLOWED_USER_IDS, bot, db, is_trusted, mongo_db, save_db
from hostbot.shared import BRAND, DIV, GuardedView, add_footer_divider, dm_owners, log_action

# ==========================================
# SETTINGS
# ==========================================
NUKE_WINDOW = 180               # seconds (3 minutes)
NUKE_DELETE_LIMIT = 3           # important channels + roles deleted inside the window
NUKE_BAN_LIMIT = 3              # bans + kicks inside the window
IMPORTANT_CHANNEL_MESSAGES = 50 # a channel "matters" once it has at least this many messages
BACKUP_EVERY = 86400            # automatic backup once a day
BACKUP_KEEP = 7                 # how many automatic backups to keep per server
BACKUP_DIR = "backups"

# ==========================================
# MESSAGE COUNTS (so the anti-nuke knows which channels matter)
# ==========================================
_counts_dirty = False


def msg_counts(guild_id):
    return db.setdefault("msg_counts", {}).setdefault(str(guild_id), {})


@bot.listen("on_message")
async def _count_message(msg):
    global _counts_dirty
    if msg.guild is None or msg.author.bot:
        return
    counts = msg_counts(msg.guild.id)
    cid = str(msg.channel.id)
    if counts.get(cid, 0) < 100000:
        counts[cid] = counts.get(cid, 0) + 1
        _counts_dirty = True


def flush_msg_counts():
    """Called by the background timer: save counts at most every 30 seconds instead of on every message."""
    global _counts_dirty
    if _counts_dirty:
        _counts_dirty = False
        save_db(db)


async def seed_msg_counts(guild):
    """First time only: count up to 200 recent messages per channel so channels that already have
    history count as important straight away (not just after the bot has seen 50 new messages)."""
    seeded = db.setdefault("msg_counts_seeded", [])
    if guild.id in seeded:
        return
    counts = msg_counts(guild.id)
    me = guild.me
    for ch in list(guild.text_channels) + list(getattr(guild, "voice_channels", [])):
        perms = ch.permissions_for(me)
        if not (perms.view_channel and perms.read_message_history):
            continue
        try:
            n = 0
            async for _ in ch.history(limit=200):
                n += 1
            counts[str(ch.id)] = max(counts.get(str(ch.id), 0), n)
        except discord.HTTPException:
            pass
        await asyncio.sleep(0.5)
    seeded.append(guild.id)
    save_db(db)


# ==========================================
# ANTI-NUKE
# ==========================================
_actions = {}   # (guild id, user id, kind) -> timestamps
_stopping = set()  # (guild id, user id) being handled right now, so one nuke = one alert


def is_protected(guild, user_id):
    """People the anti-nuke never punishes: the server owner, the bot itself, bot owners and /trust users."""
    if user_id in (guild.owner_id, bot.user.id) or user_id in BOT_OWNER_IDS or user_id in ALLOWED_USER_IDS:
        return True
    return user_id in db.get("trusted_users", {}).get(str(guild.id), [])


def _record(guild_id, user_id, kind):
    now = time.time()
    key = (guild_id, user_id, kind)
    hits = [t for t in _actions.get(key, []) if now - t < NUKE_WINDOW] + [now]
    _actions[key] = hits
    return len(hits)


async def stop_nuker(guild, user_id, why, details):
    """Strip every role the bot can remove, then alert the owners."""
    _actions.pop((guild.id, user_id, "delete"), None)
    _actions.pop((guild.id, user_id, "ban"), None)
    member = guild.get_member(user_id)
    removed, kept = [], []
    if member:
        me = guild.me
        for role in member.roles:
            if role.is_default() or role.managed:
                continue
            (removed if role < me.top_role else kept).append(role)
        if removed:
            try:
                await member.remove_roles(*removed, reason=f"Anti-nuke: {why}")
            except discord.HTTPException as e:
                print("anti-nuke role removal failed:", e)
                kept += removed
                removed = []
    incidents = db.setdefault("nuke_incidents", {}).setdefault(str(guild.id), [])
    incidents.append({"user": user_id, "at": int(time.time()), "why": why,
                      "roles": [r.id for r in removed], "details": details[-10:]})
    del incidents[:-20]
    save_db(db)

    status = (f"Removed **{len(removed)}** role(s): " + ", ".join(r.name for r in removed) if removed
              else "⚠️ **I couldn't remove any roles.**")
    if kept:
        status += ("\n⚠️ Still has (above my role, move my role higher): " + ", ".join(r.name for r in kept))
    if not member:
        status = "They're no longer in the server."
    text = (f"<@{user_id}> **{why}** within {NUKE_WINDOW // 60} minutes.\n{status}\n\n"
            "**What they did:**\n" + "\n".join(f"• {d}" for d in details[-10:])
            + f"\n\nTo reverse it: `/undo user:{user_id}`")
    await log_action(guild, "🚨 Anti-Nuke Triggered", text, color=0xE74C3C, channel_key="ai_log_channel")
    await dm_owners(f"🚨 **Anti-nuke triggered in {guild.name}**\n" + text, key=f"nuke_{guild.id}_{user_id}", cooldown=60)
    try:
        owner = guild.owner or await bot.fetch_user(guild.owner_id)
        if owner and owner.id not in BOT_OWNER_IDS:
            await owner.send(f"🚨 **Anti-nuke triggered in {guild.name}**\n" + text[:1800])
    except discord.HTTPException:
        pass


_details = {}   # (guild id, user id) -> recent action descriptions for the alert


@bot.listen("on_audit_log_entry_create")
async def _watch_audit_log(entry):
    guild = entry.guild
    user_id = getattr(entry.user, "id", None) or entry.user_id
    if not user_id or is_protected(guild, user_id):
        return
    A = discord.AuditLogAction
    kind, label = None, None
    if entry.action == A.channel_delete:
        cid = getattr(entry.target, "id", None)
        name = getattr(entry.before, "name", None) or "a channel"
        n = msg_counts(guild.id).pop(str(cid), 0) if cid else 0
        if n >= IMPORTANT_CHANNEL_MESSAGES:
            kind, label = "delete", f"Deleted channel **#{name}** ({n}+ messages)"
    elif entry.action == A.role_delete:
        name = getattr(entry.before, "name", None) or "a role"
        kind, label = "delete", f"Deleted role **@{name}**"
    elif entry.action in (A.ban, A.kick):
        kind = "ban"
        verb = "Banned" if entry.action == A.ban else "Kicked"
        label = f"{verb} <@{getattr(entry.target, 'id', 0)}>"
    if not kind:
        return

    d = _details.setdefault((guild.id, user_id), [])
    d.append(f"{label} · <t:{int(time.time())}:T>")
    del d[:-15]
    hits = _record(guild.id, user_id, kind)
    limit = NUKE_DELETE_LIMIT if kind == "delete" else NUKE_BAN_LIMIT
    if hits >= limit and (guild.id, user_id) not in _stopping:
        _stopping.add((guild.id, user_id))
        why = (f"Deleted {hits} important channels/roles" if kind == "delete" else f"Banned/kicked {hits} members")
        try:
            await stop_nuker(guild, user_id, why, _details.pop((guild.id, user_id), []))
        finally:
            _stopping.discard((guild.id, user_id))


def cleanup_nuke_tracking():
    now = time.time()
    for k in [k for k, v in _actions.items() if not v or now - v[-1] > NUKE_WINDOW]:
        _actions.pop(k, None)
        _details.pop(k[:2], None)


# ==========================================
# BACKUPS (layout only: roles, categories, channels, permissions, who has which role)
# ==========================================
backup_col = mongo_db["server_backups"] if mongo_db is not None else None


def _overwrites(ch):
    out = []
    for target, ow in ch.overwrites.items():
        allow, deny = ow.pair()
        out.append({"id": target.id, "type": "role" if isinstance(target, discord.Role) else "member",
                    "allow": allow.value, "deny": deny.value})
    return out


def snapshot(guild):
    roles = [{"id": r.id, "name": r.name, "color": r.color.value, "hoist": r.hoist, "mentionable": r.mentionable,
              "permissions": r.permissions.value, "position": r.position}
             for r in guild.roles if not r.is_default() and not r.managed]
    everyone = guild.default_role.permissions.value
    channels = []
    for ch in guild.channels:
        c = {"id": ch.id, "name": ch.name, "type": str(ch.type), "position": ch.position,
             "category": ch.category_id, "overwrites": _overwrites(ch)}
        if isinstance(ch, discord.TextChannel):
            c.update(topic=ch.topic, nsfw=ch.nsfw, slowmode=ch.slowmode_delay)
        elif isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            c.update(bitrate=ch.bitrate, user_limit=ch.user_limit)
        elif isinstance(ch, discord.ForumChannel):
            c.update(topic=ch.topic, nsfw=ch.nsfw)
        channels.append(c)
    role_ids = {r["id"] for r in roles}
    members = {str(m.id): [r.id for r in m.roles if r.id in role_ids] for m in guild.members if not m.bot}
    members = {k: v for k, v in members.items() if v}
    return {"roles": roles, "everyone_perms": everyone, "channels": channels, "members": members}


def _store_backup(guild_id, data, reason, by):
    now = int(time.time())
    doc = {"_id": f"{guild_id}:{now}", "guild_id": guild_id, "created_at": now, "reason": reason, "by": by,
           "data": data}
    if backup_col is not None:
        backup_col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        autos = list(backup_col.find({"guild_id": guild_id, "reason": "auto"}, {"_id": 1}).sort("created_at", -1))
        for old in autos[BACKUP_KEEP:]:
            backup_col.delete_one({"_id": old["_id"]})
    else:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        with open(os.path.join(BACKUP_DIR, f"{guild_id}_{now}.json"), "w") as f:
            json.dump(doc, f)
        autos = sorted((x for x in os.listdir(BACKUP_DIR) if x.startswith(f"{guild_id}_")), reverse=True)
        autos = [x for x in autos if _load_file(x).get("reason") == "auto"]
        for old in autos[BACKUP_KEEP:]:
            os.remove(os.path.join(BACKUP_DIR, old))
    return doc


def _load_file(name):
    try:
        with open(os.path.join(BACKUP_DIR, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def list_backups(guild_id):
    """Newest first, without the bulky data."""
    if backup_col is not None:
        return list(backup_col.find({"guild_id": guild_id}, {"data": 0}).sort("created_at", -1).limit(25))
    if not os.path.isdir(BACKUP_DIR):
        return []
    docs = [_load_file(x) for x in os.listdir(BACKUP_DIR) if x.startswith(f"{guild_id}_")]
    return sorted((d for d in docs if d), key=lambda d: -d["created_at"])[:25]


def load_backup(backup_id):
    if backup_col is not None:
        return backup_col.find_one({"_id": backup_id})
    gid, ts = backup_id.split(":")
    return _load_file(f"{gid}_{ts}.json") or None


async def make_backup(guild, reason="auto", by=None):
    data = snapshot(guild)
    return await asyncio.get_running_loop().run_in_executor(None, _store_backup, guild.id, data, reason, by)


_backup_running = False


async def run_daily_backups():
    """Runs in the background so a slow backup never holds up the event timers."""
    global _backup_running
    if _backup_running:
        return
    _backup_running = True
    try:
        await _run_daily_backups()
    finally:
        _backup_running = False


async def _run_daily_backups():
    last = db.setdefault("last_backup", {})
    now = time.time()
    for guild in bot.guilds:
        if now - last.get(str(guild.id), 0) < BACKUP_EVERY:
            continue
        last[str(guild.id)] = int(now)
        save_db(db)
        try:
            await make_backup(guild, "auto")
            await seed_msg_counts(guild)
        except Exception as e:
            print("daily backup failed:", e)


# ---------- restore ----------
CHANNEL_TYPES = {"text": discord.ChannelType.text, "voice": discord.ChannelType.voice,
                 "category": discord.ChannelType.category, "news": discord.ChannelType.news,
                 "stage_voice": discord.ChannelType.stage_voice, "forum": discord.ChannelType.forum}


def restore_plan(guild, data):
    have_roles = {r.id for r in guild.roles}
    have_channels = {c.id for c in guild.channels}
    missing_roles = [r for r in data["roles"] if r["id"] not in have_roles]
    missing_channels = [c for c in data["channels"] if c["id"] not in have_channels and c["type"] in CHANNEL_TYPES]
    changed_roles = []
    for r in data["roles"]:
        cur = guild.get_role(r["id"])
        if cur and cur < guild.me.top_role and cur.permissions.value != r["permissions"]:
            changed_roles.append(r)
    return missing_roles, missing_channels, changed_roles


async def do_restore(guild, data, fix_permissions):
    me = guild.me
    role_map = {r.id: r.id for r in guild.roles}   # old id -> current id
    done = {"roles": 0, "channels": 0, "perms": 0, "member_roles": 0, "failed": 0}
    missing_roles, missing_channels, changed_roles = restore_plan(guild, data)
    reason = "Restored from Host Assistant backup"

    # 1. Roles (top of the list first so the order comes out close to the original).
    for r in sorted(missing_roles, key=lambda r: -r["position"]):
        perms = discord.Permissions(r["permissions"])
        if not me.guild_permissions.administrator:
            perms = discord.Permissions(perms.value & me.guild_permissions.value)
        try:
            new = await guild.create_role(name=r["name"], color=discord.Color(r["color"]), hoist=r["hoist"],
                                          mentionable=r["mentionable"], permissions=perms, reason=reason)
            role_map[r["id"]] = new.id
            done["roles"] += 1
        except discord.HTTPException as e:
            print("restore role failed:", r["name"], e)
            done["failed"] += 1
        await asyncio.sleep(0.7)

    # Put recreated roles back roughly where they were (below the bot's own role).
    try:
        wanted = {}
        for r in missing_roles:
            new = guild.get_role(role_map.get(r["id"], 0))
            if new:
                wanted[new] = max(1, min(r["position"], me.top_role.position - 1))
        if wanted:
            await guild.edit_role_positions(positions=wanted, reason=reason)
    except discord.HTTPException as e:
        print("restore role positions failed:", e)

    # 2. Permissions of roles the mod changed.
    if fix_permissions:
        for r in changed_roles:
            role = guild.get_role(r["id"])
            try:
                await role.edit(permissions=discord.Permissions(r["permissions"]), reason=reason)
                done["perms"] += 1
            except discord.HTTPException:
                done["failed"] += 1
            await asyncio.sleep(0.5)

    def overwrites_for(c):
        out = {}
        for ow in c.get("overwrites", []):
            if ow["type"] == "role":
                target = guild.get_role(role_map.get(ow["id"], 0))
                if ow["id"] == guild.id:
                    target = guild.default_role
            else:
                target = guild.get_member(ow["id"])
            if target:
                out[target] = discord.PermissionOverwrite.from_pair(discord.Permissions(ow["allow"]),
                                                                     discord.Permissions(ow["deny"]))
        return out

    # 3. Categories first, then the channels inside them.
    chan_map = {c.id: c.id for c in guild.channels}
    cats = [c for c in missing_channels if c["type"] == "category"]
    others = [c for c in missing_channels if c["type"] != "category"]
    for c in sorted(cats, key=lambda c: c["position"]) + sorted(others, key=lambda c: c["position"]):
        try:
            kw = {"overwrites": overwrites_for(c), "reason": reason, "position": c["position"]}
            parent = guild.get_channel(chan_map.get(c.get("category"), 0)) if c.get("category") else None
            t = c["type"]
            if t == "category":
                new = await guild.create_category(c["name"], **kw)
            elif t in ("text", "news"):
                new = await guild.create_text_channel(c["name"], category=parent, topic=c.get("topic"),
                                                      nsfw=c.get("nsfw", False),
                                                      slowmode_delay=c.get("slowmode", 0), **kw)
            elif t == "voice":
                new = await guild.create_voice_channel(c["name"], category=parent, user_limit=c.get("user_limit", 0), **kw)
            elif t == "stage_voice":
                new = await guild.create_stage_channel(c["name"], category=parent, **kw)
            elif t == "forum":
                new = await guild.create_forum(c["name"], category=parent, topic=c.get("topic"), **kw)
            else:
                continue
            chan_map[c["id"]] = new.id
            done["channels"] += 1
        except discord.HTTPException as e:
            print("restore channel failed:", c["name"], e)
            done["failed"] += 1
        await asyncio.sleep(0.7)

    # 4. Give recreated roles back to the members who had them.
    recreated = {old: new for old, new in role_map.items() if old != new}
    if recreated:
        for uid, old_roles in data.get("members", {}).items():
            member = guild.get_member(int(uid))
            give = [guild.get_role(recreated[r]) for r in old_roles if r in recreated]
            give = [r for r in give if r and r not in member.roles] if member else []
            if give:
                try:
                    await member.add_roles(*give, reason=reason)
                    done["member_roles"] += 1
                except discord.HTTPException:
                    done["failed"] += 1
                await asyncio.sleep(0.5)
    return done


def can_restore(member):
    return member.id == member.guild.owner_id or member.id in BOT_OWNER_IDS or is_trusted(member)


class RestoreConfirm(GuardedView):
    def __init__(self, author_id, backup):
        super().__init__(timeout=120)
        self.author_id, self.backup = author_id, backup

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the person who started the restore can confirm it.", ephemeral=True)
            return False
        return await super().interaction_check(interaction)

    async def _run(self, interaction, fix_permissions):
        await interaction.response.edit_message(content="⏳ Restoring... this can take a few minutes.", embed=None, view=None)
        self.stop()
        done = await do_restore(interaction.guild, self.backup["data"], fix_permissions)
        summary = (f"Recreated **{done['roles']}** role(s) and **{done['channels']}** channel(s)"
                   + (f", reset permissions on **{done['perms']}** role(s)" if fix_permissions else "")
                   + f", gave roles back to **{done['member_roles']}** member(s)."
                   + (f"\n⚠️ {done['failed']} thing(s) failed, check that my role is at the top." if done["failed"] else ""))
        await interaction.edit_original_response(content="✅ " + summary)
        await log_action(interaction.guild, "Server Restored",
                         f"**By:** {interaction.user.mention}\n**Backup from:** <t:{self.backup['created_at']}:f>\n{summary}",
                         color=0xE74C3C, channel_key="ai_log_channel")

    @discord.ui.button(label="Restore missing things", style=discord.ButtonStyle.success)
    async def missing_only(self, interaction, btn):
        await self._run(interaction, fix_permissions=False)

    @discord.ui.button(label="Restore + fix role permissions", style=discord.ButtonStyle.danger)
    async def full(self, interaction, btn):
        await self._run(interaction, fix_permissions=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, btn):
        await interaction.response.edit_message(content="Restore cancelled.", embed=None, view=None)
        self.stop()


backup_group = app_commands.Group(name="backup", description="Server layout backups", guild_only=True)
bot.tree.add_command(backup_group)


@backup_group.command(name="create", description="Save a backup of the server's roles, channels and permissions now")
async def backup_create(interaction: discord.Interaction):
    if not can_restore(interaction.user):
        await interaction.response.send_message("Only the server owner and trusted users can do that.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    doc = await make_backup(interaction.guild, "manual", interaction.user.id)
    d = doc["data"]
    await interaction.followup.send(
        f"💾 Backup saved: **{len(d['roles'])}** roles, **{len(d['channels'])}** channels, "
        f"roles of **{len(d['members'])}** members.", ephemeral=True)


async def _backup_autocomplete(interaction: discord.Interaction, current: str):
    out = []
    for b in list_backups(interaction.guild_id):
        when = datetime.datetime.fromtimestamp(b["created_at"], datetime.timezone.utc).strftime("%b %d %Y, %H:%M UTC")
        label = f"{when} · {'automatic' if b['reason'] == 'auto' else 'manual'}"
        if current.lower() in label.lower():
            out.append(app_commands.Choice(name=label, value=b["_id"]))
    return out[:25]


@backup_group.command(name="list", description="See the saved backups")
async def backup_list(interaction: discord.Interaction):
    if not can_restore(interaction.user):
        await interaction.response.send_message("Only the server owner and trusted users can do that.", ephemeral=True)
        return
    rows = list_backups(interaction.guild_id)
    lines = [f"• <t:{b['created_at']}:f> (<t:{b['created_at']}:R>) · {'🤖 automatic' if b['reason'] == 'auto' else '👤 manual'}"
             for b in rows]
    emb = discord.Embed(color=0x3498DB, description=(
        f"# 💾 Backups · {len(rows)}\n{DIV}\n" + ("\n".join(lines) or "*No backups yet. Use `/backup create`.*")))
    add_footer_divider(emb)
    emb.set_footer(text=f"One automatic backup a day, the last {BACKUP_KEEP} are kept · {BRAND}")
    await interaction.response.send_message(embed=emb, ephemeral=True)


@backup_group.command(name="restore", description="Rebuild deleted roles and channels from a backup")
@app_commands.describe(backup="Which backup to restore from")
@app_commands.autocomplete(backup=_backup_autocomplete)
async def backup_restore(interaction: discord.Interaction, backup: str):
    if not can_restore(interaction.user):
        await interaction.response.send_message("Only the server owner and trusted users can do that.", ephemeral=True)
        return
    doc = load_backup(backup)
    if not doc or doc.get("guild_id") != interaction.guild_id:
        await interaction.response.send_message("I couldn't find that backup.", ephemeral=True)
        return
    missing_roles, missing_channels, changed_roles = restore_plan(interaction.guild, doc["data"])
    if not (missing_roles or missing_channels or changed_roles):
        await interaction.response.send_message("Nothing to restore: every role and channel from that backup still exists "
                                                "and role permissions match.", ephemeral=True)
        return

    def names(items, prefix):
        shown = ", ".join(f"{prefix}{i['name']}" for i in items[:15])
        return (shown + (f" *+{len(items) - 15} more*" if len(items) > 15 else "")) or "*none*"

    emb = discord.Embed(color=0xF1C40F, description=(
        f"# ♻️ Restore from <t:{doc['created_at']}:f>\n{DIV}\n"
        f"### Roles to recreate · {len(missing_roles)}\n{names(missing_roles, '@')}\n"
        f"### Channels to recreate · {len(missing_channels)}\n{names(missing_channels, '#')}\n"
        f"### Roles with changed permissions · {len(changed_roles)}\n{names(changed_roles, '@')}\n{DIV}\n"
        "**Restore missing things** only adds what was deleted.\n"
        "**Restore + fix role permissions** also puts role permissions back to how they were.\n"
        "-# Nothing is ever deleted. Messages in deleted channels can't be brought back."))
    add_footer_divider(emb)
    emb.set_footer(text="Confirm within 2 minutes")
    await interaction.response.send_message(embed=emb, view=RestoreConfirm(interaction.user.id, doc), ephemeral=True)


# ==========================================
# /undo: reverse what one person did, straight from the Audit Log (kept by Discord for 45 days)
# ==========================================
UNDO_MAX_ENTRIES = 1000
_CHANNEL_FIELDS = ("name", "topic", "nsfw", "slowmode_delay", "bitrate", "user_limit")
_ROLE_FIELDS = ("name", "permissions", "colour", "hoist", "mentionable")


class UndoPlan:
    """Everything one person did, boiled down to the original state that needs putting back."""
    def __init__(self):
        self.deleted_roles = {}      # old role id -> {name, permissions, colour, hoist, mentionable}
        self.deleted_channels = {}   # old channel id -> {name, type, position, parent, overwrites, ...}
        self.role_edits = {}         # role id -> original values of the fields they changed
        self.channel_edits = {}      # channel id -> original values of the fields they changed
        self.overwrites = {}         # (channel id, target id) -> ("remove", None) or ("set", (allow, deny, is_role))
        self.member_roles = {}       # member id -> {"add": set(role ids), "remove": set(role ids)}
        self.created_roles = {}      # role id -> name (deleted again when undoing everything)
        self.created_channels = {}   # channel id -> name
        self.banned = {}             # user id -> name of people they banned (unbanned when undoing)
        self.kicks = 0               # kicks can't be reversed, only reported
        self.actors = {}             # user id -> number of their actions included

    def empty(self):
        return not (self.deleted_roles or self.deleted_channels or self.role_edits or self.channel_edits
                    or self.overwrites or self.member_roles or self.created_roles or self.created_channels
                    or self.banned)


def _get(obj, attr):
    return getattr(obj, attr, None)


async def build_undo_plan(guild, user_id, hours):
    """user_id=None means everyone, except bots, the server owner and trusted users."""
    A = discord.AuditLogAction
    handled = {A.role_delete, A.role_create, A.role_update, A.channel_delete, A.channel_create, A.channel_update,
               A.overwrite_create, A.overwrite_update, A.overwrite_delete, A.member_role_update,
               A.ban, A.unban, A.kick}
    plan = UndoPlan()
    after = discord.utils.utcnow() - datetime.timedelta(hours=hours)
    kw = {"limit": UNDO_MAX_ENTRIES * (3 if user_id is None else 1), "after": after, "oldest_first": True}
    if user_id is not None:
        kw["user"] = discord.Object(id=user_id)
    async for e in guild.audit_logs(**kw):
        if e.action not in handled:
            continue
        actor = getattr(e.user, "id", None) or getattr(e, "user_id", None)
        if user_id is None:
            if actor is None or getattr(e.user, "bot", False) or is_protected(guild, actor):
                continue
        if actor:
            plan.actors[actor] = plan.actors.get(actor, 0) + 1
        tid = _get(e.target, "id")
        b, a = e.before, e.after
        if e.action == A.role_delete:
            if plan.created_roles.pop(tid, None) is None:   # their own new role deleted again = nothing to do
                plan.deleted_roles[tid] = {f: _get(b, f) for f in _ROLE_FIELDS}
        elif e.action == A.role_create:
            plan.created_roles[tid] = _get(a, "name") or "?"
        elif e.action == A.role_update:
            orig = plan.role_edits.setdefault(tid, {})
            for f in _ROLE_FIELDS:
                if _get(b, f) is not None and f not in orig:
                    orig[f] = _get(b, f)   # keep the value from BEFORE their first change
        elif e.action == A.channel_delete:
            if plan.created_channels.pop(tid, None) is not None:
                continue   # their own new channel deleted again = nothing to do
            plan.deleted_channels[tid] = {
                "name": _get(b, "name"), "type": _get(b, "type"), "position": _get(b, "position"),
                "parent": _get(b, "parent_id"), "overwrites": _get(b, "overwrites") or [],
                **{f: _get(b, f) for f in _CHANNEL_FIELDS[1:]}}
        elif e.action == A.channel_create:
            plan.created_channels[tid] = _get(a, "name") or "?"
        elif e.action == A.channel_update:
            orig = plan.channel_edits.setdefault(tid, {})
            for f in _CHANNEL_FIELDS:
                if _get(b, f) is not None and f not in orig:
                    orig[f] = _get(b, f)
        elif e.action in (A.overwrite_create, A.overwrite_update, A.overwrite_delete):
            target = e.extra
            key = (tid, _get(target, "id"))
            if key in plan.overwrites:
                continue  # only the state before their FIRST change matters
            is_role = isinstance(target, discord.Role) or getattr(target, "type", None) is discord.Role
            if e.action == A.overwrite_create:
                plan.overwrites[key] = ("remove", None)
            else:
                plan.overwrites[key] = ("set", (_get(b, "allow"), _get(b, "deny"), is_role))
        elif e.action == A.member_role_update:
            rec = plan.member_roles.setdefault(tid, {"add": set(), "remove": set()})
            for r in _get(a, "roles") or []:      # they gave these -> take them away again
                if r.id in rec["add"]:
                    rec["add"].discard(r.id)
                else:
                    rec["remove"].add(r.id)
            for r in _get(b, "roles") or []:      # they took these -> give them back
                if r.id in rec["remove"]:
                    rec["remove"].discard(r.id)
                else:
                    rec["add"].add(r.id)
        elif e.action == A.ban:
            plan.banned[tid] = str(e.target) if e.target else str(tid)
        elif e.action == A.unban:
            plan.banned.pop(tid, None)
        elif e.action == A.kick:
            plan.kicks += 1
    # Settings they changed on things they created don't matter: those get deleted.
    for cid in plan.created_channels:
        plan.channel_edits.pop(cid, None)
    plan.overwrites = {k: v for k, v in plan.overwrites.items() if k[0] not in plan.created_channels}
    for rid in plan.created_roles:
        plan.role_edits.pop(rid, None)
    plan.member_roles = {m: r for m, r in plan.member_roles.items() if r["add"] or r["remove"]}
    return plan


async def run_undo(guild, plan, remove_created=True):
    me = guild.me
    reason = "Undo via Host Assistant (reversing audit log actions)"
    role_map, chan_map = {}, {}
    done = {"roles": 0, "channels": 0, "role_edits": 0, "channel_edits": 0, "overwrites": 0,
            "members": 0, "removed": 0, "unbanned": 0, "failed": 0}

    def role(rid):
        return guild.get_role(role_map.get(rid, rid))

    # 1. Recreate deleted roles with their original permissions, color and settings.
    for old_id, r in plan.deleted_roles.items():
        kw = {"name": r["name"] or "restored-role", "reason": reason}
        if r["permissions"] is not None:
            kw["permissions"] = r["permissions"]
        if r["colour"] is not None:
            kw["colour"] = r["colour"]
        for f in ("hoist", "mentionable"):
            if r[f] is not None:
                kw[f] = r[f]
        try:
            new = await guild.create_role(**kw)
            role_map[old_id] = new.id
            done["roles"] += 1
        except discord.HTTPException as e:
            print("undo: role create failed:", e)
            done["failed"] += 1
        await asyncio.sleep(0.7)

    # 2. Put back settings of roles they edited (including roles that were edited, then deleted).
    for rid, orig in plan.role_edits.items():
        r = role(rid)
        if not r or not (r < me.top_role) or not orig:
            continue
        try:
            await r.edit(reason=reason, **orig)
            done["role_edits"] += 1
        except discord.HTTPException:
            done["failed"] += 1
        await asyncio.sleep(0.5)

    def mapped_overwrites(ows):
        out = {}
        for target, ow in ows:
            tid = getattr(target, "id", None)
            is_role = isinstance(target, discord.Role) or getattr(target, "type", None) is discord.Role
            t = (guild.default_role if tid == guild.id else role(tid)) if is_role else guild.get_member(tid)
            if t:
                out[t] = ow
        return out

    # 3. Recreate deleted categories first, then the channels inside them.
    items = sorted(plan.deleted_channels.items(),
                   key=lambda kv: (kv[1]["type"] != discord.ChannelType.category, kv[1]["position"] or 0))
    for old_id, c in items:
        t = c["type"]
        kw = {"overwrites": mapped_overwrites(c["overwrites"]), "reason": reason}
        if c["position"] is not None:
            kw["position"] = c["position"]
        parent_id = int(c["parent"]) if c["parent"] else None
        parent = guild.get_channel(chan_map.get(parent_id, parent_id)) if parent_id else None
        if t != discord.ChannelType.category and isinstance(parent, discord.CategoryChannel):
            kw["category"] = parent
        name = c["name"] or "restored-channel"
        try:
            if t == discord.ChannelType.category:
                new = await guild.create_category(name, **kw)
            elif t in (discord.ChannelType.voice,):
                new = await guild.create_voice_channel(name, user_limit=c.get("user_limit") or 0, **kw)
            elif t == discord.ChannelType.stage_voice:
                new = await guild.create_stage_channel(name, **kw)
            elif t == discord.ChannelType.forum:
                new = await guild.create_forum(name, topic=c.get("topic"), **kw)
            else:
                new = await guild.create_text_channel(name, topic=c.get("topic"), nsfw=bool(c.get("nsfw")),
                                                      slowmode_delay=c.get("slowmode_delay") or 0, **kw)
            chan_map[old_id] = new.id
            done["channels"] += 1
        except discord.HTTPException as e:
            print("undo: channel create failed:", name, e)
            done["failed"] += 1
        await asyncio.sleep(0.7)

    # 4. Put back channel settings and channel permissions they changed.
    for cid, orig in plan.channel_edits.items():
        ch = guild.get_channel(chan_map.get(cid, cid))
        orig = {k: v for k, v in orig.items() if v is not None and hasattr(ch, k if k != "slowmode_delay" else "slowmode_delay")}
        if not ch or not orig:
            continue
        try:
            await ch.edit(reason=reason, **orig)
            done["channel_edits"] += 1
        except (discord.HTTPException, TypeError):
            done["failed"] += 1
        await asyncio.sleep(0.5)
    for (cid, tid), (kind, val) in plan.overwrites.items():
        ch = guild.get_channel(chan_map.get(cid, cid))
        if not ch or cid in plan.deleted_channels:
            continue  # recreated channels already got their original permissions
        if kind == "remove":
            target = role(tid) or guild.get_member(tid)
            new_ow = None
        else:
            allow, deny, is_role = val
            target = (guild.default_role if tid == guild.id else role(tid)) if is_role else guild.get_member(tid)
            cur = ch.overwrites_for(target) if target else None
            cur_allow, cur_deny = cur.pair() if cur else (discord.Permissions.none(), discord.Permissions.none())
            new_ow = discord.PermissionOverwrite.from_pair(allow or cur_allow, deny or cur_deny)
        if not target:
            continue
        try:
            await ch.set_permissions(target, overwrite=new_ow, reason=reason)
            done["overwrites"] += 1
        except discord.HTTPException:
            done["failed"] += 1
        await asyncio.sleep(0.5)

    # 5. Give back roles they took from people, and take away roles they handed out.
    for mid, rec in plan.member_roles.items():
        m = guild.get_member(mid)
        if not m:
            continue
        give = [r for r in (role(i) for i in rec["add"]) if r and r < me.top_role and r not in m.roles]
        take = [r for r in (role(i) for i in rec["remove"])
                if r and r < me.top_role and r in m.roles and not (remove_created and r.id in plan.created_roles)]
        try:
            if give:
                await m.add_roles(*give, reason=reason)
            if take:
                await m.remove_roles(*take, reason=reason)
            if give or take:
                done["members"] += 1
        except discord.HTTPException:
            done["failed"] += 1
        await asyncio.sleep(0.5)

    # 6. Unban the people they banned.
    for uid in plan.banned:
        try:
            await guild.unban(discord.Object(id=uid), reason=reason)
            done["unbanned"] += 1
        except discord.NotFound:
            pass  # already unbanned
        except discord.HTTPException:
            done["failed"] += 1
        await asyncio.sleep(0.5)

    # 7. Delete the channels and roles they created (unless asked to keep them).
    if remove_created:
        for cid in plan.created_channels:
            ch = guild.get_channel(cid)
            if not ch:
                continue
            try:
                await ch.delete(reason=reason)
                done["removed"] += 1
            except discord.HTTPException:
                done["failed"] += 1
            await asyncio.sleep(0.7)
        for rid in plan.created_roles:
            r = guild.get_role(rid)
            if not r or r.managed or not (r < me.top_role):
                continue
            try:
                await r.delete(reason=reason)
                done["removed"] += 1
            except discord.HTTPException:
                done["failed"] += 1
            await asyncio.sleep(0.7)
    return done


class UndoConfirm(GuardedView):
    def __init__(self, author_id, target_id, plan):
        super().__init__(timeout=180)
        self.author_id, self.target_id, self.plan = author_id, target_id, plan

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the person who ran /undo can confirm it.", ephemeral=True)
            return False
        return await super().interaction_check(interaction)

    async def _run(self, interaction, remove_created):
        await interaction.response.edit_message(content="⏳ Undoing... this can take a few minutes.", embed=None, view=None)
        self.stop()
        d = await run_undo(interaction.guild, self.plan, remove_created)
        summary = (f"Recreated **{d['roles']}** role(s) and **{d['channels']}** channel(s), "
                   f"reverted **{d['role_edits']}** role edit(s), **{d['channel_edits']}** channel edit(s) and "
                   f"**{d['overwrites']}** channel permission change(s), fixed roles of **{d['members']}** member(s), "
                   f"unbanned **{d['unbanned']}**, deleted **{d['removed']}** thing(s) they created.")
        if d["failed"]:
            summary += f"\n⚠️ {d['failed']} thing(s) failed (usually a role above mine)."
        await interaction.edit_original_response(content="✅ " + summary)
        await log_action(interaction.guild, "Actions Undone",
                         f"**By:** {interaction.user.mention}\n**Undid actions of:** "
                         + (f"<@{self.target_id}>" if self.target_id else "everyone (except bots, owner, trusted)")
                         + f"\n{summary}",
                         color=0xE74C3C, channel_key="ai_log_channel")

    @discord.ui.button(label="Undo everything", style=discord.ButtonStyle.danger)
    async def everything(self, interaction, btn):
        await self._run(interaction, remove_created=True)

    @discord.ui.button(label="Undo, but keep what they created", style=discord.ButtonStyle.primary)
    async def keep_created(self, interaction, btn):
        await self._run(interaction, remove_created=False)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, btn):
        await interaction.response.edit_message(content="Undo cancelled.", embed=None, view=None)
        self.stop()


@bot.tree.command(name="undo", description="Reverse what someone did to the server, using the Audit Log")
@app_commands.guild_only()
@app_commands.describe(
    user="@mention or user ID. Leave empty to undo EVERYONE's actions (except bots, the owner and trusted users)",
    hours="How far back to look (default: 24 hours when undoing everyone, 45 days for one person)")
async def undo_cmd(interaction: discord.Interaction, user: str = None,
                   hours: app_commands.Range[int, 1, 1080] = None):
    user_id = None
    if user:
        digits = "".join(ch for ch in user if ch.isdigit())
        if not (17 <= len(digits) <= 20):
            await interaction.response.send_message(
                "Give me an @mention or a user ID (Developer Mode on → long-press them → Copy User ID), "
                "or leave it empty to undo everyone's actions.", ephemeral=True)
            return
        user_id = int(digits)
    hours = hours or (1080 if user_id else 24)
    who = f"<@{user_id}>" if user_id else "everyone (except bots, the owner and trusted users)"
    when = f"the last {hours}h" if hours < 1080 else "the last 45 days"
    if not can_restore(interaction.user):
        await interaction.response.send_message("Only the server owner and trusted users can do that.", ephemeral=True)
        return
    if not interaction.guild.me.guild_permissions.view_audit_log:
        await interaction.response.send_message("I need the **View Audit Log** permission for this.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    plan = await build_undo_plan(interaction.guild, user_id, hours)
    if plan.empty():
        await interaction.followup.send(f"I found nothing to undo from {who} in {when}.", ephemeral=True)
        return

    def names(values, prefix, limit=12):
        values = [v for v in values if v]
        shown = ", ".join(f"{prefix}{v}" for v in values[:limit])
        return (shown + (f" *+{len(values) - limit} more*" if len(values) > limit else "")) or "*none*"

    role_names = [r["name"] for r in plan.deleted_roles.values()]
    chan_names = [c["name"] for c in plan.deleted_channels.values()]
    edited_roles = [getattr(interaction.guild.get_role(r), "name", None) or f"deleted role" for r in plan.role_edits]
    lines = [
        f"### Roles to recreate · {len(role_names)}\n{names(role_names, '@')}",
        f"### Channels to recreate · {len(chan_names)}\n{names(chan_names, '#')}",
        f"### Role edits to revert · {len(plan.role_edits)}\n{names(edited_roles, '@')}",
        f"### Other changes to revert\n"
        f"• **{len(plan.channel_edits)}** channel edit(s) · **{len(plan.overwrites)}** channel permission change(s)\n"
        f"• Roles of **{len(plan.member_roles)}** member(s) put back\n"
        f"• **{len(plan.banned)}** member(s) unbanned",
        f"### Things they created (deleted with *Undo everything*) · {len(plan.created_channels) + len(plan.created_roles)}\n"
        + names([f"#{n}" for n in plan.created_channels.values()] + [f"@{n}" for n in plan.created_roles.values()], ""),
    ]
    notes = []
    if plan.kicks:
        notes.append(f"They kicked {plan.kicks} member(s). Kicks can't be undone, those people need a new invite.")
    notes.append("Messages in deleted channels can't be brought back, and recreated roles start at the bottom "
                 "of the role list, so drag them back into place.")
    emb = discord.Embed(color=0xF1C40F, description=(
        f"# ↩️ Undo\n-# Actions by {who} in {when}\n{DIV}\n"
        + "### Whose actions\n" + ", ".join(f"<@{u}> ({n})" for u, n in
                                             sorted(plan.actors.items(), key=lambda kv: -kv[1])[:15]) + "\n" + "\n".join(lines) + f"\n{DIV}\n"
        + "\n".join(f"-# {n}" for n in notes)))
    add_footer_divider(emb)
    emb.set_footer(text="Confirm within 3 minutes")
    await interaction.followup.send(embed=emb, view=UndoConfirm(interaction.user.id, user_id, plan), ephemeral=True)
