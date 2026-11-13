"""Everything AI: Groq/OpenRouter calls, chatbot queue + on_message, /clearchat, and the AI server-admin engine."""

import aiohttp
import asyncio
import base64
import colorsys
import datetime
import difflib
import discord
import json
import random
import re
import time
from discord.http import Route

from hostbot.core import (
    AI_COOLDOWN, ALLOWED_USER_IDS, BOT_OWNER_IDS, GIF_ADDON, GIF_CHANCE, GIF_TAG_RE, GIPHY_KEY,
    GROQ_MAX_IMAGE_CHARS, GROQ_VISION_MODELS, MAX_WORDS, OPENROUTER_FREE_MODELS,
    OPENROUTER_VISION_MODELS, PERSONA_BESTIE, PERSONA_ROAST, SOFT_WORDS, TYPING_DELAY,
    _bg_tasks, _queue_notices, ai_history, ai_queue, bot, db, get_config, groq_client, has_role_id,
    is_disabled_guild, is_trusted, is_whitelisted_guild, openrouter_client, save_db, user_cooldowns,
)
from hostbot.shared import (
    DIV, GuardedView, add_footer_divider, dm_owners, log_action, safe_reply, send_notice,
)
from hostbot import core

def pick_persona(member):
    """Best-friend mode for you, trusted people and server admins. Roast mode for everyone else."""
    try:
        return PERSONA_BESTIE if is_staff(member) else PERSONA_ROAST
    except Exception:
        return PERSONA_ROAST

# Only these servers are allowed to have Host Assistant installed.
# ==========================================
# AI SERVER ADMIN ENGINE
# ==========================================
# Flow: staff pings the bot -> the LLM turns the message into a JSON list of whitelisted
# actions -> code resolves targets, checks permissions/hierarchy, asks for confirmation on
# destructive stuff, executes, and posts ONE clean result card.

ADMIN_VERB_RE = re.compile(
    r"\b(create|make|add|delete|remove|rename|lock|unlock|hide|show|kick|ban|unban|timeout|mute|unmute|"
    r"give|assign|take|purge|clear|wipe|slowmode|nickname|nick|move|set|change|open|close|restrict|"
    r"announce|say|send|only|turn|toggle|enable|disable|switch|allow|grant|deny|revoke|let|stop|put|place|"
    r"update|edit|reset|display|mention|hoist|style|colou?r|swap|raise|lower)\b", re.IGNORECASE)
ADMIN_NOUN_RE = re.compile(
    r"\b(channel|category|role|roles|member|user|slowmode|nickname|messages|topic|permission|permissions|"
    r"access|accessible|vc|voice|display|separately|mentionable|hoist|gradient|holographic|style|colou?r|"
    r"position|hierarchy|above|below|images?|emojis?|reactions?|embeds?)\b", re.IGNORECASE)

# What each action needs from the PERSON asking (not just the bot).
PERM_FOR = {
    "create_channel": "manage_channels", "delete_channel": "manage_channels",
    "rename_channel": "manage_channels", "channel_access": "manage_channels",
    "lock_channel": "manage_channels", "unlock_channel": "manage_channels",
    "slowmode": "manage_channels", "set_topic": "manage_channels", "move_channel": "manage_channels",
    "create_role": "manage_roles", "delete_role": "manage_roles", "rename_role": "manage_roles",
    "role_color": "manage_roles", "give_role": "manage_roles", "remove_role": "manage_roles",
    "move_role": "manage_roles", "role_permissions": "manage_roles",
    "role_style": "manage_roles", "role_display": "manage_roles",
    "kick": "kick_members", "ban": "ban_members", "unban": "ban_members",
    "timeout": "moderate_members", "untimeout": "moderate_members",
    "nickname": "manage_nicknames", "purge": "manage_messages", "send_message": "send_messages",
}
ALWAYS_CONFIRM = {"delete_channel", "delete_role", "kick", "ban"}

BASE_COLORS = {
    "red": 0xE74C3C, "blue": 0x3498DB, "green": 0x2ECC71, "yellow": 0xF1C40F, "orange": 0xE67E22,
    "purple": 0x9B59B6, "pink": 0xFF69B4, "gold": 0xFFD700, "teal": 0x1ABC9C, "white": 0xFFFFFF,
    "black": 0x010101, "grey": 0x95A5A6, "gray": 0x95A5A6, "silver": 0xC0C0C0,
    "lime": 0x32CD32, "cyan": 0x00BCD4, "magenta": 0xFF00FF, "brown": 0x8B5A2B, "maroon": 0x800000,
    "navy": 0x001F7F, "olive": 0x808000, "turquoise": 0x40E0D0, "indigo": 0x4B0082, "violet": 0xEE82EE,
    "coral": 0xFF7F50, "salmon": 0xFA8072, "crimson": 0xDC143C, "mint": 0x3EB489, "lavender": 0xB57EDC,
    "beige": 0xF5F5DC, "peach": 0xFFB07C, "skyblue": 0x87CEEB, "emerald": 0x50C878, "forestgreen": 0x228B22,
    "bloodred": 0x8A0303, "burgundy": 0x800020, "rose": 0xFF007F, "amber": 0xFFBF00, "bronze": 0xCD7F32,
    "sand": 0xE6D5A8, "cream": 0xFFFDD0, "blurple": 0x5865F2,
}
DARK_WORDS = {"dark", "darker", "deep", "dim", "dull"}
LIGHT_WORDS = {"light", "lighter", "pale", "pastel", "soft"}
BRIGHT_WORDS = {"bright", "neon", "vivid"}
STRONG_WORDS = {"very", "extra", "super", "ultra", "really"}

ADMIN_PARSER_PROMPT = """You convert a Discord admin's message into JSON server actions.
Reply with ONLY one JSON object. No prose, no markdown, no code fences.
Format: {"actions":[{"action":"<name>", ...params}]}
If the message is NOT asking to change something on the server (chat, a question, a joke), reply {"actions":[]}.
Max 6 actions. Copy channel/role/user references EXACTLY as written (mentions like <#123>, <@&123>, <@123>, or plain names). Never invent targets.
If the message says "this channel" or gives no channel for a channel action, omit the "channel" field.

Actions and params:
create_channel: name, type ("text"|"voice"|"category", default "text"), roles (optional list: makes it private to those roles), category (optional)
delete_channel: channel
rename_channel: channel, name
channel_access: channel, mode ("restrict" = only the listed roles can see it | "public" = reset to everyone | "hide" = hide from listed roles | "show" = show to listed roles), roles (list)
lock_channel: channel (optional)       (stop everyone from sending messages)
unlock_channel: channel (optional)
slowmode: channel (optional), seconds (0-21600)
set_topic: channel (optional), topic
move_channel: channel, category
create_role: name, style (optional: "solid"|"gradient"|"holographic", default solid; use gradient when two colors are asked for), color (optional: hex or any color name, shades allowed like \"dark green\", \"light blue\", \"neon pink\"), color2 (optional, second color for gradient), hoist (optional bool), mentionable (optional bool)
delete_role: role
rename_role: role, name
role_color: role, color
role_style: role, style ("solid"|"gradient"|"holographic"), color (optional), color2 (needed for gradient)
role_display: role, hoist (optional bool: true = "display role members separately"), mentionable (optional bool: true = "allow anyone to @mention this role")
move_role: role, above (a role) OR below (a role)    (puts the role right above/below that role in the hierarchy)
role_permissions: role, allow (list), deny (list)    (use Discord permission names in snake_case, e.g. attach_files for images, embed_links, add_reactions, use_external_emojis, send_messages)
give_role: members (list), role
remove_role: members (list), role
kick: member, reason (optional)
ban: member, reason (optional)
unban: user (user ID)
timeout: member, minutes, reason (optional)
untimeout: member
nickname: member, nickname (omit or empty to reset)
purge: channel (optional), count (1-100), member (optional, only that member's messages)
send_message: channel (optional), text
"""

class AdminCtx:
    def __init__(self, msg):
        self.msg = msg
        self.guild = msg.guild
        self.author = msg.author
        self.reason = f"Requested by {msg.author} via Host Assistant"

def has_perm(member, perm):
    """Bot owners and real admins can do anything. Everyone else needs the matching
    Discord permission themselves (Moderate Members for timeouts, Manage Roles for roles...)."""
    if member is None:
        return False
    if member.id in BOT_OWNER_IDS:
        return True
    gp = getattr(member, "guild_permissions", None)
    return bool(gp and (gp.administrator or getattr(gp, perm, False)))

# A non-admin who asks for a mute without the permission gets muted instead.
MUTE_STRIKE_RESET_HOURS = 24          # strikes reset after this long without another attempt
MUTE_STRIKE_MAX_MINUTES = 40320       # Discord's 28-day timeout limit

async def punish_mute_attempt(ctx):
    """1 min the first time, then 2, 4, 8... doubling with every attempt."""
    g, m = ctx.guild, ctx.author
    strikes = db.setdefault("mute_strikes", {}).setdefault(str(g.id), {})
    rec = strikes.get(str(m.id)) or {"count": 0, "last": 0}
    now = time.time()
    if now - rec.get("last", 0) > MUTE_STRIKE_RESET_HOURS * 3600:
        rec["count"] = 0
    rec["count"] += 1
    rec["last"] = int(now)
    strikes[str(m.id)] = rec
    save_db(db)
    mins = min(MUTE_STRIKE_MAX_MINUTES, 2 ** (rec["count"] - 1))
    try:
        await m.timeout(datetime.timedelta(minutes=mins),
                        reason="Tried to mute someone without permission (via Host Assistant)")
    except discord.HTTPException:
        return "❌ You don't have permission to mute people. (I couldn't mute you back.)"
    return (f"🔇 You don't have permission to mute people, so I muted **you** for {mins} min. "
            "Try it again and it gets longer.")

def is_staff(member):
    """Server admins, trusted users, or the configured Staff role."""
    if member is None:
        return False
    if is_trusted(member):
        return True
    gp = getattr(member, "guild_permissions", None)
    if gp and gp.administrator:
        return True
    guild = getattr(member, "guild", None)
    if guild:
        staff_role = get_config(guild.id).get("staff_role")
        if staff_role and any(r.id == staff_role for r in getattr(member, "roles", [])):
            return True
    return False

def is_ai_user(member, guild_id):
    """AI chat is restricted to VIPs, configured Staff, server admins, and bot owners."""
    if member is None:
        return False
    if member.id in BOT_OWNER_IDS or member.id in ALLOWED_USER_IDS:
        return True
    if is_staff(member):
        return True
    vip_role = get_config(guild_id).get("superpower_role")
    return has_role_id(member, vip_role)

def can_view_ai_images(member, guild_id):
    """Vision is available to VIP, Staff, Double Booster, admins, and bot owners."""
    if member is None:
        return False
    if member.id in BOT_OWNER_IDS or member.id in ALLOWED_USER_IDS:
        return True
    perms = getattr(member, "guild_permissions", None)
    if perms and perms.administrator:
        return True
    if is_staff(member):
        return True
    vip_role = get_config(guild_id).get("superpower_role")
    double_booster_role = get_config(guild_id).get("double_booster_role")
    return has_role_id(member, vip_role) or has_role_id(member, double_booster_role)

def looks_like_admin_request(msg, txt):
    if not ADMIN_VERB_RE.search(txt):
        return False
    if ADMIN_NOUN_RE.search(txt):
        return True
    others = [m for m in msg.mentions if m != bot.user]
    return bool(msg.role_mentions or msg.channel_mentions or others)

# ---------- target resolution ----------
def as_list(v):
    if v is None or v == "":
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]

def _id_from(ref):
    m = re.search(r"\d{17,20}", str(ref))
    return int(m.group()) if m else None

def _norm(s):
    return re.sub(r"[\s_]+", "-", str(s).strip().lstrip("#@").lower())

def _to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default

def find_channel(guild, ref, kinds=None):
    if not ref:
        return None
    chans = [c for c in guild.channels if kinds is None or isinstance(c, kinds)]
    cid = _id_from(ref)
    if cid:
        for c in chans:
            if c.id == cid:
                return c
    name = _norm(ref)
    for c in chans:
        if _norm(c.name) == name:
            return c
    close = difflib.get_close_matches(name, [_norm(c.name) for c in chans], n=1, cutoff=0.85)
    if close:
        for c in chans:
            if _norm(c.name) == close[0]:
                return c
    return None

def find_role(guild, ref, roles=None):
    """Look a role up by mention/ID/name. Pass a freshly fetched `roles` list to avoid stale cache."""
    if not ref:
        return None
    pool = roles if roles is not None else guild.roles
    s = str(ref).strip()
    if s.lower().lstrip("@") in ("everyone", "here"):
        return guild.default_role
    cid = _id_from(s)
    if cid:
        for r in pool:
            if r.id == cid:
                return r
    name = s.lstrip("@").lower()
    for r in pool:
        if r.name.lower() == name:
            return r
    close = difflib.get_close_matches(name, [r.name.lower() for r in pool], n=1, cutoff=0.85)
    if close:
        for r in pool:
            if r.name.lower() == close[0]:
                return r
    return None

def find_member(guild, ref):
    if not ref:
        return None
    cid = _id_from(ref)
    if cid:
        m = guild.get_member(cid)
        if m:
            return m
    s = str(ref).strip().lstrip("@")
    m = guild.get_member_named(s)
    if m:
        return m
    low = s.lower()
    for m in guild.members:
        if m.display_name.lower() == low or m.name.lower() == low:
            return m
    close = difflib.get_close_matches(low, [m.display_name.lower() for m in guild.members], n=1, cutoff=0.85)
    if close:
        for m in guild.members:
            if m.display_name.lower() == close[0]:
                return m
    return None

def parse_color(v):
    """Hex (#rrggbb, #rgb), rgb(r,g,b), or a color name with optional shades:
    'dark green', 'very light blue', 'neon pink', 'sky blue'. Returns None if not understood."""
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip().lower().replace("-", " ").replace("_", " ")
    h = s.lstrip("#")
    if h.startswith("0x"):
        h = h[2:]
    if re.fullmatch(r"[0-9a-f]{3}", h):
        h = "".join(c * 2 for c in h)
    if re.fullmatch(r"[0-9a-f]{6}", h) and (s.startswith(("#", "0x")) or not h.isalpha()):
        return discord.Colour(int(h, 16) or 0x010101)
    m = re.fullmatch(r"rgb\(?\s*(\d{1,3})[\s,]+(\d{1,3})[\s,]+(\d{1,3})\s*\)?", s)
    if m:
        r, g, b = (min(255, int(x)) for x in m.groups())
        return discord.Colour(((r << 16) | (g << 8) | b) or 0x010101)

    words = s.split()
    strong = any(w in STRONG_WORDS for w in words)
    dark = any(w in DARK_WORDS for w in words)
    light = any(w in LIGHT_WORDS for w in words)
    bright = any(w in BRIGHT_WORDS for w in words)
    rest = [w for w in words if w not in DARK_WORDS | LIGHT_WORDS | BRIGHT_WORDS | STRONG_WORDS]
    base = BASE_COLORS.get("".join(rest)) or (BASE_COLORS.get(rest[-1]) if rest else None)
    if base is None:
        return None
    hh, ll, ss = colorsys.rgb_to_hls((base >> 16 & 255) / 255, (base >> 8 & 255) / 255, (base & 255) / 255)
    if dark:
        ll *= 0.25 if strong else 0.5
    if light:
        ll += (1 - ll) * (0.75 if strong else 0.5)
    if bright:
        ss, ll = 1.0, 0.5
    r, g, b = (round(x * 255) for x in colorsys.hls_to_rgb(hh, max(0.0, min(1.0, ll)), ss))
    return discord.Colour(((r << 16) | (g << 8) | b) or 0x010101)

def clean_channel_name(v):
    return str(v or "").strip().lstrip("#")[:100]

# ---------- hierarchy checks ----------
def check_role(ctx, role):
    g = ctx.guild
    if role is None:
        return False, "Couldn't find that role."
    if role.is_default() or role.managed:
        return False, f"**{role.name}** can't be edited."
    if role >= g.me.top_role:
        return False, f"**{role.name}** is above my role. Move my role higher."
    if not (ctx.author.id in BOT_OWNER_IDS or ctx.author.id == g.owner_id) and role >= ctx.author.top_role:
        return False, f"**{role.name}** is above your highest role."
    return True, ""

def check_member(ctx, m):
    g = ctx.guild
    if m is None:
        return False, "Couldn't find that member."
    if m.id == g.owner_id:
        return False, "Can't touch the server owner."
    if m.id == g.me.id:
        return False, "Not doing that to myself."
    if m.top_role >= g.me.top_role:
        return False, f"**{m.display_name}** outranks me."
    if not (ctx.author.id in BOT_OWNER_IDS or ctx.author.id == g.owner_id) and m.top_role >= ctx.author.top_role:
        return False, f"**{m.display_name}** outranks you."
    return True, ""

def pick_channel(ctx, a, kinds=None, key="channel"):
    ref = a.get(key)
    if ref:
        ch = find_channel(ctx.guild, ref, kinds)
    else:
        ch = ctx.msg.channel
        ch = ch if (kinds is None or isinstance(ch, kinds)) else None
    # Non-admins can't use me on channels they can't even see.
    if ch is not None and not _author_has_in(ctx, ch, "view_channel"):
        return None
    return ch

def _author_has_in(ctx, ch, perm):
    if ctx.author.id in BOT_OWNER_IDS:
        return True
    p = ch.permissions_for(ctx.author)
    return bool(p.administrator or getattr(p, perm, False))

TEXT_KINDS = (discord.TextChannel,)
VOICE_KINDS = (discord.VoiceChannel, discord.StageChannel)

# ---------- action handlers: each returns (ok, text) ----------
async def act_create_channel(ctx, a):
    name = clean_channel_name(a.get("name"))
    if not name:
        return False, "Missing channel name."
    ctype = str(a.get("type") or "text").lower()
    cat = find_channel(ctx.guild, a.get("category"), (discord.CategoryChannel,)) if a.get("category") else None
    cat_note = ""
    if a.get("category") and not cat and ctype != "category":
        cat_note = f" (couldn't find the category **{a.get('category')}**, so it's at the top level)"
    roles, missing = [], []
    for x in as_list(a.get("roles")):
        r = find_role(ctx.guild, x)
        if r:
            roles.append(r)
        else:
            missing.append(str(x))
    if missing:
        # Never fall back to a PUBLIC channel when the person asked for a private one.
        return False, f"Couldn't find the role {', '.join(missing)}, so I didn't create anything."
    overwrites = {}
    if roles:
        overwrites[ctx.guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        overwrites[ctx.guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True)
        for r in roles:
            if ctype == "voice":
                overwrites[r] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
            else:
                overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    kw = {"overwrites": overwrites, "reason": ctx.reason}
    if ctype == "category":
        ch = await ctx.guild.create_category(name, **kw)
    else:
        if cat:
            kw["category"] = cat
        if ctype == "voice":
            ch = await ctx.guild.create_voice_channel(name, **kw)
        else:
            ch = await ctx.guild.create_text_channel(name, **kw)
    who = f" for {', '.join(r.mention for r in roles)}" if roles else ""
    return True, f"Created {ctype} {ch.mention}{who}{cat_note}"

async def act_delete_channel(ctx, a):
    ch = pick_channel(ctx, a) if a.get("channel") else None
    if not ch:
        return False, "Couldn't find that channel."
    name = ch.name
    await ch.delete(reason=ctx.reason)
    return True, f"Deleted **#{name}**"

async def act_rename_channel(ctx, a):
    ch = pick_channel(ctx, a)  # "this channel" = the one the message was sent in
    new = clean_channel_name(a.get("name"))
    if not ch:
        return False, "Couldn't find that channel."
    if not new:
        return False, "Missing the new name."
    await ch.edit(name=new, reason=ctx.reason)
    return True, f"Renamed to {ch.mention}"

async def act_channel_access(ctx, a):
    ch = pick_channel(ctx, a)
    if not ch:
        return False, "Couldn't find that channel."
    mode = str(a.get("mode") or "restrict").lower()
    roles = [r for r in (find_role(ctx.guild, x) for x in as_list(a.get("roles"))) if r]
    if mode != "public" and not roles:
        return False, "Which role(s)? I couldn't find any."
    is_voice = isinstance(ch, VOICE_KINDS)
    grant = {"view_channel": True, "connect": True, "speak": True} if is_voice else \
            {"view_channel": True, "send_messages": True, "read_message_history": True}
    names = ", ".join(r.mention for r in roles)
    if mode == "restrict":
        # Keep the bot itself able to see the channel before locking everyone else out.
        try:
            await ch.set_permissions(ctx.guild.me, view_channel=True, send_messages=True, connect=True, reason=ctx.reason)
        except discord.HTTPException:
            pass
        await ch.set_permissions(ctx.guild.default_role, view_channel=False, reason=ctx.reason)
        for r in roles:
            await ch.set_permissions(r, reason=ctx.reason, **grant)
        return True, f"{ch.mention} is now only accessible to {names}"
    if mode == "public":
        await ch.set_permissions(ctx.guild.default_role, view_channel=None, reason=ctx.reason)
        return True, f"{ch.mention} is visible to everyone again"
    if mode == "hide":
        for r in roles:
            await ch.set_permissions(r, view_channel=False, reason=ctx.reason)
        return True, f"Hid {ch.mention} from {names}"
    if mode == "show":
        for r in roles:
            await ch.set_permissions(r, view_channel=True, reason=ctx.reason)
        return True, f"{ch.mention} is now visible to {names}"
    return False, f"Unknown access mode `{mode}`."

async def act_lock(ctx, a, lock=True):
    ch = pick_channel(ctx, a, TEXT_KINDS)
    if not ch:
        return False, "Couldn't find that text channel."
    await ch.set_permissions(ctx.guild.default_role, send_messages=False if lock else None, reason=ctx.reason)
    return True, f"{'Locked' if lock else 'Unlocked'} {ch.mention}"

async def act_lock_channel(ctx, a):
    return await act_lock(ctx, a, True)

async def act_unlock_channel(ctx, a):
    return await act_lock(ctx, a, False)

async def act_slowmode(ctx, a):
    ch = pick_channel(ctx, a, TEXT_KINDS)
    if not ch:
        return False, "Couldn't find that text channel."
    secs = max(0, min(21600, _to_int(a.get("seconds"))))
    await ch.edit(slowmode_delay=secs, reason=ctx.reason)
    return True, f"Slowmode in {ch.mention}: {secs}s" if secs else f"Slowmode off in {ch.mention}"

async def act_set_topic(ctx, a):
    ch = pick_channel(ctx, a, TEXT_KINDS)
    if not ch:
        return False, "Couldn't find that text channel."
    await ch.edit(topic=str(a.get("topic") or "")[:1024], reason=ctx.reason)
    return True, f"Updated topic of {ch.mention}"

async def act_move_channel(ctx, a):
    ch = pick_channel(ctx, a)
    cat = find_channel(ctx.guild, a.get("category"), (discord.CategoryChannel,))
    if not ch or isinstance(ch, discord.CategoryChannel):
        return False, "Couldn't find that channel."
    if not cat:
        return False, "Couldn't find that category."
    await ch.edit(category=cat, reason=ctx.reason)
    return True, f"Moved {ch.mention} into **{cat.name}**"

def _to_bool(v, default=None):
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "on", "1", "y")

HOLO_COLORS = (11127295, 16759788, 16761760)  # Discord's fixed holographic palette

async def _patch_role_colors(ctx, role, primary, secondary=None, tertiary=None):
    """Role gradient/holographic styles are set through the API's `colors` object."""
    route = Route("PATCH", "/guilds/{guild_id}/roles/{role_id}", guild_id=ctx.guild.id, role_id=role.id)
    payload = {"colors": {"primary_color": primary, "secondary_color": secondary, "tertiary_color": tertiary}}
    await bot.http.request(route, json=payload, reason=ctx.reason)

async def apply_role_style(ctx, role, style, c1, c2):
    """Returns (style_actually_applied, note). Falls back to solid if the server hasn't unlocked the style."""
    if style == "solid":
        base = c1 or role.colour
        try:
            await _patch_role_colors(ctx, role, base.value)
        except discord.HTTPException:
            await role.edit(colour=base, reason=ctx.reason)
        return "solid", ""
    colors = HOLO_COLORS if style == "holographic" else (c1.value, c2.value, None)
    try:
        await _patch_role_colors(ctx, role, *colors)
        return style, ""
    except discord.HTTPException as e:
        print(f"role style {style} failed:", e)
        fallback = c1 or discord.Colour(HOLO_COLORS[0])
        try:
            await role.edit(colour=fallback, reason=ctx.reason)
        except discord.HTTPException:
            pass
        return "solid", f" ({style} isn't unlocked here, the server probably needs more boosts, so I used a solid color)"

async def act_create_role(ctx, a):
    name = str(a.get("name") or "").strip()[:100]
    if not name:
        return False, "Missing role name."
    style = str(a.get("style") or "solid").lower()
    if style not in ("solid", "gradient", "holographic"):
        style = "solid"
    c1, c2 = parse_color(a.get("color")), parse_color(a.get("color2"))
    if style == "gradient" and not (c1 and c2):
        return False, "A gradient needs two colors."
    role = await ctx.guild.create_role(
        name=name, colour=c1 or discord.Colour.default(),
        hoist=bool(_to_bool(a.get("hoist"), False)), mentionable=bool(_to_bool(a.get("mentionable"), True)),
        reason=ctx.reason)
    note = ""
    if style != "solid":
        style, note = await apply_role_style(ctx, role, style, c1, c2)
    label = f" ({style})" if style != "solid" else ""
    return True, f"Created role {role.mention}{label}{note}"

async def act_role_style(ctx, a):
    role = find_role(ctx.guild, a.get("role"))
    ok, why = check_role(ctx, role)
    if not ok:
        return False, why
    style = str(a.get("style") or "").lower()
    if style not in ("solid", "gradient", "holographic"):
        return False, "Style must be solid, gradient or holographic."
    c1, c2 = parse_color(a.get("color")), parse_color(a.get("color2"))
    if style == "gradient" and not (c1 and c2):
        return False, "A gradient needs two colors."
    applied, note = await apply_role_style(ctx, role, style, c1, c2)
    return True, f"{role.mention} is now {applied}{note}"

async def act_role_display(ctx, a):
    role = find_role(ctx.guild, a.get("role"))
    ok, why = check_role(ctx, role)
    if not ok:
        return False, why
    hoist, ment = _to_bool(a.get("hoist")), _to_bool(a.get("mentionable"))
    if hoist is None and ment is None:
        return False, "Tell me what to change: display separately and/or mentionable."
    kw = {}
    bits = []
    if hoist is not None:
        kw["hoist"] = hoist
        bits.append(f"display separately {'on' if hoist else 'off'}")
    if ment is not None:
        kw["mentionable"] = ment
        bits.append(f"mentionable {'on' if ment else 'off'}")
    await role.edit(reason=ctx.reason, **kw)
    return True, f"{role.mention}: " + ", ".join(bits)

async def act_delete_role(ctx, a):
    role = find_role(ctx.guild, a.get("role"))
    ok, why = check_role(ctx, role)
    if not ok:
        return False, why
    name = role.name
    await role.delete(reason=ctx.reason)
    return True, f"Deleted role **{name}**"

async def act_rename_role(ctx, a):
    role = find_role(ctx.guild, a.get("role"))
    ok, why = check_role(ctx, role)
    if not ok:
        return False, why
    new = str(a.get("name") or "").strip()[:100]
    if not new:
        return False, "Missing the new name."
    await role.edit(name=new, reason=ctx.reason)
    return True, f"Renamed role to {role.mention}"

async def act_role_color(ctx, a):
    role = find_role(ctx.guild, a.get("role"))
    ok, why = check_role(ctx, role)
    if not ok:
        return False, why
    col = parse_color(a.get("color"))
    if col is None:
        return False, "I didn't understand that color."
    await role.edit(colour=col, reason=ctx.reason)
    return True, f"Recolored {role.mention}"

async def _roles_bulk(ctx, a, give):
    role = find_role(ctx.guild, a.get("role"))
    ok, why = check_role(ctx, role)
    if not ok:
        return False, why
    refs = as_list(a.get("members") or a.get("member"))
    if not refs:
        return False, "Who should get it?"
    done, failed = [], []
    for ref in refs[:25]:
        m = find_member(ctx.guild, ref)
        if not m:
            failed.append(str(ref))
            continue
        try:
            if give:
                await m.add_roles(role, reason=ctx.reason)
            else:
                await m.remove_roles(role, reason=ctx.reason)
            done.append(m.mention)
        except discord.HTTPException:
            failed.append(m.display_name)
    verb = "Gave" if give else "Removed"
    prep = "to" if give else "from"
    if not done:
        return False, f"Couldn't update anyone ({', '.join(failed)})."
    text = f"{verb} {role.mention} {prep} {', '.join(done)}"
    if failed:
        text += f" (skipped: {', '.join(failed)})"
    return True, text

async def act_give_role(ctx, a):
    return await _roles_bulk(ctx, a, True)

async def act_remove_role(ctx, a):
    return await _roles_bulk(ctx, a, False)

async def act_kick(ctx, a):
    m = find_member(ctx.guild, a.get("member"))
    ok, why = check_member(ctx, m)
    if not ok:
        return False, why
    await m.kick(reason=str(a.get("reason") or ctx.reason)[:400])
    return True, f"Kicked **{m.display_name}**"

async def act_ban(ctx, a):
    ref = a.get("member") or a.get("user")
    m = find_member(ctx.guild, ref)
    reason = str(a.get("reason") or ctx.reason)[:400]
    if m:
        ok, why = check_member(ctx, m)
        if not ok:
            return False, why
        await m.ban(reason=reason)
        return True, f"Banned **{m.display_name}**"
    uid = _id_from(ref)
    if uid:
        await ctx.guild.ban(discord.Object(id=uid), reason=reason)
        return True, f"Banned user ID `{uid}`"
    return False, "Couldn't find that member."

async def act_unban(ctx, a):
    uid = _id_from(a.get("user") or a.get("member"))
    if not uid:
        return False, "Give me the user ID to unban."
    await ctx.guild.unban(discord.Object(id=uid), reason=ctx.reason)
    return True, f"Unbanned `{uid}`"

async def act_timeout(ctx, a):
    m = find_member(ctx.guild, a.get("member"))
    ok, why = check_member(ctx, m)
    if not ok:
        return False, why
    mins = max(1, min(40320, _to_int(a.get("minutes"), 10)))
    await m.timeout(datetime.timedelta(minutes=mins), reason=str(a.get("reason") or ctx.reason)[:400])
    return True, f"Timed out **{m.display_name}** for {mins} min"

async def act_untimeout(ctx, a):
    m = find_member(ctx.guild, a.get("member"))
    ok, why = check_member(ctx, m)
    if not ok:
        return False, why
    await m.timeout(None, reason=ctx.reason)
    return True, f"Removed timeout from **{m.display_name}**"

async def act_nickname(ctx, a):
    m = find_member(ctx.guild, a.get("member"))
    ok, why = check_member(ctx, m)
    if not ok:
        return False, why
    nick = str(a.get("nickname") or "").strip()[:32] or None
    await m.edit(nick=nick, reason=ctx.reason)
    return True, f"Nickname of {m.mention}: **{nick}**" if nick else f"Reset nickname of {m.mention}"

async def act_purge(ctx, a):
    ch = pick_channel(ctx, a, TEXT_KINDS)
    if not ch:
        return False, "Couldn't find that text channel."
    if not _author_has_in(ctx, ch, "manage_messages"):
        return False, "You need **manage messages** in that channel."
    me = ch.permissions_for(ctx.guild.me)
    if not (me.manage_messages and me.read_message_history):
        return False, f"I need **Manage Messages** and **Read Message History** in {ch.mention}."
    n = max(1, min(100, _to_int(a.get("count"), 10)))
    target = None
    if a.get("member"):
        target = find_member(ctx.guild, a.get("member"))
        if not target:
            # Never fall back to deleting everyone's messages when the named member can't be found.
            return False, "Couldn't find that member, so I didn't delete anything."
    before = discord.Object(id=ctx.msg.id) if ch.id == ctx.msg.channel.id else None
    check = (lambda m: m.author.id == target.id) if target else (lambda m: True)
    try:
        deleted = await ch.purge(limit=n, before=before, check=check)
    except (discord.Forbidden, discord.HTTPException):
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        await dm_owners(f"⚠️ **Purge failed**\nServer: {ctx.guild.name}\nError: `{type(e).__name__}: {str(e)[:500]}`", key="purge_fail")
        return False, "Something broke on that one."
    return True, f"Deleted {len(deleted)} message(s) in {ch.mention}"

async def act_send_message(ctx, a):
    ch = pick_channel(ctx, a, TEXT_KINDS)
    text = str(a.get("text") or "").strip()
    if not ch:
        return False, "Couldn't find that text channel."
    if not text:
        return False, "Nothing to send."
    if not _author_has_in(ctx, ch, "send_messages"):
        return False, "You can't send messages in that channel yourself."
    await ch.send(text[:2000], allowed_mentions=discord.AllowedMentions.none())
    return True, f"Sent your message in {ch.mention}"

# ---------- role hierarchy + role permissions ----------
PERM_ALIASES = {
    "images": "attach_files", "image": "attach_files", "send_images": "attach_files", "files": "attach_files",
    "attach_images": "attach_files", "upload_files": "attach_files", "embeds": "embed_links", "links": "embed_links",
    "send_links": "embed_links", "reactions": "add_reactions", "emojis": "use_external_emojis",
    "external_emojis": "use_external_emojis", "stickers": "use_external_stickers",
    "external_stickers": "use_external_stickers", "send": "send_messages", "talk": "speak", "voice": "connect",
    "history": "read_message_history", "view": "view_channel", "threads": "send_messages_in_threads",
}
# Only the bot owner may GRANT these (and it always needs a Confirm click).
DANGEROUS_PERMS = {
    "administrator", "manage_guild", "manage_roles", "manage_channels", "ban_members", "kick_members",
    "manage_webhooks", "mention_everyone", "moderate_members", "manage_messages", "manage_nicknames",
    "view_audit_log", "manage_expressions", "manage_emojis_and_stickers", "manage_threads", "manage_events",
}

def norm_perm(name):
    n = re.sub(r"[\s\-]+", "_", str(name).strip().lower())
    n = PERM_ALIASES.get(n, n)
    return n if n in discord.Permissions.VALID_FLAGS else None

def norm_perms(v):
    out = []
    for x in as_list(v):
        n = norm_perm(x)
        if n and n not in out:
            out.append(n)
    return out

async def act_move_role(ctx, a):
    g = ctx.guild
    # Cached role positions go stale (e.g. right after a role is created), so always read fresh ones.
    fresh = sorted(await g.fetch_roles(), key=lambda r: r.position)
    role = find_role(g, a.get("role"), fresh)
    if role is None:
        return False, "Couldn't find that role."
    if role.is_default() or role.managed:
        return False, f"**{role.name}** can't be moved."
    above_ref, below_ref = a.get("above"), a.get("below")
    target = find_role(g, above_ref or below_ref, fresh)
    if not target or target.is_default():
        return False, "Couldn't find the role to place it next to."
    if target.id == role.id:
        return False, "That's the same role."

    me_ids = {r.id for r in g.me.roles}
    bot_top = max((r for r in fresh if r.id in me_ids), key=lambda r: r.position)
    if role.position >= bot_top.position:
        return False, f"**{role.name}** is above my role. Move my role higher first."
    privileged = ctx.author.id in BOT_OWNER_IDS or ctx.author.id == g.owner_id
    author_ids = {r.id for r in ctx.author.roles}
    author_top = max((r for r in fresh if r.id in author_ids), key=lambda r: r.position)
    if not privileged and role.position >= author_top.position:
        return False, f"**{role.name}** is above your highest role."

    word = "above" if above_ref else "below"
    if (above_ref and role.position > target.position) or (below_ref and role.position < target.position):
        return True, f"{role.mention} is already {word} {target.mention}"

    old_idx = next(i for i, r in enumerate(fresh) if r.id == role.id)
    order = [r for r in fresh if r.id != role.id]
    t_idx = next(i for i, r in enumerate(order) if r.id == target.id)
    order.insert(t_idx + 1 if above_ref else t_idx, role)
    new_idx = next(i for i, r in enumerate(order) if r.id == role.id)
    bot_idx = next(i for i, r in enumerate(order) if r.id == bot_top.id)
    if new_idx >= bot_idx:
        return False, "That spot is above my own role. Move my role higher first."
    if not privileged:
        auth_idx = next(i for i, r in enumerate(order) if r.id == author_top.id)
        if new_idx >= auth_idx:
            return False, "You can only place roles below your own highest role."

    lo, hi = sorted((old_idx, new_idx))
    changes = [{"id": order[i].id, "position": i} for i in range(lo, hi + 1) if order[i].position != i]
    if changes:
        route = Route("PATCH", "/guilds/{guild_id}/roles", guild_id=g.id)
        await bot.http.request(route, json=changes, reason=ctx.reason)

    # Don't trust the request: read the order back and confirm it really changed.
    after = {r.id: r.position for r in await g.fetch_roles()}
    ok = after.get(role.id, -1) > after.get(target.id, 0) if above_ref else after.get(role.id, 99999) < after.get(target.id, 0)
    if not ok:
        return False, "Discord didn't apply that move. Make sure my role is above both roles."
    return True, f"Moved {role.mention} {word} {target.mention}"

async def act_role_permissions(ctx, a):
    g = ctx.guild
    role = find_role(g, a.get("role"))
    ok, why = check_role(ctx, role)
    if not ok:
        return False, why
    allow_raw, deny_raw = as_list(a.get("allow")), as_list(a.get("deny"))
    allow, deny = norm_perms(allow_raw), norm_perms(deny_raw)
    unknown = [str(x) for x in allow_raw + deny_raw if not norm_perm(x)]
    if not allow and not deny:
        return False, "I didn't recognise any of those permissions."
    if set(allow) & set(deny):
        return False, "Same permission in both allow and deny."
    is_owner = ctx.author.id in BOT_OWNER_IDS
    bad = [p for p in allow if p in DANGEROUS_PERMS]
    if bad and not is_owner:
        return False, "Only the bot owner can grant: " + ", ".join(p.replace("_", " ") for p in bad)
    mep = g.me.guild_permissions
    for p in allow:
        if not (mep.administrator or getattr(mep, p, False)):
            return False, f"I don't have **{p.replace('_', ' ')}** myself, so I can't grant it."
        if not (is_owner or ctx.author.id == g.owner_id):
            ap = ctx.author.guild_permissions
            if not (ap.administrator or getattr(ap, p, False)):
                return False, f"You don't have **{p.replace('_', ' ')}** yourself."
    perms = discord.Permissions(role.permissions.value)
    perms.update(**{**{p: True for p in allow}, **{p: False for p in deny}})
    await role.edit(permissions=perms, reason=ctx.reason)
    bits = []
    if allow:
        bits.append("allowed " + ", ".join(p.replace("_", " ") for p in allow))
    if deny:
        bits.append("denied " + ", ".join(p.replace("_", " ") for p in deny))
    text = f"Updated {role.mention}: " + "; ".join(bits)
    if unknown:
        text += f" (ignored: {', '.join(unknown)})"
    return True, text

ADMIN_ACTIONS = {
    "create_channel": act_create_channel, "delete_channel": act_delete_channel,
    "rename_channel": act_rename_channel, "channel_access": act_channel_access,
    "lock_channel": act_lock_channel, "unlock_channel": act_unlock_channel,
    "slowmode": act_slowmode, "set_topic": act_set_topic, "move_channel": act_move_channel,
    "create_role": act_create_role, "delete_role": act_delete_role, "rename_role": act_rename_role,
    "role_color": act_role_color, "give_role": act_give_role, "remove_role": act_remove_role,
    "move_role": act_move_role, "role_permissions": act_role_permissions,
    "role_style": act_role_style, "role_display": act_role_display,
    "kick": act_kick, "ban": act_ban, "unban": act_unban,
    "timeout": act_timeout, "untimeout": act_untimeout, "nickname": act_nickname,
    "purge": act_purge, "send_message": act_send_message,
}

# ---------- planning / confirmation / execution ----------
def needs_confirm(a):
    act = a.get("action")
    if act == "role_permissions" and any(p in DANGEROUS_PERMS for p in norm_perms(a.get("allow"))):
        return True
    return act in ALWAYS_CONFIRM or (act == "purge" and _to_int(a.get("count"), 10) > 20)

def summarize(a):
    label = str(a.get("action", "?")).replace("_", " ").capitalize()
    bits = []
    for k, v in a.items():
        if k == "action" or v in (None, "", []):
            continue
        bits.append(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}")
    detail = ", ".join(bits)[:150]
    return f"**{label}**" + (f" ({detail})" if detail else "")

class ConfirmView(GuardedView):
    def __init__(self, author_id):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.value = None

    async def _gate(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Not your request.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, btn):
        if not await self._gate(interaction):
            return
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, btn):
        if not await self._gate(interaction):
            return
        self.value = False
        await interaction.response.defer()
        self.stop()

async def parse_admin_request(txt, image_urls=None):
    """Returns list of actions ([] = not an admin request) or None if every AI provider failed."""
    image_urls = image_urls or []
    content = [{"type": "text", "text": txt or "Please inspect the attached image and determine the requested admin action."}]
    content.extend({"type": "image_url", "image_url": {"url": url}} for url in image_urls[:4])
    user_message = {"role": "user", "content": content if image_urls else (txt or "")}
    messages = [{"role": "system", "content": ADMIN_PARSER_PROMPT}, user_message]
    raw, _, _ = await llm_complete(messages, groq_tokens=1500, or_tokens=800, temperature=0)
    if not raw:
        return None
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e <= s:
        return []
    try:
        data = json.loads(raw[s:e + 1])
    except ValueError:
        return []
    acts = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(acts, list):
        return []
    return [a for a in acts if isinstance(a, dict) and isinstance(a.get("action"), str)][:6]

async def run_admin_flow(msg, txt):
    uid = msg.author.id
    image_urls = [a.url for a in msg.attachments if (a.content_type or "").startswith("image/")][:4]
    if not can_view_ai_images(msg.author, msg.guild.id if msg.guild else 0):
        image_urls = []
    try:
        async with msg.channel.typing():
            plan = await parse_admin_request(txt, image_urls=image_urls)

        if plan is None:
            await safe_reply(msg, "⚠️ Couldn't reach my brain right now, try again in a sec.", delete_after=15)
            return

        if not plan:
            # Not actually an admin request: treat it as normal chat.
            user_cooldowns[uid] = time.time()
            await ai_queue.put((msg, txt, uid))
            return

        ctx = AdminCtx(msg)
        lines = []
        runnable = []
        for a in plan:
            name = a["action"].strip().lower()
            if name not in ADMIN_ACTIONS:
                lines.append(f"❌ I can't do `{name}`.")
                continue
            perm = PERM_FOR.get(name)
            if perm and not has_perm(msg.author, perm):
                if name == "timeout":
                    lines.append(await punish_mute_attempt(ctx))
                else:
                    lines.append(f"❌ You need the **{perm.replace('_', ' ')}** permission for that.")
                continue
            runnable.append((name, a))

        prompt = None
        if any(needs_confirm(a) for _, a in runnable):
            emb = discord.Embed(description="## ⚠️ Confirm these actions\n" + DIV + "\n" +
                                "\n".join(f"• {summarize(a)}" for _, a in runnable), color=0xFEE75C)
            add_footer_divider(emb)
            emb.set_footer(text="Confirm within 30 seconds")
            view = ConfirmView(uid)
            prompt = await safe_reply(msg, embed=emb, view=view)
            if prompt is None:
                return
            await view.wait()
            if not view.value:
                emb.description = emb.description.replace(
                    "## ⚠️ Confirm these actions", "## ❌ Cancelled" if view.value is False else "## ⌛ Timed out")
                emb.color = 0x4E5058
                add_footer_divider(emb)
                emb.set_footer(text="Nothing was changed")
                try:
                    await prompt.edit(embed=emb, view=None)
                except discord.HTTPException:
                    pass
                return
            emb.description = emb.description.replace("## ⚠️ Confirm these actions", "## ⏳ Working on it")
            emb.set_footer(text="Running...")
            try:
                await prompt.edit(embed=emb, view=None)
            except discord.HTTPException:
                pass

        ok_count = 0
        for name, a in runnable:
            handler = ADMIN_ACTIONS[name]
            try:
                ok, text = await handler(ctx, a)
            except discord.Forbidden:
                ok, text = False, "I'm missing permissions for that (check my role position)."
            except discord.HTTPException as e:
                print(f"admin action {name} failed:", e)
                ok, text = False, "Discord rejected that request."
            except Exception as e:
                import traceback; traceback.print_exc()
                print(f"admin action {name} crashed:", repr(e))
                ok, text = False, "Something broke on that one."
                await dm_owners(f"⚠️ **AI admin action `{name}` crashed**\nServer: {msg.guild.name}\nError: `{type(e).__name__}: {str(e)[:500]}`", key="admin_action_crash")
            lines.append(f"{'✅' if ok else '❌'} {text}")
            ok_count += 1 if ok else 0

        if ok_count == len(plan):
            title, color = "✅ Done", 0x57F287
        elif ok_count == 0:
            title, color = "❌ Couldn't do that", 0xED4245
        else:
            title, color = "⚠️ Partly done", 0xF0B232
        res = discord.Embed(description=f"## {title}\n{DIV}\n" + "\n".join(lines)[:3800], color=color)
        add_footer_divider(res)
        res.set_footer(text=f"Requested by {msg.author.display_name}")
        if prompt is not None:
            # Update the confirmation message in place instead of posting a new one.
            try:
                await prompt.edit(embed=res, view=None)
            except discord.HTTPException:
                await safe_reply(msg, embed=res)
        else:
            await safe_reply(msg, embed=res)

        if ok_count or any(l.startswith("🔇") for l in lines):
            await log_action(msg.guild, "AI Admin Action",
                             f"<@{uid}> asked:\n> {txt[:300]}\n\n" + "\n".join(lines)[:1500],
                             color=0xED4245, channel_key="ai_log_channel")
    except Exception as e:
        print("run_admin_flow error:", e)
        await dm_owners(f"⚠️ **Admin flow exception:** `{e}`", key="admin_exc")
        await safe_reply(msg, "⚠️ Something went wrong, nothing else was changed.")

# ==========================================
# LLM HELPER + BACKGROUND QUEUE WORKER (GROQ -> OPENROUTER)
# ==========================================
def limit_words(t, n):
    """Hard word cap. Cuts at the last full sentence if there is one, else just trims."""
    words = t.split()
    if len(words) <= n:
        return t
    cut = " ".join(words[:n])
    ends = [cut.rfind(c) for c in ".!?"]
    last = max(ends)
    if last >= len(cut) // 2:
        return cut[:last + 1]
    return cut.rstrip(",;:- ")

def dedupe_sentences(t):
    """Models sometimes loop and repeat a sentence. Fix glued punctuation, then drop repeats."""
    t = re.sub(r"([?!])(?=[A-Za-z])", r"\1 ", t)
    parts = re.split(r"(?<=[.!?])\s+", t)
    seen, out = set(), []
    for p in parts:
        key = re.sub(r"[^a-z0-9]+", " ", p.lower()).strip()
        if key and key in seen:
            continue
        seen.add(key)
        out.append(p)
    return " ".join(out)

def clean_ai_text(t):
    """Tidy model output: no stray headings, trailing spaces or big blank gaps."""
    t = t.replace("\r", "")
    t = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u200d\ufe0f]", "", t)  # no emojis
    t = re.sub(r"\s*[\u2014\u2013]\s*", ", ", t)  # em/en dashes read as AI
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"(\*{1,3})(\S(?:[^\n]*?\S)?)\1", r"\2", t)  # **bold** / *italic* -> plain
    t = re.sub(r"^\s*[-*\u2022]\s+", "", t, flags=re.MULTILINE)  # list bullets
    t = t.replace("`", "")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

def messages_have_images(messages):
    """Return True when any chat message contains an OpenAI-compatible image_url part."""
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                return True
    return False

async def attachment_to_data_url(attachment):
    """Download a Discord image attachment and convert it to a base64 data URL.

    Some OpenRouter providers expose vision endpoints that accept image input but
    reject remote image URLs. Data URLs avoid that provider-specific mismatch.
    """
    try:
        content_type = (attachment.content_type or "").split(";")[0].lower()
        if content_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            return None
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(attachment.url, allow_redirects=True) as r:
                if r.status != 200:
                    return None
                data = await r.read()
        # Keep vision requests reasonably sized. Discord images larger than this
        # are skipped rather than creating an oversized API request.
        if len(data) > 8 * 1024 * 1024:
            return None
        return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"
    except Exception as e:
        print("attachment_to_data_url failed:", e)
        return None

async def llm_complete(messages, groq_tokens=1024, or_tokens=400, temperature=None):
    """Run normal text through Groq first; route image conversations to OpenRouter vision.
    Returns (text_or_None, groq_err, openrouter_err)."""
    extra = {} if temperature is None else {"temperature": temperature}
    ans, groq_err, openrouter_err = None, "", ""
    has_images = messages_have_images(messages)

    def _strip_think(text):
        # Reasoning models can put their thinking in the reply; users should never see it.
        return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()

    # Images: try Groq's vision models first. Every model gets the real image, never a text-only copy.
    if has_images and groq_client:
        img_chars = sum(len(p.get("image_url", {}).get("url", ""))
                        for m in messages if isinstance(m.get("content"), list)
                        for p in m["content"] if p.get("type") == "image_url")
        if img_chars <= GROQ_MAX_IMAGE_CHARS:
            for groq_model in GROQ_VISION_MODELS:
                try:
                    res = await groq_client.chat.completions.create(
                        model=groq_model, messages=messages, max_tokens=groq_tokens, **extra)
                    out = _strip_think(res.choices[0].message.content) if res.choices else ""
                    if out:
                        return out, groq_err, openrouter_err
                except Exception as e:
                    groq_err = f"{groq_model}: {e}"
                    print(f"Groq vision ({groq_model}) error:", e)
        else:
            groq_err = "image too large for Groq (>4MB), used OpenRouter"

    # Text-only conversations go to Groq's text models.
    if not has_images:
        if groq_client:
            for groq_model in ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]:
                try:
                    res = await groq_client.chat.completions.create(
                        model=groq_model, messages=messages, max_tokens=groq_tokens, **extra)
                    if res.choices and (res.choices[0].message.content or "").strip():
                        return _strip_think(res.choices[0].message.content), groq_err, openrouter_err
                except Exception as e:
                    groq_err = str(e)
                    print(f"Groq ({groq_model}) error:", e)
        else:
            groq_err = "GROQ_API_KEY not set"

    if openrouter_client:
        models = OPENROUTER_VISION_MODELS if has_images else OPENROUTER_FREE_MODELS
        for open_model in models:
            try:
                res = await openrouter_client.chat.completions.create(
                    model=open_model, messages=messages, max_tokens=or_tokens,
                    extra_headers={"HTTP-Referer": "https://github.com", "X-Title": "Host Assistant"},
                    **extra)
                if res.choices and (res.choices[0].message.content or "").strip():
                    return _strip_think(res.choices[0].message.content), groq_err, openrouter_err
            except Exception as e:
                openrouter_err = str(e)
                print(f"OpenRouter ({open_model}) error:", e)
    else:
        openrouter_err = "OPENROUTER_API_KEY not set"

    return ans, groq_err, openrouter_err

async def fetch_gif(query):
    """A real GIF from Giphy (never a model-invented link). Returns a URL or None."""
    if not GIPHY_KEY or not query:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get("https://api.giphy.com/v1/gifs/search", params={
                    "api_key": GIPHY_KEY, "q": query[:40], "limit": 8, "rating": "pg-13", "lang": "en"}) as r:
                if r.status != 200:
                    return None
                data = await r.json()
        items = data.get("data") or []
        if not items:
            return None
        imgs = random.choice(items).get("images", {})
        return (imgs.get("downsized") or imgs.get("original") or {}).get("url")
    except Exception as e:
        print("fetch_gif failed:", e)
        return None

async def process_ai_queue():
    while True:
        msg, txt, uid = await ai_queue.get()
        waiting = _queue_notices.pop(msg.id, None)
        if waiting:
            try:
                await waiting.delete()
            except discord.HTTPException:
                pass
        try:
            # Artificial typing delay so replies feel human
            async with msg.channel.typing():
                await asyncio.sleep(TYPING_DELAY)

                if uid not in ai_history:
                    ai_history[uid] = []

                image_attachments = [a for a in msg.attachments if (a.content_type or "").startswith("image/")][:4]
                if not can_view_ai_images(msg.author, msg.guild.id if msg.guild else 0):
                    image_attachments = []

                # Send image bytes as data URLs instead of Discord CDN URLs.
                # This keeps vision working even when an OpenRouter provider
                # supports images but does not expose an image-URL endpoint.
                image_data_urls = []
                for attachment in image_attachments:
                    data_url = await attachment_to_data_url(attachment)
                    if data_url:
                        image_data_urls.append(data_url)

                if image_data_urls:
                    user_content = [{"type": "text", "text": txt or "Please inspect the attached image."}]
                    user_content.extend({"type": "image_url", "image_url": {"url": url}} for url in image_data_urls)
                else:
                    user_content = txt

                hist_entry = {"role": "user", "content": user_content}
                ai_history[uid].append(hist_entry)
                ai_history[uid] = ai_history[uid][-6:]

                want_gif = bool(GIPHY_KEY) and random.random() < GIF_CHANCE
                persona = pick_persona(msg.author) + (GIF_ADDON if want_gif else "")
                messages = [{"role": "system", "content": persona}] + ai_history[uid]
                ans, groq_err, openrouter_err = await llm_complete(messages, temperature=0.8)

                # Keep only a text note of any image so later turns don't get routed to the vision model.
                if image_data_urls:
                    hist_entry["content"] = f"[sent an image] {txt}"
                hist = ai_history.get(uid) or []  # /clearchat may have wiped it while we waited

                # Free vision endpoints come and go. If every one failed, still answer the text part.
                if not ans and image_data_urls:
                    blind = [{"role": "system", "content": persona}] + [
                        m for m in ai_history.get(uid, []) if m is not hist_entry]
                    blind.append({"role": "user", "content": f"{txt}\n(they attached an image but you can't see it "
                                                              "right now. say so in a few words, in your usual tone)"})
                    ans, groq_err, openrouter_err = await llm_complete(blind, temperature=0.8)

                if not ans:
                    if hist and hist[-1] is hist_entry:
                        hist.pop()  # don't leave an unanswered message behind
                    # Send detailed error logs directly to your Owner DM
                    await dm_owners(
                        f"⚠️ **AI Fallback Failed**\n• Groq Error: `{groq_err or 'None'}`\n"
                        f"• OpenRouter Error: `{openrouter_err or 'None'}`\n• User Message: `{txt[:300]}`",
                        key="ai_fail")
                    # Send clean generic message in public channel
                    await safe_reply(msg, "⚠️ My brain laggin right now, try again in a sec bro.", delete_after=15)
                    continue

                tag = GIF_TAG_RE.search(ans)
                gif_query = tag.group(1).strip() if (tag and want_gif) else None
                ans = GIF_TAG_RE.sub("", ans)
                ans = re.sub(r"https?://\S+", "", ans)  # the model never posts raw links
                ans = limit_words(dedupe_sentences(clean_ai_text(ans)), MAX_WORDS)
                if len(ans.split()) > SOFT_WORDS:
                    gif_query = None  # long replies never come with a GIF
                if any(h is hist_entry for h in ai_history.get(uid, ())):
                    ai_history[uid].append({"role": "assistant", "content": ans or "lol"})

                if len(ans) > 2000:
                    ans = ans[:1996] + "..."

                # Replies past the normal length "take a bit longer to type" (about +0.04s per extra word).
                await asyncio.sleep(max(0, len(ans.split()) - SOFT_WORDS) * 0.04)

                gif_url = await fetch_gif(gif_query) if gif_query else None
                if gif_url:
                    emb = discord.Embed(color=0x2B2D31)  # same shade as the dark theme, so no visible bar
                    emb.set_image(url=gif_url)
                    await safe_reply(msg, ans or None, embed=emb)
                else:
                    await safe_reply(msg, ans or "lol")
        except Exception as e:
            print("AI Chat Error:", e)
            await dm_owners(f"⚠️ **Critical AI Exception:** `{e}`", key="ai_crash")
            await safe_reply(msg, "⚠️ Something went wrong, my bad bro.", delete_after=15)
        finally:
            ai_queue.task_done()
            await asyncio.sleep(2)

# ==========================================
# ON MESSAGE EVENT (AI CHATBOT & GUILD CHECKS)
# ==========================================

async def handle_ai_trigger(msg):

    # AI Chat Trigger (Ping or Reply)
    pinged = bot.user in msg.mentions
    ref = msg.reference.resolved if msg.reference else None
    is_reply = isinstance(ref, discord.Message) and ref.author == bot.user

    if not (pinged or is_reply):
        return

    txt = re.sub(rf"<@!?{bot.user.id}>", "", msg.content).strip()
    has_image = any((a.content_type or "").startswith("image/") for a in msg.attachments)

    # Host Assistant AI is intentionally gated. The bot owners/admins always pass;
    # normal AI access is VIP or Staff. Image vision is a separate Double Booster perk.
    # In DMs there is no server role to check, so only bot owners and allowed users may chat there.
    if not msg.guild and msg.author.id not in BOT_OWNER_IDS and msg.author.id not in ALLOWED_USER_IDS:
        return
    if msg.guild and not is_ai_user(msg.author, msg.guild.id):
        # Replying to one of our own notices must not trigger another one (no notice chains).
        if is_reply and ref.embeds and (ref.embeds[0].title or "").startswith("🔒"):
            return
        await send_notice(msg, "🔒 Members only", "AI chat is for **VIP** and **Staff** roles.", key="lock")
        return

    image_allowed = bool(msg.guild and can_view_ai_images(msg.author, msg.guild.id))
    if has_image and not image_allowed:
        # The image is never forwarded to an AI provider. Text can still be answered.
        has_image = False
        if not txt:
            await send_notice(msg, "🖼️ Double Booster perk",
                              "Image analysis is for Double Boosters. Send some text and I'll still chat.", key="img")
            return

    # Mute Command Check (Owner Only)
    if txt.lower() == "shut up":
        if msg.author.id in BOT_OWNER_IDS:
            core.bot_muted = True
        return

    if core.bot_muted:
        if txt.lower() == "you can talk again" and msg.author.id in BOT_OWNER_IDS:
            core.bot_muted = False
            await safe_reply(msg, "back, did you miss me?")
        return

    if not txt and not has_image:
        await safe_reply(msg, "tf you want bro?", delete_after=10)
        return
    if not txt and has_image:
        txt = "Please look at the attached image and tell me what you see."

    uid = msg.author.id

    now = time.time()

    # Individual per-user cooldown check (AI_COOLDOWN)
    last_msg_time = user_cooldowns.get(uid, 0)
    if now - last_msg_time < AI_COOLDOWN:
        wait = int(AI_COOLDOWN - (now - last_msg_time)) + 1
        try:
            await msg.delete()  # needs Manage Messages; silently skipped if the bot lacks it
        except discord.HTTPException:
            pass
        try:
            await msg.channel.send(f"{msg.author.mention} ayo wait {wait} more seconds", delete_after=5,
                                   allowed_mentions=discord.AllowedMentions(users=[msg.author]))
        except discord.HTTPException:
            pass
        return

    user_cooldowns[uid] = now

    # Everyone who passed the AI-access check above can ask for admin actions (cooldown applies too), but each action
    # needs the matching Discord permission (admins and bot owners can do everything).
    if msg.guild and looks_like_admin_request(msg, txt):
        task = asyncio.create_task(run_admin_flow(msg, txt))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        return

    q_size = ai_queue.qsize()
    await ai_queue.put((msg, txt, uid))

    if q_size > 0:
        waiting = await safe_reply(msg, f"⏳ You're #{q_size + 1} in line, one sec...", delete_after=30)
        if waiting:
            _queue_notices[msg.id] = waiting

@bot.event
async def on_message(msg):
    if core.RATE_LIMIT_TRIGGERED:
        return
    if msg.author.bot:
        return

    if msg.guild and not is_whitelisted_guild(msg.guild.id):
        return

    if msg.guild and is_disabled_guild(msg.guild.id):
        return

    try:
        await handle_ai_trigger(msg)
    except Exception as e:
        print("on_message AI error:", e)

    # Always reached now, so prefix commands (!sync) work regardless of AI state.
    await bot.process_commands(msg)

@bot.tree.command(name="clearchat", description="Clears your AI chat history")
async def clearchat_cmd(interaction: discord.Interaction):
    if interaction.guild_id and not is_ai_user(interaction.user, interaction.guild_id):
        await interaction.response.send_message("🔒 AI access is limited to the VIP and Staff roles.", ephemeral=True)
        return
    uid = interaction.user.id
    if uid in ai_history:
        del ai_history[uid]
        await interaction.response.send_message("AI memory cleared.", ephemeral=True)
    else:
        await interaction.response.send_message("No memory to clear.", ephemeral=True)

@bot.tree.command(name="clearchat_all", description="Owner only: wipes AI chat memory for everyone")
async def clearchat_all_cmd(interaction: discord.Interaction):
    if interaction.user.id not in BOT_OWNER_IDS:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    n = len(ai_history)
    ai_history.clear()
    await interaction.response.send_message(f"Cleared AI memory for {n} user(s).", ephemeral=True)
