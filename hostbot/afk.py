"""AFK: "h!afk Too tired" marks you away. Pinging you gets a reply; talking again brings you back.

The same feature as the Hard bot's r!afk, with the h! prefix. It is a message trigger (like Hard's), not a
slash command: type it in any channel the bot can see.
"""

import re
import time

import discord

from hostbot import core
from hostbot.core import bot, db, is_disabled_guild, is_whitelisted_guild, save_db
from hostbot.shared import safe_reply
from hostbot.ui import COLORS, card, divider, footnote, msg as card_msg, section, text, thumb

COMMAND = re.compile(r"h!afk(?:\s+(.*))?", re.I | re.S)
REPLY_COOLDOWN = 60          # seconds: one "they're AFK" reply per channel and person
MAX_PINGS = 10               # pings remembered while someone is away
AFK_TAG = "[AFK] "
MAX_NICK = 32                # Discord's nickname limit
_AFK_PREFIX = re.compile(r"^\[AFK\]\s*", re.I)
_last_reply = {}             # (channel id, user id) -> time


# ---------------------------------------------------------------- the [AFK] tag in the nickname

def strip_afk(name):
    return _AFK_PREFIX.sub("", name or "")


def with_afk(name, on):
    """'Superman' -> '[AFK] Superman' (cut to fit Discord's 32 characters), or the tag removed."""
    base = strip_afk(name)
    prefix = AFK_TAG if on else ""
    return f"{prefix}{base[:MAX_NICK - len(prefix)].rstrip()}"


def can_rename(member):
    """The bot can't rename the server owner or anyone whose top role is above its own."""
    g, me = member.guild, member.guild.me
    return bool(me and member.id != g.owner_id and me.guild_permissions.manage_nicknames
                and member.top_role < me.top_role)


async def set_afk_tag(member, on):
    """Adds or removes the [AFK] tag. Silently skips people the bot can't rename."""
    if not can_rename(member):
        return False
    nxt = with_afk(member.nick or member.display_name, on)
    plain = member.global_name or member.name
    value = None if (not on and nxt == plain) else nxt  # no custom name left: clear the nickname instead of freezing it
    if value == member.nick:
        return False
    try:
        await member.edit(nick=value, reason="AFK" if on else "Back from AFK")
        return True
    except discord.HTTPException:
        return False


# ---------------------------------------------------------------- who is away (saved, so a restart doesn't forget)

def get_afk(guild_id, user_id):
    return db.get("afk", {}).get(str(guild_id), {}).get(str(user_id))


def save_afk(guild_id, user_id, record):
    store = db.setdefault("afk", {}).setdefault(str(guild_id), {})
    if record is None:
        store.pop(str(user_id), None)
    else:
        store[str(user_id)] = record
    save_db(db)


def _ts(t):
    return f"<t:{int(t)}:R>"


async def _reply(message, layout_kwargs, seconds):
    """Reply without pinging anyone; the reply cleans itself up."""
    try:
        await safe_reply(message, delete_after=seconds, **layout_kwargs)
    except Exception as e:
        print("afk reply failed:", e)


# ---------------------------------------------------------------- one message

async def handle_afk(message):
    """Handles AFK for one message. Returns True if it was the h!afk command."""
    if message.guild is None or message.author.bot or core.RATE_LIMIT_TRIGGERED:
        return False
    if not is_whitelisted_guild(message.guild.id) or is_disabled_guild(message.guild.id):
        return False
    g, author = message.guild, message.author
    name = strip_afk(author.display_name)

    # 1. "h!afk reason"
    cmd = COMMAND.fullmatch((message.content or "").strip())
    if cmd:
        reason = re.sub(r"@(everyone|here)", "@​\\1", (cmd.group(1) or "").strip())[:200] or "AFK"
        save_afk(g.id, author.id, {"reason": reason, "since": int(time.time()), "pings": []})
        await set_afk_tag(author, True)
        await _reply(message, card_msg(card(
            COLORS["info"],
            section([f"### 💤 {name} is now AFK", f"> {reason}"], thumb(author.display_avatar.url)),
            footnote("Send any message to come back"))), 15)
        return True

    # 2. An AFK person talks again: welcome them back.
    mine = get_afk(g.id, author.id)
    if mine:
        save_afk(g.id, author.id, None)
        await set_afk_tag(author, False)
        pings = mine.get("pings") or []
        away = f"**{len(pings)}** ping{'s' if len(pings) != 1 else ''} while you were away" if pings else "nobody pinged you"
        await _reply(message, card_msg(card(
            COLORS["ok"],
            text(f"### 👋 Welcome back, {name}!", f"You went AFK {_ts(mine['since'])} · {away}"),
            [divider(), text([f"• <@{p['by']}> in <#{p['channel']}> · [jump]({p['url']})" for p in pings[-5:]])]
            if pings else None)), 30)

    # 3. Someone pinged (or replied to) an AFK person.
    targets = {m.id: m for m in message.mentions if not m.bot and m.id != author.id}
    ref = message.reference.resolved if message.reference else None
    if isinstance(ref, discord.Message) and not ref.author.bot and ref.author.id != author.id:
        targets[ref.author.id] = ref.author

    notes = []
    for uid, user in targets.items():
        afk = get_afk(g.id, uid)
        if not afk:
            continue
        afk.setdefault("pings", []).append(
            {"by": author.id, "channel": message.channel.id, "url": message.jump_url, "at": int(time.time())})
        del afk["pings"][:-MAX_PINGS]
        save_afk(g.id, uid, afk)
        key = (message.channel.id, uid)
        if time.time() - _last_reply.get(key, 0) < REPLY_COOLDOWN:
            continue
        _last_reply[key] = time.time()
        who = strip_afk((g.get_member(uid) or user).display_name)
        notes.append(f"💤 **{who}** is AFK: {afk['reason']} · {_ts(afk['since'])}")
    if len(_last_reply) > 500:
        _last_reply.clear()
    if notes:
        await _reply(message, card_msg(card(COLORS["info"], text(notes))), 20)
    return False


@bot.listen("on_message")
async def _afk_on_message(message):
    try:
        await handle_afk(message)
    except Exception as e:
        print("afk error:", e)


@bot.listen("on_member_update")
async def _afk_keep_tag(before, after):
    """Changing your own nickname while AFK is fine: the [AFK] tag is put back right away."""
    if before.nick == after.nick or after.bot:
        return
    if not get_afk(after.guild.id, after.id) or is_disabled_guild(after.guild.id):
        return
    await set_afk_tag(after, True)
