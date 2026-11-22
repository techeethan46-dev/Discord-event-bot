"""/panel - write rich messages (rules, info, announcements) from banners and cards, right inside Discord.

A message is a list of blocks, each shown as a card:
  banner: {"type": "banner", "image": url, "color": int|None}
  card:   {"type": "card", "text": str, "color": int|None, "thumb": url|None, "image": url|None,
           "buttons": [{"label": str, "url": str}]}

The builder, the limits and the way channels/roles are typed follow the /message builder of the Hard bot.
"""

import re
import time
from urllib.parse import urlparse

import discord
from discord import app_commands

from hostbot import core
from hostbot.core import bot, db, save_db
from hostbot.shared import GuardedView, emergency_rate_limit_shutdown, is_server_admin, log_action
from hostbot.ui import COLORS, card, divider, footnote, gallery, msg, notice, rows, section, text, thumb

# Discord allows 4000 characters of text and 40 components in one message; stay a little under both.
MAX_TEXT = 3800
MAX_COMPONENTS = 38
MAX_BLOCKS = 8
SAMPLE_COLORS = "#5865F2, blue, gold, red"
ATTACHMENT = "attachment://"


# ---------------------------------------------------------------- reading what people type

_NAMED = {
    "blue": 0x5865F2, "red": 0xF2555A, "green": 0x3FCF8E, "gold": 0xF7C948, "yellow": 0xF5D547, "orange": 0xF5A524,
    "purple": 0xA66BFF, "pink": 0xFF6FB5, "cyan": 0x56B6F7, "white": 0xF2F3F5, "grey": 0x8E9297, "gray": 0x8E9297,
    "black": 0x1E1F22,
}


def parse_color(raw):
    """'#ff8800', 'ff8800', 'f80' or a colour name -> (value, error). Empty -> (None, None)."""
    s = str(raw or "").strip().lower().lstrip("#")
    if not s:
        return None, None
    if s in _NAMED:
        return _NAMED[s], None
    if re.fullmatch(r"[0-9a-f]{6}", s):
        return int(s, 16), None
    if re.fullmatch(r"[0-9a-f]{3}", s):
        return int("".join(c * 2 for c in s), 16), None
    return None, f'"{raw}" isn\'t a color. Use a hex code like #5865F2, or a name like blue, red, gold or purple.'


def parse_image(raw, label="image"):
    """Image links must be https:// and point straight at the picture -> (url, error)."""
    s = str(raw or "").strip()
    if not s:
        return None, None
    u = urlparse(s)
    if u.scheme != "https" or not u.netloc:
        return None, f"The {label} must be a link starting with https:// (a direct link to the picture)."
    return s, None


def parse_buttons(raw):
    """One 'Label | https://link' per line, up to 5 -> (buttons, error)."""
    lines = [l.strip() for l in str(raw or "").split("\n") if l.strip()]
    if len(lines) > 5:
        return None, "A card can have up to 5 buttons."
    out = []
    for line in lines:
        label, _, link = line.partition("|")
        url, err = parse_image(link, "button link")
        if not label.strip() or not link.strip() or err or not url:
            return None, f'Button "{line[:40]}" must look like: Join the game | https://www.roblox.com/games/123'
        out.append({"label": label.strip()[:80], "url": url})
    return out, None


# ---------------------------------------------------------------- #channel and @Role while typing

# Names are compared ignoring case, spaces and symbols, so "#rules" finds a channel called "┃📜・rules".
def _simple(s):
    return re.sub(r"[\W_]+", "", str(s or "").lower())


def _token(sign):
    return re.compile(rf"(^|[\s(\[]){re.escape(sign)}([^\W_][\w-]*)")


def _only(items, name):
    want = _simple(name)
    found = [x for x in items if _simple(x.name) and _simple(x.name) == want]
    return found[0] if len(found) == 1 else None  # two with the same name: better to leave it than link the wrong one


_LEGACY_CHANNEL = re.compile(r"\{#([^}]+)\}")
_LEGACY_ROLE = re.compile(r"\{@([^}]+)\}")


def resolve_mentions(raw, guild):
    """Typed `#rules` becomes a real channel link and `@VIP` a role link, when exactly one channel/role has that name.
    The older `{#rules}` / `{@VIP}` placeholders still work. Role links don't ping anyone."""
    channels = list(guild.channels)
    roles = [r for r in guild.roles if r.id != guild.id]
    t = str(raw or "")

    def legacy_chan(m):
        c = _only(channels, m.group(1))
        return f"<#{c.id}>" if c else f"#{m.group(1)}"

    def legacy_role(m):
        r = _only(roles, m.group(1))
        return f"<@&{r.id}>" if r else f"@{m.group(1)}"

    def chan(m):
        c = _only(channels, m.group(2))
        return f"{m.group(1)}<#{c.id}>" if c else m.group(0)

    def role(m):
        if re.fullmatch(r"(?i)everyone|here", m.group(2)):
            return m.group(0)
        r = _only(roles, m.group(2))
        return f"{m.group(1)}<@&{r.id}>" if r else m.group(0)

    t = _LEGACY_ROLE.sub(legacy_role, _LEGACY_CHANNEL.sub(legacy_chan, t))
    return _token("@").sub(role, _token("#").sub(chan, t))


def readable_mentions(raw, guild):
    """The reverse, for editing: <#123> becomes #rules again (only when typing that back finds the same channel)."""
    channels = list(guild.channels)
    roles = [r for r in guild.roles if r.id != guild.id]

    def back(sign, item_id, items):
        item = next((x for x in items if x.id == item_id), None)
        t = re.sub(r"[^\w-]+", "", str(item.name)) if item else ""
        if t and re.match(r"[^\W_]", t):
            hit = _only(items, t)
            if hit is not None and hit.id == item_id:
                return f"{sign}{t}"
        return None

    t = re.sub(r"<#(\d+)>", lambda m: back("#", int(m.group(1)), channels) or m.group(0), str(raw or ""))
    return re.sub(r"<@&(\d+)>", lambda m: back("@", int(m.group(1)), roles) or m.group(0), t)


# ---------------------------------------------------------------- limits

def text_length(blocks):
    return sum(len(b.get("text") or "") for b in blocks)


def component_count(blocks):
    """How many of Discord's 40 components the message uses (rounded up a bit to be safe)."""
    n = 0
    for b in blocks:
        if b["type"] == "banner":
            n += 1 + 2
        else:
            n += 1 + (3 if b.get("thumb") else 1) + (2 if b.get("image") else 0)
            if b.get("buttons"):
                n += 1 + len(b["buttons"])
    return n


def problem(blocks):
    """A problem that would stop this message from being sent, or None."""
    if not blocks:
        return "Add at least one card or banner first."
    if len(blocks) > MAX_BLOCKS:
        return f"A message can have up to {MAX_BLOCKS} blocks."
    chars = text_length(blocks)
    if chars > MAX_TEXT:
        return (f"Too long: one message holds {MAX_TEXT:,} characters of text and you're at {chars:,}. "
                "Put the rest in a second message.")
    if component_count(blocks) > MAX_COMPONENTS:
        return "This message has too many parts (pictures, buttons). Remove a few or split it into two messages."
    return None


def block_label(b):
    """Title of a block for lists: the first line of its text without the markdown."""
    if b["type"] == "banner":
        return "Banner image"
    first = next((l for l in b["text"].split("\n") if l.strip()), "Card")
    first = re.sub(r"^[#>\-\s*_`]+", "", first)
    return (re.sub(r"[*_`]", "", first).strip()[:45]) or "Card"


# ---------------------------------------------------------------- showing it

def _image_url(u, attachments):
    """https links as they are; attachment:// references stay as they are when posting (attachments=None) and
    become the real link in previews."""
    if not u:
        return None
    if u.startswith(ATTACHMENT):
        return u if attachments is None else attachments.get(u[len(ATTACHMENT):])
    return u if u.startswith("https://") else None


def render_blocks(blocks, attachments=None):
    """One card per block. `attachments` maps file names to links, for previews of uploaded banners."""
    out = []
    for b in blocks:
        color = b.get("color") if b.get("color") is not None else COLORS["brand"]
        if b["type"] == "banner":
            out.append(card(color, gallery(_image_url(b.get("image"), attachments))))
            continue
        thumb_url = _image_url(b.get("thumb"), attachments)
        buttons = [discord.ui.Button(style=discord.ButtonStyle.link, label=x["label"], url=x["url"])
                   for x in b.get("buttons") or []]
        out.append(card(color,
                        section(b["text"], thumb(thumb_url)) if thumb_url else text(b["text"]),
                        gallery(_image_url(b.get("image"), attachments)),
                        discord.ui.ActionRow(*buttons) if buttons else None))
    return out


# ---------------------------------------------------------------- saved panels (so /panel can edit them later)

def remember_panel(message, guild_id, blocks, author_id):
    store = db.setdefault("panels", {})
    store[str(message.id)] = {"guild_id": guild_id, "channel_id": message.channel.id, "blocks": blocks,
                              "by": author_id, "at": int(time.time())}
    while len(store) > 200:
        store.pop(next(iter(store)))
    save_db(db)


def _blocks_from_saved(rec):
    """Panels saved by older versions had sections + one colour; turn them into blocks."""
    if rec.get("blocks"):
        return [dict(b) for b in rec["blocks"]]
    color = rec.get("color")
    out = []
    if rec.get("banner"):
        out.append({"type": "banner", "image": rec["banner"], "color": color})
    for sec in rec.get("sections") or []:
        title, body = sec.get("title") or "", sec.get("body") or ""
        out.append({"type": "card", "text": f"## {title}\n{body}" if title else body, "color": color, "thumb": None,
                    "image": sec.get("image") or rec.get("divider"), "buttons": []})
    return out


_TITLE_RE = re.compile(r"^## __(.+?)__\n?(.*)$", re.S)


def _blocks_from_embeds(message):
    """The very first panels were classic embeds."""
    out = []
    atts = {a.filename for a in message.attachments}
    for e in message.embeds:
        color = e.color.value if e.color else None
        img = e.image.url if e.image and e.image.url else None
        if img:
            name = img.split("?")[0].rsplit("/", 1)[-1]
            if name in atts:
                img = ATTACHMENT + name
        desc = e.description or ""
        if not desc:
            if img:
                out.append({"type": "banner", "image": img, "color": color})
            continue
        m = _TITLE_RE.match(desc)
        body = f"## {m.group(1)}\n{m.group(2)}" if m else desc
        out.append({"type": "card", "text": body, "color": color, "thumb": None, "image": img, "buttons": []})
    return out


# ---------------------------------------------------------------- the builder

class _Btn(discord.ui.Button):
    def __init__(self, b, action, label, emoji=None, style=discord.ButtonStyle.secondary, row=0, disabled=False):
        super().__init__(label=label, emoji=emoji, style=style, row=row, disabled=disabled)
        self.b, self.action = b, action

    async def callback(self, interaction):
        await self.b.handle(interaction, self.action)


class _Pick(discord.ui.Select):
    def __init__(self, b):
        opts = [discord.SelectOption(label=f"{i + 1}. {block_label(x)}"[:100], value=str(i),
                                     emoji="🖼️" if x["type"] == "banner" else "📄", default=i == b.selected)
                for i, x in enumerate(b.blocks)]
        super().__init__(placeholder="Pick a block to change…", options=opts, row=0)
        self.b = b

    async def callback(self, interaction):
        self.b.selected = int(self.values[0])
        await interaction.response.edit_message(**self.b.render())


def _color_text(b):
    return f"#{b['color']:06x}" if b.get("color") is not None else None


class CardModal(discord.ui.Modal):
    def __init__(self, builder, index):
        super().__init__(title="Edit card" if index is not None else "New card")
        self.builder, self.index = builder, index
        b = builder.blocks[index] if index is not None else None
        g = builder.guild
        self.text_in = discord.ui.TextInput(
            label="Text (## Heading, **bold**, - lists)", style=discord.TextStyle.paragraph, max_length=MAX_TEXT,
            default=readable_mentions(b["text"], g)[:MAX_TEXT] if b else None,
            placeholder="## 📜 Rules\n• Be kind\n• Read #rules first")
        self.color_in = discord.ui.TextInput(
            label=f"Color ({SAMPLE_COLORS})", required=False, max_length=20, default=_color_text(b) if b else None)
        self.thumb_in = discord.ui.TextInput(
            label="Small picture on the right (link)", required=False, max_length=400, default=(b or {}).get("thumb"))
        self.image_in = discord.ui.TextInput(
            label="Big picture at the bottom (link)", required=False, max_length=400, default=(b or {}).get("image"))
        self.buttons_in = discord.ui.TextInput(
            label="Buttons (Label | link, one per line)", style=discord.TextStyle.paragraph, required=False,
            max_length=700,
            default="\n".join(f"{x['label']} | {x['url']}" for x in (b or {}).get("buttons") or []) or None)
        for item in (self.text_in, self.color_in, self.thumb_in, self.image_in, self.buttons_in):
            self.add_item(item)

    async def on_submit(self, interaction):
        bd = self.builder
        color, c_err = parse_color(self.color_in.value)
        small, t_err = parse_image(self.thumb_in.value, "small picture")
        big, i_err = parse_image(self.image_in.value, "big picture")
        buttons, b_err = parse_buttons(self.buttons_in.value)
        body = resolve_mentions((self.text_in.value or "").strip(), bd.guild)  # #rules and @VIP become real links
        err = c_err or ("The card needs some text." if not body else None) or t_err or i_err or b_err
        if err:
            await interaction.response.edit_message(**bd.render(error=err))
            return
        block = {"type": "card", "text": body, "color": color, "thumb": small, "image": big, "buttons": buttons}
        await bd.apply(interaction, block, self.index)


class BannerModal(discord.ui.Modal):
    def __init__(self, builder, index):
        super().__init__(title="Edit banner" if index is not None else "New banner")
        self.builder, self.index = builder, index
        b = builder.blocks[index] if index is not None else None
        shown = (b or {}).get("image")
        self.image_in = discord.ui.TextInput(
            label="Picture link (https://…)", required=False, max_length=400,
            placeholder="https://i.imgur.com/example.png" if not (shown or "").startswith(ATTACHMENT)
            else "Leave empty to keep the uploaded banner",
            default=None if (shown or "").startswith(ATTACHMENT) else shown)
        self.color_in = discord.ui.TextInput(
            label=f"Edge color ({SAMPLE_COLORS})", required=False, max_length=20, default=_color_text(b) if b else None)
        self.add_item(self.image_in)
        self.add_item(self.color_in)

    async def on_submit(self, interaction):
        bd = self.builder
        color, c_err = parse_color(self.color_in.value)
        keep = (self.index is not None and not (self.image_in.value or "").strip()
                and (bd.blocks[self.index].get("image") or "").startswith(ATTACHMENT))
        image, i_err = parse_image(self.image_in.value, "banner picture")
        err = c_err or i_err or (None if (image or keep) else "Paste a link to the banner picture.")
        if err:
            await interaction.response.edit_message(**bd.render(error=err))
            return
        if keep:  # an uploaded banner stays as it is when the link box is left empty
            image = bd.blocks[self.index]["image"]
        await bd.apply(interaction, {"type": "banner", "image": image, "color": color}, self.index)


class PanelBuilder(GuardedView):
    """The private builder: a card listing the blocks, with the controls inside it."""

    def __init__(self, guild, owner_id, target, mode="post", blocks=None, edit_msg=None, banner_att=None):
        super().__init__(timeout=1800)
        self.guild, self.owner_id, self.target = guild, owner_id, target
        self.mode, self.edit_msg, self.banner_att = mode, edit_msg, banner_att
        self.blocks = list(blocks or [])
        self.selected = 0 if self.blocks else None
        self.rebuild()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                **notice("bad", "This builder belongs to someone else. Run `/panel` yourself."), ephemeral=True)
            return False
        return await super().interaction_check(interaction)

    # -- layout
    def rebuild(self):
        self.clear_items()
        if self.blocks:
            self.add_item(_Pick(self))
        sel = self.selected is not None and self.selected < len(self.blocks)
        if sel:
            self.add_item(_Btn(self, "edit", "Edit", "✏️", discord.ButtonStyle.primary, row=1))
            self.add_item(_Btn(self, "up", "Up", "⬆️", row=1, disabled=self.selected == 0))
            self.add_item(_Btn(self, "down", "Down", "⬇️", row=1, disabled=self.selected == len(self.blocks) - 1))
            self.add_item(_Btn(self, "del", "Delete", "🗑️", discord.ButtonStyle.danger, row=1))
        full = len(self.blocks) >= MAX_BLOCKS
        self.add_item(_Btn(self, "addcard", "Add card", "📄", row=2, disabled=full))
        self.add_item(_Btn(self, "addbanner", "Add banner", "🖼️", row=2, disabled=full))
        self.add_item(_Btn(self, "preview", "Preview", "👁️", row=2, disabled=not self.blocks))
        self.add_item(_Btn(self, "finish", "Save changes" if self.mode == "edit" else "Post message",
                           "💾" if self.mode == "edit" else "📨", discord.ButtonStyle.success, row=3,
                           disabled=not self.blocks))
        self.add_item(_Btn(self, "cancel", "Cancel", None, row=3))

    def render(self, error=None, edit=True):
        self.rebuild()
        chars, parts = text_length(self.blocks), component_count(self.blocks)
        heading = (f"-# ✍️ PANEL BUILDER · will be posted in {self.target.mention}" if self.mode == "post"
                   else "-# ✍️ EDITING A PANEL · saving changes the live message")
        listing = [
            f"{'▶️' if i == self.selected else '▫️'} **{i + 1}.** {'🖼️' if b['type'] == 'banner' else '📄'} {block_label(b)}"
            + (f" · {len(b['text']):,} chars" if b["type"] == "card" else "")
            for i, b in enumerate(self.blocks)
        ] or ["*Nothing yet. Add a card or a banner below.*"]
        return msg(card(
            COLORS["brand"],
            text(heading, "### Blocks in this message"),
            text(listing),
            text(f"-# {chars:,} / {MAX_TEXT:,} characters · {parts} / {MAX_COMPONENTS} parts · up to {MAX_BLOCKS} blocks"),
            text(f"❌ {error}") if error else None,
            divider(),
            rows(self),
            footnote("Type #channel or @Role in the text and it becomes a link · up to 3,800 characters per message, "
                     "so post several for long rules")), view=self, edit=edit)

    def _ext(self):
        name = self.banner_att.filename if self.banner_att else "png"
        return name.rsplit(".", 1)[-1].lower() if "." in name else "png"

    def attachments(self):
        """File names -> links, so previews can show uploaded banners."""
        out = {}
        if self.edit_msg is not None:
            out.update({a.filename: a.url for a in self.edit_msg.attachments})
        if self.banner_att is not None:
            out["banner." + self._ext()] = self.banner_att.url
        return out

    # -- actions
    async def apply(self, interaction, block, index):
        nxt = list(self.blocks)
        if index is None:
            nxt.append(block)
        else:
            nxt[index] = block
        why = problem(nxt)
        if why:
            await interaction.response.edit_message(**self.render(error=why))
            return
        self.blocks = nxt
        self.selected = len(nxt) - 1 if index is None else index
        await interaction.response.edit_message(**self.render())

    async def handle(self, interaction, action):
        sel = self.blocks[self.selected] if self.selected is not None and self.selected < len(self.blocks) else None
        if action == "addcard":
            await interaction.response.send_modal(CardModal(self, None))
        elif action == "addbanner":
            await interaction.response.send_modal(BannerModal(self, None))
        elif action == "edit" and sel:
            await interaction.response.send_modal(
                BannerModal(self, self.selected) if sel["type"] == "banner" else CardModal(self, self.selected))
        elif action in ("up", "down") and sel:
            to = self.selected + (-1 if action == "up" else 1)
            if 0 <= to < len(self.blocks):
                self.blocks[self.selected], self.blocks[to] = self.blocks[to], self.blocks[self.selected]
                self.selected = to
            await interaction.response.edit_message(**self.render())
        elif action == "del" and sel:
            self.blocks.pop(self.selected)
            self.selected = min(self.selected, len(self.blocks) - 1) if self.blocks else None
            await interaction.response.edit_message(**self.render())
        elif action == "preview":
            why = problem(self.blocks)
            if why:
                await interaction.response.edit_message(**self.render(error=why))
            else:  # only you see it, exactly as it will be posted
                await interaction.response.send_message(
                    **msg(*render_blocks(self.blocks, self.attachments())), ephemeral=True)
        elif action == "cancel":
            self.stop()
            await interaction.response.edit_message(
                **notice("ok", "Nothing was posted or changed.", title="Cancelled", edit=True))
        elif action == "finish":
            await self.finish(interaction)
        else:
            await interaction.response.edit_message(**self.render())

    async def finish(self, interaction):
        if core.RATE_LIMIT_TRIGGERED:
            await interaction.response.send_message(
                **notice("warn", "Host Assistant is temporarily paused after a Discord rate limit."), ephemeral=True)
            return
        why = problem(self.blocks)
        if why:
            await interaction.response.edit_message(**self.render(error=why))
            return
        await interaction.response.edit_message(**notice("info", "Posting…", edit=True))

        # An uploaded banner is sent along with the message, so its picture never expires.
        file = None
        ref = next((b["image"] for b in self.blocks
                    if (b.get("image") or "").startswith(ATTACHMENT + "banner.")), None)
        if self.banner_att is not None and ref:
            try:
                file = await self.banner_att.to_file(filename=ref[len(ATTACHMENT):])
            except discord.HTTPException:
                await interaction.edit_original_response(
                    **self.render(error="I couldn't download your banner image. Try again."))
                return

        containers = render_blocks(self.blocks)
        try:
            if self.mode == "edit":
                extra = {"attachments": [file]} if file else {}
                sent = await self.edit_msg.edit(**msg(*containers, edit=True), **extra)
            else:
                extra = {"file": file} if file else {}
                sent = await self.target.send(**msg(*containers), **extra)
        except discord.HTTPException as e:
            if getattr(e, "status", None) == 429:
                await emergency_rate_limit_shutdown("/panel")
            print("panel post failed:", e)
            await interaction.edit_original_response(**self.render(
                error="I couldn't post that. Check my permissions there (Send Messages + Embed Links) "
                      "and that your picture links work."))
            return

        remember_panel(sent, self.guild.id, self.blocks, self.owner_id)
        self.stop()
        where = sent.channel.mention
        if self.mode == "edit":
            done = notice("ok", f"The message in {where} was updated.\n{sent.jump_url}", title="Saved", edit=True)
        else:
            done = notice("ok", f"Posted in {where}.\nChange it any time with `/panel`.\n{sent.jump_url}",
                          title="Posted!", edit=True)
        await interaction.edit_original_response(**done)
        try:
            await log_action(self.guild, "Panel posted", f"<@{self.owner_id}> posted a panel in {where}")
        except Exception as e:
            print("panel log failed:", e)


# ---------------------------------------------------------------- the command

async def _panel_autocomplete(interaction: discord.Interaction, current: str):
    """The panels made with /panel in this server, newest first."""
    out = []
    for mid, rec in reversed(list(db.get("panels", {}).items())):
        if rec.get("guild_id") != interaction.guild_id:
            continue
        ch = interaction.guild.get_channel(rec.get("channel_id") or 0)
        blocks = _blocks_from_saved(rec)
        first = next((b for b in blocks if b["type"] == "card"), blocks[0] if blocks else None)
        name = f"#{ch.name if ch else 'channel'} · {block_label(first) if first else 'panel'}"[:100]
        if current.lower() in name.lower() or current in mid:
            out.append(app_commands.Choice(name=name, value=mid))
    return out[:25]


@bot.tree.command(name="panel", description="Write rules, info or announcements from banners and cards, and edit them later")
@app_commands.guild_only()
@app_commands.describe(
    channel="Where to post it (default: this channel)",
    banner="Optional banner image to start with",
    message_id="Edit one of my earlier panels instead: pick it from the list (or paste its message ID)",
)
@app_commands.autocomplete(message_id=_panel_autocomplete)
async def panel_cmd(interaction: discord.Interaction, channel: discord.TextChannel = None,
                    banner: discord.Attachment = None, message_id: str = None):
    if not is_server_admin(interaction.user):
        await interaction.response.send_message(
            **notice("bad", "You need to be a server admin to use this.", title="Not allowed"), ephemeral=True)
        return
    if banner is not None and not (banner.content_type or "").startswith("image/"):
        await interaction.response.send_message(**notice("bad", "The banner has to be an image."), ephemeral=True)
        return

    target = channel or interaction.channel
    if message_id:
        digits = re.sub(r"\D", "", message_id)
        rec = db.get("panels", {}).get(digits)
        if rec and rec.get("guild_id") == interaction.guild_id:
            target = interaction.guild.get_channel(rec["channel_id"]) or target
        try:
            edit_msg = await target.fetch_message(int(digits))
        except (ValueError, discord.HTTPException):
            await interaction.response.send_message(**notice(
                "bad", "Pick it from the list as you type, or paste the message's ID (long-press → Copy ID) "
                       "with the channel set to where it is.", title="Can't find that message"), ephemeral=True)
            return
        if edit_msg.author.id != bot.user.id:
            await interaction.response.send_message(**notice("bad", "I can only edit my own messages."), ephemeral=True)
            return
        blocks = _blocks_from_saved(rec) if rec else _blocks_from_embeds(edit_msg)
        if not blocks:
            await interaction.response.send_message(**notice(
                "bad", "I can only edit messages that were made with `/panel`.", title="Can't edit that"), ephemeral=True)
            return
        builder = PanelBuilder(interaction.guild, interaction.user.id, edit_msg.channel, "edit", blocks, edit_msg, banner)
    else:
        builder = PanelBuilder(interaction.guild, interaction.user.id, target, "post", None, None, banner)
        if banner is not None:
            builder.blocks.append({"type": "banner", "image": f"{ATTACHMENT}banner.{builder._ext()}", "color": None})
            builder.selected = 0
    await interaction.response.send_message(**builder.render(edit=False), ephemeral=True)
