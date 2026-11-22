"""The shared look for setup, moderation, profiles and the panel builder.

A Python port of the look used by the Hard bot (its src/lib/ui.js): a soft colour palette, rounded
Components V2 cards with a coloured edge, real divider lines, small grey footers, an avatar beside
the text, and short status cards (notice) for replies. Screens are built from these pieces instead
of discord.Embed, so they all feel like one app.

    card(COLORS["ok"], section(["### Name", "text"], thumb(url)), divider(), footnote("small text"))
    await interaction.response.send_message(**msg(card(...)), ephemeral=True)
    await interaction.response.send_message(**notice("ok", "Saved", title="Done"), ephemeral=True)

Buttons and menus of a classic discord.ui.View go inside the card with rows(view).
"""

import time

import discord

from hostbot.core import get_config
from hostbot.shared import V2View, _rows_from_view

COLORS = {
    "brand": 0x6C7BFF,  # soft indigo: the default
    "ok": 0x3FCF8E,
    "warn": 0xF5A524,
    "bad": 0xF2555A,
    "info": 0x56B6F7,
}
ICONS = {"ok": "✅", "warn": "⚠️", "bad": "⛔", "info": "💬", "brand": "✨"}
NO_MENTIONS = discord.AllowedMentions.none()


# ---------------------------------------------------------------- building blocks

def _flat(items):
    for it in items:
        if isinstance(it, (list, tuple)):
            yield from _flat(it)
        elif it is not None and it is not False:
            yield it


def text(*lines):
    """A block of markdown text. Pass lines; None/False lines are skipped."""
    body = "\n".join(str(x) for x in _flat(lines))
    return discord.ui.TextDisplay(body or "​")


def divider(big=False, line=True):
    """A horizontal line (or just spacing with line=False)."""
    return discord.ui.Separator(
        visible=line, spacing=discord.SeparatorSpacing.large if big else discord.SeparatorSpacing.small)


def footnote(s):
    """Small grey footer line."""
    return text(f"-# {s}")


def thumb(url):
    return discord.ui.Thumbnail(url) if url else None


def gallery(*urls):
    items = [discord.MediaGalleryItem(u) for u in _flat(urls) if u]
    return discord.ui.MediaGallery(*items) if items else None


def section(content, accessory=None):
    """Text with a thumbnail or a button on the right. Plain text when there is no accessory."""
    t = content if isinstance(content, discord.ui.TextDisplay) else text(content)
    if accessory is None:
        return t
    return discord.ui.Section(t, accessory=accessory)


def card(accent, *parts):
    """A rounded card with a coloured left edge. Parts: strings, text/section/divider/gallery/rows."""
    c = discord.ui.Container(accent_colour=accent)
    for p in _flat(parts):
        c.add_item(discord.ui.TextDisplay(p) if isinstance(p, str) else p)
    return c


def progress(done, total):
    """Slim progress bar, e.g. ▰▰▰▱▱▱"""
    done = max(0, min(done, total))
    return f"`{'▰' * done}{'▱' * (total - done)}`"


def rows(view):
    """The buttons and menus of a classic View as ActionRows, to put inside a card."""
    return _rows_from_view(view) if view is not None else []


# ---------------------------------------------------------------- messages

def msg(*cards, view=None, edit=False, mentions=NO_MENTIONS):
    """Keyword arguments for send()/edit()/send_message()/edit_message(). Nobody is pinged unless you pass
    `mentions`. The classic `view` (if any) keeps handling its buttons; put its rows inside a card with rows(view).
    edit=True also clears old content/embeds, which turns an older message into a card message."""
    lay = getattr(view, "_v2", None) if view is not None else None
    if lay is None or not isinstance(lay, V2View):
        lay = V2View(view) if view is not None else V2View(timeout=None)
    else:
        lay.clear_items()
    for c in _flat(cards):
        lay.add_item(c)
    kw = {"view": lay}
    if mentions is not None:
        kw["allowed_mentions"] = mentions
    if edit:
        kw.update(content=None, embeds=[])
    return kw


def notice(kind, message, title=None, edit=False):
    """Short status card: notice("ok", "Saved!") or notice("bad", "Nope", title="Not allowed").
    Use it with ephemeral=True for replies only the user should see; edit=True when it replaces an older message."""
    icon = ICONS.get(kind, "")
    body = [f"### {icon} {title}", message] if title else [f"{icon}  {message}"]
    return msg(card(COLORS.get(kind, COLORS["brand"]), text(body)), edit=edit)


# ---------------------------------------------------------------- moderation log + DM

_MOD_STYLE = {
    "Warn": ("⚠️", COLORS["warn"]),
    "Warning removed": ("🧽", COLORS["ok"]),
    "Blacklist": ("⛔", COLORS["bad"]),
    "Blacklist removed": ("🕊️", COLORS["ok"]),
}


async def mod_log(guild, action, target, moderator, reason=None, extra=None, color=None):
    """One card per moderation action in the event log channel: what happened, to whom, by whom and why.
    `target` is a member/user, `moderator` a member/user or a mention string, `extra` an optional (name, value)."""
    log_id = get_config(guild.id).get("log_channel")
    channel = guild.get_channel(log_id) if log_id else None
    if channel is None:
        return
    icon, accent = _MOD_STYLE.get(action, ("🛡️", color or COLORS["warn"]))
    name = getattr(target, "display_name", None) or getattr(target, "global_name", None) or getattr(target, "name", None) \
        or str(target.id)
    avatar = getattr(getattr(target, "display_avatar", None), "url", None)
    by = moderator if isinstance(moderator, str) else moderator.mention
    lines = [
        f"### {icon} {action}",
        f"**Member** **{name}** · `{getattr(target, 'name', target.id)}`",
        f"**By** {by}",
        f"**Reason** {reason or 'No reason given'}",
        f"**{extra[0]}** {extra[1]}" if extra else None,
    ]
    card_ = card(color or accent,
                 section(lines, thumb(avatar)),
                 divider(),
                 footnote(f"User ID {target.id} · <t:{int(time.time())}:f>"))
    try:
        await channel.send(**msg(card_))
    except discord.HTTPException as e:
        print("mod log failed:", e)


async def notify(user, guild, title, reason, kind="warn", extra=None):
    """Best-effort DM to the member (closed DMs are fine): which server, what happened, why."""
    icon = guild.icon.url if getattr(guild, "icon", None) else None
    card_ = card(COLORS[kind],
                 section([f"-# {guild.name}", f"### {title}"], thumb(icon)),
                 divider(),
                 text(f"**Reason**\n> {reason}", extra),
                 footnote("If you think this was a mistake, contact the server staff."))
    try:
        await user.send(**msg(card_))
        return True
    except discord.HTTPException:
        return False
