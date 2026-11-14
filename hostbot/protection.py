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
            "**What they did:**\n" + "\n".join(f"• {d}" for d in details[-10:]))
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
