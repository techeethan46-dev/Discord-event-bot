"""Shared helpers used everywhere: time parsing, event lookup, permissions, notices, logging, event banner embed, per-event log + waitlist."""

import asyncio
import discord
import re
import time

from hostbot.core import (
    BOT_OWNER_IDS, SAFE_MENTIONS, _RATE_LIMIT_TASKS, _bg_tasks, bot, db,
    disabled_guild_response, get_config, is_trusted, save_db,
)
from hostbot import core

def parse_time(t_str):
    """'1h30m', '45m', '90s', '2d' or a bare number (minutes) -> seconds.
    Returns None when the text can't be understood, so callers can say so instead of guessing."""
    s = str(t_str or "").lower().replace(" ", "")
    if s in ("", "now", "0"):
        return 0
    if not re.fullmatch(r"(?:\d+[dhms]?)+", s):
        return None
    mult = {"d": 86400, "h": 3600, "m": 60, "s": 1, "": 60}
    return sum(int(n) * mult[u] for n, u in re.findall(r"(\d+)([dhms]?)", s))

def find_event(msg_id):
    for key, info in db["events"].items():
        if msg_id in info.get("message_ids", []):
            return key, info
    return None, None

def find_active_event(name, guild_id):
    """Find a running (not concluded) event in this server by its name. Returns (key, info) or (None, None)."""
    low = str(name or "").lower().strip()
    info = db["events"].get(low)
    if info and not info.get("concluded") and event_in_guild(info, guild_id):
        return low, info
    for k, v in db["events"].items():
        if (not v.get("concluded") and event_in_guild(v, guild_id)
                and str(v.get("display_name", "")).lower().strip() == low):
            return k, v
    return None, None

def new_event_key(name):
    """A key that is never already taken, so old/concluded events (and their ratings) are never overwritten."""
    base = name.lower().strip()
    if base not in db["events"]:
        return base
    n = int(time.time())
    while f"{base}~{n}" in db["events"]:
        n += 1
    return f"{base}~{n}"

def event_in_guild(info, guild_id):
    # Old events have no guild_id saved, so they stay visible everywhere (as before).
    return info.get("guild_id") in (None, guild_id)

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

def is_server_admin(member):
    """Server admins, plus the bot owners in ANY server (so you can run /setup etc. everywhere)."""
    return member.id in BOT_OWNER_IDS or member.guild_permissions.administrator

def has_host_role(member, guild_id):
    """Return True only when the member has this server's configured Host role."""
    host_role_id = get_config(guild_id).get("host_role")
    return bool(host_role_id) and any(r.id == host_role_id for r in getattr(member, "roles", []))

def is_vip(member, guild_id):
    """Booster, VIP role, or admin: may hold Superpower nations."""
    if member is None:
        return False
    if member.premium_since or member.guild_permissions.administrator:
        return True
    sp_role = get_config(guild_id).get("superpower_role")
    return bool(sp_role) and any(r.id == sp_role for r in member.roles)


async def emergency_rate_limit_shutdown(source="Discord API"):
    """Trip once when Discord returns a 429 and stop our own activity immediately."""
    if core.RATE_LIMIT_TRIGGERED:
        return
    core.RATE_LIMIT_TRIGGERED = True
    print(f"🚨 DISCORD 429 DETECTED ({source}) — Host Assistant entering safe hold mode.")
    current = asyncio.current_task()
    for task in list(_bg_tasks) + list(_RATE_LIMIT_TASKS):
        if task is not None and task is not current and not task.done():
            task.cancel()
    if core.ai_worker_task is not None and core.ai_worker_task is not current and not core.ai_worker_task.done():
        core.ai_worker_task.cancel()
    try:
        await bot.close()
    except Exception as e:
        print("Rate-limit shutdown close error:", e)

_notice_last = {}

async def send_notice(msg, title, text, ttl=10, key="notice", every=60):
    """A small branded notice that deletes itself. (Plain message replies can't be ephemeral;
    only slash commands and buttons can.) Each person sees the same notice at most once per `every` seconds."""
    now = time.time()
    k = (msg.author.id, key)
    if now - _notice_last.get(k, 0) < every:
        return None
    if len(_notice_last) > 500:
        _notice_last.clear()
    _notice_last[k] = now
    emb = discord.Embed(description=f"### {title}\n{text}", color=0x2B2D31)
    emb.set_footer(text=BRAND)
    return await safe_reply(msg, embed=emb, delete_after=ttl)

async def safe_reply(msg, content=None, **kwargs):
    """Reply without ever raising (a dead worker = a dead AI chat) and without live pings."""
    kwargs.setdefault("allowed_mentions", SAFE_MENTIONS)
    if "embed" in kwargs or "embeds" in kwargs or isinstance(kwargs.get("view"), discord.ui.LayoutView):
        # Cards are always sent as Components V2 (text goes inside the card).
        kwargs.update(v2(content, embed=kwargs.pop("embed", None), embeds=kwargs.pop("embeds", None),
                         view=kwargs.pop("view", None)))
        content = None
    try:
        return await msg.reply(content, **kwargs)
    except discord.HTTPException as e:
        if getattr(e, "status", None) == 429:
            await emergency_rate_limit_shutdown("safe_reply")
            return None
        try:
            return await msg.channel.send(content, **kwargs)
        except discord.HTTPException as e2:
            if getattr(e2, "status", None) == 429:
                await emergency_rate_limit_shutdown("safe_reply fallback")
            else:
                print("safe_reply failed:", e2)
            return None

async def add_event_role(guild, uid):
    r_id = get_config(guild.id).get("event_role")
    role = guild.get_role(r_id) if r_id else None
    mem = guild.get_member(uid)
    if role and mem:
        try: await mem.add_roles(role)
        except discord.HTTPException: pass

async def remove_event_role(guild, uid):
    r_id = get_config(guild.id).get("event_role")
    role = guild.get_role(r_id) if r_id else None
    mem = guild.get_member(uid)
    if role and mem:
        try: await mem.remove_roles(role)
        except discord.HTTPException: pass

_dm_last = {}

async def dm_owners(text, key=None, cooldown=300):
    """DM the bot owners. Repeats of the same alert are throttled so an outage can't spam them."""
    now = time.time()
    k = key or text[:40]
    if now - _dm_last.get(k, 0) < cooldown:
        return
    if len(_dm_last) > 200:
        _dm_last.clear()
    _dm_last[k] = now
    for owner_id in BOT_OWNER_IDS:
        try:
            owner = await bot.fetch_user(owner_id)
            await owner.send(text[:1900])
        except Exception as e:
            print("Failed to DM owner:", e)

async def log_action(guild, title, desc, color=0x3498DB, channel_key=None):
    # AI Admin actions have their own setup channel.  Everything else keeps using
    # the normal event log channel, so existing logging is unchanged.
    log_id = get_config(guild.id).get(channel_key or "log_channel")
    if not log_id:
        # Preserve the old fallback if an AI log channel was not configured.
        if channel_key == "ai_log_channel":
            log_id = get_config(guild.id).get("log_channel")
        if not log_id:
            return
    channel = guild.get_channel(log_id)
    if channel:
        emb = discord.Embed(description=f"## 📝 {title}\n{DIV}\n{desc}", color=color)
        try: 
            await channel.send(**v2(embed=emb), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException: 
            pass

# One solid, straight divider. (~~ strikethrough over blank Braille characters draws a continuous line;
# the box-drawing dashes it replaces rendered as broken segments on phones.)
DIV = "~~" + "\u2800" * 25 + "~~"
SECTION_SEP = f"\n{DIV}\n"


def add_footer_divider(embed, minimum_length=280):
    """Add the shared divider before footer metadata on medium/long embeds."""
    description = embed.description or ""
    fields_length = sum(len(field.name or "") + len(field.value or "") for field in embed.fields)
    if len(description) + fields_length >= minimum_length and description and not description.endswith(DIV):
        embed.description = description.rstrip() + f"\n{DIV}"
    return embed
BRAND = "AbyssalNoob1 is the Best"


# ==========================================
# COMPONENTS V2
# Every card the bot sends is a Components V2 message: a Container with the colored side line,
# real separator lines instead of the old text divider, small grey footers, avatars beside the
# text, and buttons/menus inside the card. Cards are still described with discord.Embed (handy
# to build), and v2() turns them into the V2 layout right before sending or editing.
# ==========================================
V2_TEXT_LIMIT = 4000      # Discord's limit for all text in one V2 message
V2_COMPONENT_LIMIT = 40   # ...and for the number of components


def _sep(big=False):
    return discord.ui.Separator(spacing=discord.SeparatorSpacing.large if big else discord.SeparatorSpacing.small)


def _split_on_div(text):
    """Split text at the old divider lines; each chunk becomes its own block between separators."""
    chunks, cur = [], []
    for line in (text or "").split("\n"):
        if line.strip() == DIV:
            chunks.append("\n".join(cur).strip("\n"))
            cur = []
        else:
            cur.append(line)
    chunks.append("\n".join(cur).strip("\n"))
    return [c for c in chunks if c.strip()]


def _fields_text(fields):
    """Embed fields as text: inline ones as compact 'name · value' lines, others as a small heading + text."""
    blocks, inline = [], []
    for f in fields:
        if f.inline:
            inline.append(f"{f.name} · {f.value}" if f.name.strip() not in ("", "​") else f.value)
            continue
        if inline:
            blocks.append("\n".join(inline))
            inline = []
        blocks.append(f"**{f.name}**\n{f.value}" if f.name.strip() not in ("", "​") else f.value)
    if inline:
        blocks.append("\n".join(inline))
    return [b for b in blocks if b.strip()]


def embed_container(e, rows=()):
    """One discord.Embed -> one V2 Container (plus optional ActionRows placed at the bottom of the card)."""
    color = e.colour.value if e.colour else None
    c = discord.ui.Container(accent_colour=color)
    head = []
    if e.author and e.author.name:
        head.append(f"-# {e.author.name}")
    if e.title:
        head.append(f"## {e.title}")
    blocks = _split_on_div(e.description)
    if head:
        if blocks and not blocks[0].lstrip().startswith("#"):
            blocks[0] = "\n".join(head) + "\n" + blocks[0]
        else:
            blocks.insert(0, "\n".join(head))
    field_blocks = _fields_text(e.fields)

    thumb = e.thumbnail.url if e.thumbnail and e.thumbnail.url else None
    first = True
    for b in blocks:
        if not first:
            c.add_item(_sep())
        if first and thumb:
            c.add_item(discord.ui.Section(discord.ui.TextDisplay(b), accessory=discord.ui.Thumbnail(thumb)))
            thumb = None
        else:
            c.add_item(discord.ui.TextDisplay(b))
        first = False
    if field_blocks:
        if not first:
            c.add_item(_sep())
        text = "\n\n".join(field_blocks)
        if thumb:
            c.add_item(discord.ui.Section(discord.ui.TextDisplay(text), accessory=discord.ui.Thumbnail(thumb)))
        else:
            c.add_item(discord.ui.TextDisplay(text))
        first = False
    if e.image and e.image.url:
        c.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(e.image.url)))
        first = False
    rows = list(rows)
    if rows:
        if not first:
            c.add_item(_sep())
        for r in rows:
            c.add_item(r)
    foot = []
    if e.footer and e.footer.text:
        foot.append(e.footer.text)
    if e.timestamp:
        foot.append(f"<t:{int(e.timestamp.timestamp())}:f>")
    if foot:
        c.add_item(_sep())
        c.add_item(discord.ui.TextDisplay("-# " + "  ·  ".join(foot)))
    return c


def _rows_from_view(view):
    """Put a classic View's buttons/selects into ActionRows, keeping their row order."""
    if view is None:
        return []
    groups = {}
    for item in view.children:
        groups.setdefault(item.row if item.row is not None else 99, []).append(item)
    rows = []
    for _, items in sorted(groups.items()):
        cur = []
        for item in items:
            is_select = not isinstance(item, discord.ui.Button)
            if is_select or len(cur) >= 5:
                if cur:
                    rows.append(discord.ui.ActionRow(*cur))
                cur = []
            if is_select:
                rows.append(discord.ui.ActionRow(item))
            else:
                cur.append(item)
        if cur:
            rows.append(discord.ui.ActionRow(*cur))
    return rows


class V2View(discord.ui.LayoutView):
    """The V2 layout around a classic View: the classic View keeps its buttons, callbacks, checks,
    timeout and wait(), this class only carries the layout Discord shows."""

    def __init__(self, inner=None, timeout=None):
        super().__init__(timeout=inner.timeout if inner is not None else timeout)
        self._inner = inner
        if inner is not None:
            inner_stop = inner.stop
            outer = self

            def stop():
                inner_stop()
                discord.ui.LayoutView.stop(outer)
            inner.stop = stop
            inner._v2 = self

    def __getattr__(self, name):
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    async def interaction_check(self, interaction):
        if self._inner is not None:
            return await self._inner.interaction_check(interaction)
        return not await disabled_guild_response(interaction)

    async def on_timeout(self):
        if self._inner is not None:
            await self._inner.on_timeout()
            self._inner.stop()

    async def on_error(self, interaction, error, item):
        if self._inner is not None:
            return await self._inner.on_error(interaction, error, item)
        return await super().on_error(interaction, error, item)


def _plain_container(text, color=None):
    c = discord.ui.Container(accent_colour=color)
    for i, b in enumerate(_split_on_div(text) or [text]):
        if i:
            c.add_item(_sep())
        c.add_item(discord.ui.TextDisplay(b))
    return c


def build_layout(content=None, embeds=(), view=None, color=None):
    """Make the LayoutView for a message. Buttons/menus of `view` go inside the last card."""
    lay = getattr(view, "_v2", None) if view is not None else None
    if lay is None or not isinstance(lay, V2View):
        lay = V2View(view) if view is not None else V2View(timeout=None)
    else:
        lay.clear_items()
    rows = _rows_from_view(view)
    if content:
        if embeds:
            lay.add_item(discord.ui.TextDisplay(content))
        else:
            lay.add_item(_plain_container(content, color))
            for r in rows:
                lay.add_item(r)
            return lay
    embeds = list(embeds)
    for i, e in enumerate(embeds):
        lay.add_item(embed_container(e, rows if i == len(embeds) - 1 else ()))
    if not embeds and rows:
        for r in rows:
            lay.add_item(r)
    return lay


def v2(content=None, *, embed=None, embeds=None, view=None, edit=False, color=None):
    """Keyword arguments for send()/edit()/send_message()/edit_message() that render as Components V2.
    With edit=True the old content/embeds are cleared too, which also turns an old-style message into V2."""
    if embed is not None:
        embeds = [embed]
    if isinstance(view, discord.ui.LayoutView) and not embeds and not content:
        lay = view   # already a hand-built V2 layout
    else:
        lay = build_layout(content, embeds or [], view, color)
    kw = {"view": lay}
    if edit:
        kw.update(content=None, embeds=[])
    return kw


def message_text(msg):
    """All text of a message: content, embed titles/descriptions and V2 text blocks."""
    out = [msg.content or ""]
    for e in msg.embeds:
        out += [e.title or "", e.description or ""]
    stack = list(getattr(msg, "components", []) or [])
    while stack:
        c = stack.pop()
        if getattr(c, "content", None):
            out.append(c.content)
        stack.extend(getattr(c, "children", []) or [])
        acc = getattr(c, "accessory", None)
        if acc is not None:
            stack.append(acc)
    return "\n".join(out)


def card_text(title, body="", color=None, footer=None):
    """Quick one-card message: a heading, body text (old dividers become separators) and a footer."""
    e = discord.Embed(description=(f"## {title}\n{DIV}\n{body}" if body else f"## {title}"), color=color)
    if footer:
        e.set_footer(text=footer)
    return e
EVENT_PART_LIMIT = 3800   # split the banner into another message before Discord's 4000-character V2 limit
EVENT_BUTTONS = None      # set by events.py: returns the persistent Claim/Swap/Leave/Host buttons view


def _event_state(info):
    now = int(time.time())
    start, end = info["start_time"], info["end_time"]
    if info.get("concluded"):
        return "🔒", 0xE74C3C, "🔒 Concluded"
    if now >= end:
        return "⚔️", 0xF1C40F, f"⚔️ Live · reservations closed <t:{end}:R>"
    if now < start:
        return "🕒", 0x3498DB, f"🕒 Opens <t:{start}:R>"
    return "🚩", 0x2ECC71, f"🟢 Open · closes <t:{end}:R>"


def event_blocks(name, info):
    """The banner as (kind, text) blocks in display order. 'lines' blocks can be split between messages."""
    start, end = info["start_time"], info["end_time"]
    host, cohost = info["host_id"], info.get("cohost_id")
    res = info["reservations"]
    wait = info.get("waitlist", [])
    sps = info.get("superpowers", [])
    helpers = info.get("helpers", [])

    sp_set = {s.title() for s in sps}
    sp_lines, reg_lines = [], []
    for country, u in res.items():
        line = f"• **{country}** · `{u['roblox']}` · <@{u['discord_id']}>"
        (sp_lines if country.title() in sp_set else reg_lines).append(line)

    emoji, _, status = _event_state(info)
    h_str = f"<@{host}>" + (f" & <@{cohost}>" if cohost else "")
    sched = (f"### 🗓️ Schedule\n• **Starts:** <t:{start}:f>\n• **Ends:** <t:{end}:f>\n• **Host:** {h_str}")
    if info.get("trial"):
        sched += "\n• **🎓 Trial event:** players can leave feedback on this trial host"
    if helpers:
        sched += "\n• **Helpers:** " + ", ".join(f"<@{i}>" for i in helpers[:15])

    blocks = [("head", f"# {emoji} {name}\n## {status}"), ("text", sched)]
    if sps:
        unclaimed = [s for s in sps if s.title() not in res]
        body = "\n".join(sp_lines)
        if unclaimed:
            body += ("\n" if body else "") + "*Available:* " + ", ".join(f"**{s}**" for s in unclaimed)
        blocks.append(("text", f"### ⭐ Superpowers\n{body}"))
    blocks.append(("lines", (f"### 📋 Reservations ({len(reg_lines)})", reg_lines or ["*No reservations yet*"])))
    if wait:
        w_lines = [f"• **{w['country']}** · <@{w['discord_id']}>" for w in wait[:5]]
        more = f"\n*+{len(wait) - 5} more*" if len(wait) > 5 else ""
        blocks.append(("bottom", f"### ⏳ Waitlist ({len(wait)})\n" + "\n".join(w_lines) + more))
    return blocks


def _pack_event(name, info):
    """Pack the banner blocks into messages of at most EVENT_PART_LIMIT characters.
    Returns a list of parts, each a list of text blocks (one block = one text area between separators)."""
    blocks = event_blocks(name, info)
    emoji = _event_state(info)[0]
    max_p = info.get("max_players", 0)
    footer = (f"-# {len(info['reservations'])}{f' / {max_p}' if max_p > 0 else ''} reservations"
              f"  ·  updated live  ·  {BRAND}")
    bottom = [t for k, t in blocks if k == "bottom"]
    bottom_len = sum(len(t) for t in bottom) + len(footer)
    parts, cur, size = [], [], 0

    def new_part():
        nonlocal cur, size
        parts.append(cur)
        cont = f"### {emoji} {name} · continued"
        cur, size = [cont], len(cont)

    for kind, text in blocks:
        if kind == "bottom":
            continue
        if kind == "lines":
            heading, lines = text
            chunk = heading
            for ln in lines:
                if size + len(chunk) + len(ln) + 1 > EVENT_PART_LIMIT - 50:
                    cur.append(chunk)
                    new_part()
                    chunk = "### 📋 Reservations (continued)"
                chunk += "\n" + ln
            cur.append(chunk)
            size += len(chunk)
            continue
        if cur and size + len(text) > EVENT_PART_LIMIT - 50:
            new_part()
        cur.append(text)
        size += len(text)
    # The waitlist and footer always go on the LAST message, under the reservations.
    if size + bottom_len > EVENT_PART_LIMIT - 50:
        new_part()
    cur.extend(bottom)
    parts.append(cur)
    if len(parts) > 1:
        parts[0][0] = parts[0][0].replace(f"# {emoji} {name}", f"# {emoji} {name} · Part 1/{len(parts)}", 1)
        for i in range(1, len(parts)):
            parts[i][0] = f"### {emoji} {name} · Part {i + 1}/{len(parts)}"
    parts[-1].append(footer)
    return parts


def build_event_layouts(info):
    """One LayoutView per message. Buttons sit inside the last card (gone once the event is concluded)."""
    name = info["display_name"]
    color = _event_state(info)[1]
    parts = _pack_event(name, info)
    layouts = []
    for i, blocks in enumerate(parts):
        last = i == len(parts) - 1
        buttons = EVENT_BUTTONS() if (last and EVENT_BUTTONS and not info.get("concluded")) else None
        lay = V2View(buttons) if buttons else V2View(timeout=None)
        c = discord.ui.Container(accent_colour=color)
        body = [b for b in blocks if not b.startswith("-# ")]
        foot = [b for b in blocks if b.startswith("-# ")]
        for j, b in enumerate(body):
            if j:
                c.add_item(_sep())
            c.add_item(discord.ui.TextDisplay(b))
        if buttons:
            c.add_item(_sep())
            for r in _rows_from_view(buttons):
                c.add_item(r)
        for f in foot:
            c.add_item(_sep())
            c.add_item(discord.ui.TextDisplay(f))
        lay.add_item(c)
        layouts.append(lay)
    return layouts


def build_embed(name, info, guild=None):
    """Kept for anything that still wants the banner text: one embed per message part."""
    out = []
    for blocks in _pack_event(name, info):
        out.append(discord.Embed(color=_event_state(info)[1], description=f"\n{DIV}\n".join(blocks)))
    return out


_event_locks = {}


async def post_event_messages(channel, info):
    """Send every part of a banner as new messages and remember their IDs."""
    ids = []
    for lay in build_event_layouts(info):
        m = await channel.send(view=lay, allowed_mentions=discord.AllowedMentions.none())
        ids.append(m.id)
    info["message_ids"] = ids
    return ids


async def refresh_event_message(info, guild=None, remove_view=False):
    """Bring the banner up to date. It can grow into more messages (posted right under the last part)
    or shrink (extra parts are deleted). If something else was posted in between, the whole banner
    is re-posted so its parts always stay together."""
    ch = bot.get_channel(info.get("channel_id"))
    ids = list(info.get("message_ids") or [])
    if not ch or not ids:
        return False
    lock = _event_locks.setdefault(id(info), asyncio.Lock())
    async with lock:
        layouts = build_event_layouts(info)
        try:
            needs_more = len(layouts) > len(ids)
            if needs_more and ch.last_message_id not in (None, ids[-1]):
                raise _Repost()
            for i, lay in enumerate(layouts):
                if i < len(ids):
                    await ch.get_partial_message(ids[i]).edit(view=lay, content=None, embeds=[],
                                                              allowed_mentions=discord.AllowedMentions.none())
                else:
                    m = await ch.send(view=lay, allowed_mentions=discord.AllowedMentions.none())
                    ids.append(m.id)
            for extra in ids[len(layouts):]:
                try:
                    await ch.get_partial_message(extra).delete()
                except discord.HTTPException:
                    pass
            info["message_ids"] = ids[:len(layouts)]
            save_db(db)
            return True
        except (_Repost, discord.HTTPException) as e:
            # Re-post only when the parts got separated, a part was deleted, or Discord refused the
            # edit (400, e.g. an old banner it won't convert). Other errors just retry next time.
            if isinstance(e, discord.HTTPException) and not isinstance(e, discord.NotFound) and e.status != 400:
                print("refresh_event_message failed:", e)
                return False
            # Re-post the whole banner together (also converts very old banners Discord won't edit).
            try:
                new_ids = []
                for lay in layouts:
                    m = await ch.send(view=lay, allowed_mentions=discord.AllowedMentions.none())
                    new_ids.append(m.id)
            except discord.HTTPException as e2:
                print("refresh_event_message failed:", e2)
                return False
            for old in ids:
                try:
                    await ch.get_partial_message(old).delete()
                except discord.HTTPException:
                    pass
            info["message_ids"] = new_ids
            save_db(db)
            return True


class _Repost(Exception):
    pass


# ==========================================
# PER-EVENT LOG EMBED (one live embed per event; the category dropdown + page buttons edit it in place)
# ==========================================
LOG_CATS = {
    "reservation": ("📋", "Reservations"),
    "waitlist": ("⏳", "Waitlist"),
    "swap": ("🔄", "Swaps"),
    "host": ("🛠️", "Host actions"),
}
LOG_OPTIONS = [("all", "All activity", "📜"), ("reservation", "Reservations", "📋"),
               ("waitlist", "Waitlist", "⏳"), ("swap", "Swaps", "🔄"), ("host", "Host actions", "🛠️")]
LOG_BLUE = 0x3498DB
LOG_PAGE_SIZE = 5

def _fmt_ts(t):
    # Time only for recent entries, date + time once they're older than about 20 hours.
    return f"<t:{t}:d> <t:{t}:t>" if time.time() - t > 72000 else f"<t:{t}:t>"

def _log_line(e):
    emoji = LOG_CATS.get(e["c"], ("•", ""))[0]
    return f"{emoji} {_fmt_ts(e['t'])} · {e['m']}"

def log_state(info):
    v = info.get("log_view") or {}
    cat = v.get("cat", "all")
    if cat != "all" and cat not in LOG_CATS:
        cat = "all"
    rows = list(reversed([e for e in info.get("logs", []) if cat == "all" or e["c"] == cat]))
    pages = max(1, -(-len(rows) // LOG_PAGE_SIZE))
    try:
        page = int(v.get("page", 0))
    except (TypeError, ValueError):
        page = 0
    return cat, max(0, min(page, pages - 1)), pages, rows

def build_log_embed(info):
    cat, page, pages, rows = log_state(info)
    logs = info.get("logs", [])
    counts = {c: sum(1 for e in logs if e["c"] == c) for c in LOG_CATS}
    host = f"👑 **Host** · <@{info['host_id']}>" + (f" & <@{info['cohost_id']}>" if info.get("cohost_id") else "")
    stats = "\n".join(f"{LOG_CATS[c][0]} **{LOG_CATS[c][1]}** · {counts[c]}" for c in LOG_CATS)
    sec = "🕒 Latest activity" if cat == "all" else f"{LOG_CATS[cat][0]} {LOG_CATS[cat][1]}"
    body = "\n".join(_log_line(e) for e in rows[page * LOG_PAGE_SIZE:(page + 1) * LOG_PAGE_SIZE]) or "*Nothing here yet*"
    desc = (f"# 📜 Event Log · {info['display_name']}\n{DIV}\n"
            f"### 📊 Overview\n{host}\n{stats}\n{DIV}\n"
            f"### {sec}\n{body}")
    if info.get("helpers"):
        desc += f"\n{DIV}\n### 🤝 Helpers\n" + ", ".join(f"<@{i}>" for i in info["helpers"][:20])
    desc += f"\n{DIV}"
    emb = discord.Embed(color=LOG_BLUE, description=desc[:4000])
    state = "🔒 Concluded" if info.get("concluded") else "🟢 Live"
    add_footer_divider(emb)
    emb.set_footer(text=f"{len(logs)} entries · {state} · Page {page + 1}/{pages}")
    return emb

def find_event_by_log(msg_id):
    for info in db["events"].values():
        if (info.get("log_msg") or {}).get("id") == msg_id:
            return info
    return None

class LogCatSelect(discord.ui.Select):
    def __init__(self, cur="all"):
        opts = [discord.SelectOption(label=l, value=v, emoji=e, default=(v == cur)) for v, l, e in LOG_OPTIONS]
        super().__init__(custom_id="evlog_select", placeholder="Browse logs by category", options=opts, row=0)

    async def callback(self, interaction):
        info = find_event_by_log(interaction.message.id)
        if not info:
            await interaction.response.send_message("This event log is no longer available.", ephemeral=True)
            return
        info["log_view"] = {"cat": self.values[0], "page": 0}
        save_db(db)
        await interaction.response.edit_message(**v2(embed=build_log_embed(info), view=make_log_view(info), edit=True))

class LogPageButton(discord.ui.Button):
    def __init__(self, direction, disabled=True):
        super().__init__(label="◀ Prev" if direction < 0 else "Next ▶", style=discord.ButtonStyle.secondary,
                         custom_id="evlog_prev" if direction < 0 else "evlog_next", disabled=disabled, row=1)
        self.direction = direction

    async def callback(self, interaction):
        info = find_event_by_log(interaction.message.id)
        if not info:
            await interaction.response.send_message("This event log is no longer available.", ephemeral=True)
            return
        cat, page, pages, _ = log_state(info)
        info["log_view"] = {"cat": cat, "page": max(0, min(pages - 1, page + self.direction))}
        save_db(db)
        await interaction.response.edit_message(**v2(embed=build_log_embed(info), view=make_log_view(info), edit=True))

class GuardedView(discord.ui.View):
    """Base view that disables every button/select in disabled servers."""
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return not await disabled_guild_response(interaction)


class EventLogView(GuardedView):
    def __init__(self, cat="all", page=0, pages=1):
        super().__init__(timeout=None)
        self.add_item(LogCatSelect(cat))
        self.add_item(LogPageButton(-1, disabled=page <= 0))
        self.add_item(LogPageButton(+1, disabled=page >= pages - 1))

def make_log_view(info):
    cat, page, pages, _ = log_state(info)
    return EventLogView(cat, page, pages)

async def refresh_log_message(info):
    lm = info.get("log_msg")
    if not lm:
        return False
    ch = bot.get_channel(lm["ch"])
    if not ch:
        return False
    try:
        await ch.get_partial_message(lm["id"]).edit(
            **v2(embed=build_log_embed(info), view=make_log_view(info), edit=True),
            allowed_mentions=discord.AllowedMentions.none())
        return True
    except discord.NotFound:
        info.pop("log_msg", None)  # message was deleted: respawn on the next entry
        save_db(db)
        return False
    except discord.HTTPException as e:
        if e.status == 400:
            # An old-style log Discord won't convert: replace it with a fresh V2 one.
            try:
                await ch.get_partial_message(lm["id"]).delete()
            except discord.HTTPException:
                pass
            info.pop("log_msg", None)
            save_db(db)
            return False
        print("refresh_log_message failed:", e)
        return False

async def spawn_event_log(guild, info):
    log_id = get_config(guild.id).get("log_channel")
    ch = guild.get_channel(log_id) if log_id else None
    if not ch:
        return False
    try:
        m = await ch.send(**v2(embed=build_log_embed(info), view=make_log_view(info)),
                          allowed_mentions=discord.AllowedMentions.none())
        info["log_msg"] = {"ch": ch.id, "id": m.id}
        save_db(db)
        return True
    except discord.HTTPException as e:
        print("spawn_event_log failed:", e)
        return False

async def event_log(guild, info, cat, text):
    """Record an entry on the event and update (or create) its log embed."""
    logs = info.setdefault("logs", [])
    logs.append({"t": int(time.time()), "c": cat, "m": str(text)[:300]})
    if len(logs) > 500:
        del logs[:-500]
    save_db(db)
    if info.get("log_msg"):
        await refresh_log_message(info)
    if not info.get("log_msg"):
        await spawn_event_log(guild, info)

async def process_waitlist(info, opened, guild, channel):
    wait = info.get("waitlist", [])
    if not wait: 
        return

    res = info.get("reservations", {})
    max_p = info.get("max_players", 0)
    if max_p > 0 and len(res) >= max_p: 
        return

    banned = db["blacklists"].get(str(guild.id), [])
    sp_list = [s.title() for s in info.get("superpowers", [])]
    holders = {u["discord_id"] for u in res.values()}

    cands = []
    for w in list(wait):
        uid = w["discord_id"]
        c_name = w["country"].title()

        # Drop banned users and anyone who already holds a spot (prevents double spots).
        if uid in banned or uid in holders:
            wait.remove(w)
            continue

        if c_name in res:
            continue

        if c_name in sp_list and not is_vip(guild.get_member(uid), guild.id):
            continue

        cands.append(w)

    if not cands: 
        return

    promo = next((i for i in cands if i["country"].title() == opened.title()), cands[0])
    wait.remove(promo)

    info["reservations"][promo["country"].title()] = {"roblox": promo["roblox"], "discord_id": promo["discord_id"]}
    info["waitlist_fills"] = int(info.get("waitlist_fills", 0)) + 1
    await event_log(guild, info, "waitlist", f"<@{promo['discord_id']}> got **{promo['country'].title()}** from the waitlist")

    await add_event_role(guild, promo["discord_id"])

    # Tell the promoted player privately; only if their DMs are closed, ping them in the
    # event channel with a note that cleans itself up after 2 minutes.
    note = f"🔔 **{promo['country'].title()}** opened up in **{info['display_name']}** and you got it!"
    member = guild.get_member(int(promo["discord_id"]))
    dm_sent = False
    if member:
        try:
            await member.send(note)
            dm_sent = True
        except discord.HTTPException:
            pass
    if not dm_sent and channel:
        try:
            await channel.send(f"<@{promo['discord_id']}> {note}", delete_after=120)
        except discord.HTTPException:
            pass
