"""/panel - build and post styled multi-box messages from inside Discord (no code editing)."""

import re
import discord
from discord import app_commands

from hostbot import core
from hostbot.core import bot, db, save_db
from hostbot.shared import (
    GuardedView, V2View, _rows_from_view, emergency_rate_limit_shutdown, is_server_admin, log_action,
)

MAX_SECTIONS = 20
DEFAULT_COLOR = 0x0099FF
MAX_CHARS_PER_MESSAGE = 3800   # Components V2: 4000 characters of text per message
MAX_COMPONENTS_PER_MESSAGE = 38  # ...and 40 components
NO_PINGS = discord.AllowedMentions.none()

HELP = (
    "**🛠️ Panel builder**\n"
    "Press **Add section** to write a box, **Style** for color/banner, then **Post**.\n"
    "Tip: type `{#channel-name}` for a channel link and `{@Role}` for a role mention (nobody gets pinged)."
)

_CHANNEL_RE = re.compile(r"\{#([^}]+)\}")
_ROLE_RE = re.compile(r"\{@([^}]+)\}")
_TITLE_RE = re.compile(r"^## __(.+?)__\n?(.*)$", re.S)


# ---------- building the embeds ----------

def _find_by_name(items, wanted):
    """Exact name first, then 'contains' (so {#support} finds '❓｜server-support')."""
    wanted = wanted.strip().lower().lstrip("#")
    for it in items:
        if it.name.lower() == wanted:
            return it
    for it in items:
        if wanted in it.name.lower():
            return it
    return None


def resolve_text(guild, text):
    """Turn {#channel} and {@role} placeholders into real mentions (plain text if not found)."""
    def chan(m):
        c = _find_by_name(guild.channels, m.group(1))
        return c.mention if c else f"#{m.group(1)}"

    def role(m):
        r = _find_by_name(guild.roles, m.group(1))
        return r.mention if r else f"@{m.group(1)}"

    return _ROLE_RE.sub(role, _CHANNEL_RE.sub(chan, text))


def build_panel_blocks(guild, panel):
    """The panel as V2 blocks: the banner (an image gallery) and one colored card per section,
    with the divider image (if any) inside the bottom of each card."""
    color = panel.get("color", DEFAULT_COLOR)
    blocks = []
    if panel.get("banner"):
        blocks.append(discord.ui.MediaGallery(discord.MediaGalleryItem(panel["banner"])))
    for sec in panel.get("sections", []):
        body = resolve_text(guild, sec.get("body", ""))
        title = sec.get("title")
        text = (f"## __{title}__\n{body}" if title else body)[:3800] or "\u200b"
        card = discord.ui.Container(discord.ui.TextDisplay(text), accent_colour=color)
        img = sec.get("image") or panel.get("divider")
        if img:
            card.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(img)))
        blocks.append(card)
    return blocks


def _block_size(block):
    """(characters of text, number of components) for one block."""
    chars, count = 0, 0
    stack = [block]
    while stack:
        b = stack.pop()
        count += 1
        if isinstance(b, discord.ui.TextDisplay):
            chars += len(b.content)
        stack.extend(getattr(b, "children", []) or [])
    return chars, count


def chunk_blocks(blocks, reserve_components=0):
    """Group blocks into messages that fit V2's 4000-character / 40-component limits."""
    groups, cur, chars, count = [], [], 0, 0
    limit = MAX_COMPONENTS_PER_MESSAGE - reserve_components
    for b in blocks:
        c, n = _block_size(b)
        if cur and (chars + c > MAX_CHARS_PER_MESSAGE or count + n > limit):
            groups.append(cur)
            cur, chars, count = [], 0, 0
        cur.append(b)
        chars += c
        count += n
    if cur:
        groups.append(cur)
    return groups


def panel_layout(blocks):
    lay = V2View(timeout=None)
    for b in blocks:
        lay.add_item(b)
    return lay


def remember_panel(message_id, panel):
    """Saved so /panel message_id:... can load a V2 panel again for editing."""
    store = db.setdefault("panels", {})
    store[str(message_id)] = {k: panel.get(k) for k in ("color", "banner", "divider", "sections")}
    while len(store) > 200:
        store.pop(next(iter(store)))
    save_db(db)


def load_from_message(builder, msg):
    """Pre-fill the builder from one of the bot's earlier panel messages (for editing it)."""
    saved = db.get("panels", {}).get(str(msg.id))
    if saved:
        builder.color = saved.get("color") or DEFAULT_COLOR
        builder.banner_url = saved.get("banner")
        builder.divider = saved.get("divider")
        builder.sections = [dict(s) for s in saved.get("sections") or []]
        return
    # Older panels were classic embeds.
    for i, e in enumerate(msg.embeds):
        if i == 0 and e.color:
            builder.color = e.color.value
        desc = e.description or ""
        if not desc:
            if e.image and e.image.url and not builder.sections and not builder.banner_url:
                url = e.image.url
                att = next((a for a in msg.attachments if a.filename and a.filename in url.split("?")[0]), None)
                builder.banner_url = f"attachment://{att.filename}" if att else url
            continue
        m = _TITLE_RE.match(desc)
        sec = {"title": m.group(1), "body": m.group(2)} if m else {"title": "", "body": desc}
        if e.image and e.image.url:
            sec["image"] = e.image.url
        builder.sections.append(sec)


# ---------- the builder UI ----------

class SectionModal(discord.ui.Modal):
    def __init__(self, builder, index=None):
        super().__init__(title="Add section" if index is None else "Edit section")
        self.builder = builder
        self.index = index
        cur = builder.sections[index] if index is not None else {"title": "", "body": ""}
        self.heading_in = discord.ui.TextInput(
            label="Heading (optional)", default=cur["title"][:100], required=False,
            max_length=100, placeholder="📌 Server Information")
        self.body_in = discord.ui.TextInput(
            label="Text (clear both boxes to delete)" if index is not None else "Text",
            # Posted text can be longer than what was typed ({#channel} expands to a mention),
            # and Discord rejects a modal whose default is longer than max_length, so clamp it.
            style=discord.TextStyle.paragraph, default=cur["body"][:4000], required=False,
            max_length=4000, placeholder="Write your message here. **bold**, `code`, > quotes all work.")
        self.add_item(self.heading_in)
        self.add_item(self.body_in)

    async def on_submit(self, interaction: discord.Interaction):
        t = (self.heading_in.value or "").strip()
        b = (self.body_in.value or "").strip()
        sections = self.builder.sections
        if not t and not b:
            if self.index is not None and self.index < len(sections):
                sections.pop(self.index)          # cleared = deleted
        elif self.index is None:
            sections.append({"title": t, "body": b})
        elif self.index < len(sections):
            sections[self.index].update(title=t, body=b)
        await interaction.response.edit_message(**self.builder.render())


class StyleModal(discord.ui.Modal):
    def __init__(self, builder):
        super().__init__(title="Panel style")
        self.builder = builder
        self.color_in = discord.ui.TextInput(
            label="Side-bar color (hex)", default=f"{builder.color:06X}",
            required=False, max_length=7, placeholder="0099FF")
        self.banner_in = discord.ui.TextInput(
            label="Banner image link", default=builder.banner_url or "", required=False,
            max_length=400, placeholder="https://...  (type none to remove)")
        self.divider_in = discord.ui.TextInput(
            label="Divider image link (under each box)", default=builder.divider or "",
            required=False, max_length=400, placeholder="https://...  (optional)")
        for item in (self.color_in, self.banner_in, self.divider_in):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        b = self.builder
        raw = (self.color_in.value or "").strip().lstrip("#")
        banner = (self.banner_in.value or "").strip()
        divider = (self.divider_in.value or "").strip()

        if raw and not re.fullmatch(r"[0-9a-fA-F]{6}", raw):
            await interaction.response.send_message("Color must be 6 hex digits, like `0099FF`.", ephemeral=True)
            return
        for link in (banner, divider):
            if link and link.lower() != "none" and not link.lower().startswith("http"):
                await interaction.response.send_message("Image links must start with `http`.", ephemeral=True)
                return

        if raw:
            b.color = int(raw, 16)
        if banner.lower() == "none":
            b.banner_url = None
            b.banner_att = None
        elif banner:
            b.banner_url = banner
            b.banner_att = None            # a typed link replaces an attached image
        else:
            b.banner_url = None            # cleared (an attached image is kept)
        b.divider = None if divider.lower() in ("", "none") else divider
        await interaction.response.edit_message(**b.render())


class SectionPick(discord.ui.Select):
    def __init__(self, builder):
        opts = []
        for i, s in enumerate(builder.sections):
            text = (s["title"] or s["body"]).replace("\n", " ")
            opts.append(discord.SelectOption(label=f"{i + 1}. {text}"[:100], value=str(i)))
        super().__init__(placeholder="✏️ Edit a section…", options=opts, row=2)
        self.builder = builder

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SectionModal(self.builder, int(self.values[0])))


def _status(text, color=None):
    """A small V2 status card for the builder message."""
    lay = V2View(timeout=None)
    lay.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_colour=color))
    return lay


def _with_banner_link(panel, message):
    """Uploaded banners are remembered as attachment://name (attachment links expire, the reference doesn't)."""
    return dict(panel)


class PanelBuilder(GuardedView):
    def __init__(self, guild, owner_id, target, banner_att=None, edit_msg=None):
        super().__init__(timeout=1800)
        self.guild = guild
        self.owner_id = owner_id
        self.target = target
        self.edit_msg = edit_msg
        self.sections = []
        self.color = DEFAULT_COLOR
        self.banner_url = None
        self.banner_att = banner_att
        self.divider = None
        self._pick = None
        if edit_msg is not None:
            self.btn_post.label = "Update message"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This builder belongs to someone else.", ephemeral=True)
            return False
        return await super().interaction_check(interaction)

    def data(self, banner_override=None, preview=True):
        banner = banner_override or (self.banner_att.url if self.banner_att else self.banner_url)
        if preview and banner and banner.startswith("attachment://") and self.edit_msg is not None:
            # The banner is the edited message's own uploaded image: show its (fresh) link in the preview.
            name = banner[len("attachment://"):]
            att = next((a for a in self.edit_msg.attachments if a.filename == name), None)
            banner = att.url if att else None
        return {"color": self.color, "banner": banner, "divider": self.divider, "sections": self.sections}

    def render(self, note=None, edit=True):
        """The builder as a V2 message: help text, a live preview of the cards, then the controls."""
        if self._pick is not None:
            self.remove_item(self._pick)
            self._pick = None
        if self.sections:
            self._pick = SectionPick(self)
            self.add_item(self._pick)

        rows = _rows_from_view(self)
        groups = chunk_blocks(build_panel_blocks(self.guild, self.data()), reserve_components=len(rows) * 6 + 2)
        status = f"**{len(self.sections)} section(s)**"
        if self.edit_msg is not None:
            status += " · editing an existing message"
        if len(groups) > 1:
            status += f" · posts as {len(groups)} messages, preview shows the first"
        head = (f"❌ {note}\n\n" if note else "") + HELP + "\n\n" + status

        lay = getattr(self, "_v2", None)
        if lay is None:
            lay = V2View(self)
        else:
            lay.clear_items()
        lay.add_item(discord.ui.TextDisplay(head))
        for b in (groups[0] if groups else []):
            lay.add_item(b)
        for r in rows:
            lay.add_item(r)
        kw = {"view": lay}
        if edit:
            kw.update(content=None, embeds=[])
        return kw

    async def _back(self, interaction, note):
        await interaction.edit_original_response(**self.render(note=note))

    @discord.ui.button(label="Add section", emoji="➕", style=discord.ButtonStyle.primary, row=0)
    async def btn_add(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.sections) >= MAX_SECTIONS:
            await interaction.response.send_message(f"Max {MAX_SECTIONS} sections.", ephemeral=True)
            return
        await interaction.response.send_modal(SectionModal(self))

    @discord.ui.button(label="Style", emoji="🎨", style=discord.ButtonStyle.secondary, row=0)
    async def btn_style(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StyleModal(self))

    @discord.ui.button(label="Undo last", emoji="↩️", style=discord.ButtonStyle.secondary, row=0)
    async def btn_undo(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.sections:
            self.sections.pop()
        await interaction.response.edit_message(**self.render())

    @discord.ui.button(label="Post", emoji="🚀", style=discord.ButtonStyle.success, row=1)
    async def btn_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        if core.RATE_LIMIT_TRIGGERED:
            await interaction.response.send_message(
                "⚠️ Host Assistant is temporarily paused after a Discord rate limit.", ephemeral=True)
            return
        if not self.sections:
            await interaction.response.send_message("Add at least one section first.", ephemeral=True)
            return

        await interaction.response.edit_message(view=_status("⏳ Posting…"), content=None, embeds=[])

        file, banner_ref = None, None
        if self.banner_att:
            name = self.banner_att.filename
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else "png"
            try:
                file = await self.banner_att.to_file(filename=f"banner.{ext}")
            except discord.HTTPException:
                await self._back(interaction, "I couldn't download your banner image. Try again.")
                return
            banner_ref = f"attachment://banner.{ext}"

        groups = chunk_blocks(build_panel_blocks(self.guild, self.data(banner_ref, preview=False)))
        saved = self.data(banner_ref, preview=False)  # remembered for editing later

        try:
            if self.edit_msg is not None:
                if len(groups) > 1:
                    await self._back(interaction, "Too long to edit in place. Shorten it or post a new one.")
                    return
                kwargs = {"attachments": [file]} if file else {}
                edited = await self.edit_msg.edit(content=None, embeds=[], view=panel_layout(groups[0]),
                                                  allowed_mentions=NO_PINGS, **kwargs)
                remember_panel(self.edit_msg.id, _with_banner_link(saved, edited))
                result = f"✅ Updated: {self.edit_msg.jump_url}"
            else:
                first = None
                for i, grp in enumerate(groups):
                    kwargs = {"file": file} if (file and i == 0) else {}
                    sent = await self.target.send(view=panel_layout(grp), allowed_mentions=NO_PINGS, **kwargs)
                    if i == 0:
                        remember_panel(sent.id, _with_banner_link(saved, sent))
                    first = first or sent
                result = f"✅ Posted in {self.target.mention}: {first.jump_url}"
        except discord.HTTPException as e:
            if getattr(e, "status", None) == 429:
                await emergency_rate_limit_shutdown("/panel")
            print("panel post failed:", e)
            await self._back(interaction,
                             "I couldn't post that. Check my permissions there (Send Messages + Embed Links).")
            return

        self.stop()
        await interaction.edit_original_response(view=_status(result, 0x2ECC71), content=None, embeds=[])
        try:
            await log_action(self.guild, "Panel posted",
                             f"<@{self.owner_id}> posted a panel in {self.target.mention}")
        except Exception as e:
            print("panel log failed:", e)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def btn_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(view=_status("Cancelled.", 0x95A5A6), content=None, embeds=[])


@bot.tree.command(name="panel", description="Build and post a styled multi-box message (info, rules, announcements)")
@app_commands.guild_only()
@app_commands.describe(
    channel="Where to post it (default: this channel)",
    banner="Optional banner image shown at the top",
    message_id="Edit one of my earlier panel messages instead of posting a new one",
)
async def panel_cmd(interaction: discord.Interaction, channel: discord.TextChannel = None,
                    banner: discord.Attachment = None, message_id: str = None):
    if not is_server_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    if banner is not None and not (banner.content_type or "").startswith("image/"):
        await interaction.response.send_message("The banner has to be an image.", ephemeral=True)
        return

    target = channel or interaction.channel
    edit_msg = None
    if message_id:
        try:
            edit_msg = await target.fetch_message(int(message_id))
        except (ValueError, discord.HTTPException):
            await interaction.response.send_message(
                "I couldn't find that message in that channel. Use the message's ID (long-press → Copy ID).",
                ephemeral=True)
            return
        if edit_msg.author.id != bot.user.id:
            await interaction.response.send_message("I can only edit my own messages.", ephemeral=True)
            return

    builder = PanelBuilder(interaction.guild, interaction.user.id, target, banner, edit_msg)
    if edit_msg is not None:
        load_from_message(builder, edit_msg)
    await interaction.response.send_message(**builder.render(edit=False), ephemeral=True)
