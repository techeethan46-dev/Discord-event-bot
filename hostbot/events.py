"""Event features: buttons, claim/swap/rate modals, host panel, /event, /set_superpowers, /blacklist, /trust, /trusted_list."""

import difflib
import discord
import time
from discord import app_commands

from hostbot.core import (
    ALLOWED_USER_IDS, BOT_OWNER_IDS, bot, db, disabled_guild_response, get_config, has_role_id,
    save_db, swap_cooldowns,
)
from hostbot.hosts import (
    check_and_announce_achievements, check_host, trial_state,
)
from hostbot.shared import (
    DIV, GuardedView, add_event_role, build_embed, event_in_guild, event_log,
    find_active_event, find_event, is_authorized_manager, is_server_admin, is_vip,
    new_event_key, parse_time, process_waitlist, refresh_event_message, remove_event_role,
)

class RateModal(discord.ui.Modal, title="Rate Event"):
    rating = discord.ui.TextInput(label="Rating (1-5)", placeholder="e.g. 4.5", max_length=4)

    def __init__(self, host_id, key):
        super().__init__()
        self.host_id = str(host_id)
        self.key = key

    async def on_submit(self, interaction):
        if await disabled_guild_response(interaction):
            return
        info = db["events"].get(self.key)
        if not info:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return

        if time.time() - info.get("concluded_at", 0) > 3600:
            await interaction.response.send_message("Rating window closed.", ephemeral=True)
            return

        if interaction.user.id == info.get("host_id"):
            await interaction.response.send_message("You can't rate your own event.", ephemeral=True)
            return

        # Only players who actually joined can rate (events concluded before this
        # update have no participant list, so they stay open to everyone).
        participants = info.get("participants")
        if participants is not None and interaction.user.id not in participants:
            await interaction.response.send_message("Only players who joined this event can rate it.", ephemeral=True)
            return

        if "voted" not in info: 
            info["voted"] = []

        if interaction.user.id in info["voted"]:
            await interaction.response.send_message("You already voted.", ephemeral=True)
            return

        try:
            val = float(self.rating.value)
            if not (1.0 <= val <= 5.0): raise ValueError
        except ValueError:
            await interaction.response.send_message("Invalid rating.", ephemeral=True)
            return

        info["voted"].append(interaction.user.id)
        check_host(self.host_id)
        db["hosts"][self.host_id]["ratings"].append(val)
        db["hosts"][self.host_id].setdefault("rating_history", []).append({
            "rating": val,
            "event_key": self.key,
            "timestamp": int(time.time()),
            "rater_id": interaction.user.id,
        })
        save_db(db)

        await interaction.response.send_message(f"Rated **{val}/5**!", ephemeral=True)
        stats_id = get_config(interaction.guild_id).get("stats_channel")
        stats_channel = interaction.guild.get_channel(stats_id) if stats_id else None
        await check_and_announce_achievements(interaction.guild, self.host_id, stats_channel)

def reservation_access_lead_seconds(member, guild_id):
    """Return how early this member may reserve: Double Booster 7m, VIP 5m, otherwise 0."""
    if member is None:
        return 0
    if member.id in BOT_OWNER_IDS:
        return 7 * 60
    gp = getattr(member, "guild_permissions", None)
    if gp and gp.administrator:
        return 7 * 60
    double_booster_role = get_config(guild_id).get("double_booster_role")
    if has_role_id(member, double_booster_role):
        return 7 * 60
    vip_role = get_config(guild_id).get("superpower_role")
    if has_role_id(member, vip_role):
        return 5 * 60
    return 0


class ClaimModal(discord.ui.Modal, title="Claim Spot"):
    country = discord.ui.TextInput(label="Nation", required=True, max_length=40)
    roblox = discord.ui.TextInput(label="Roblox Username", required=True, max_length=40)

    def __init__(self, key):
        super().__init__()
        self.key = key

    async def on_submit(self, interaction):
        if await disabled_guild_response(interaction):
            return
        info = db["events"].get(self.key)
        if not info or info.get("concluded"):
            await interaction.response.send_message("Event is over.", ephemeral=True)
            return

        now = time.time()
        lead = reservation_access_lead_seconds(interaction.user, interaction.guild_id)
        earliest = info["start_time"] - lead
        if now < earliest:
            wait = max(1, int(earliest - now))
            mins, secs = divmod(wait, 60)
            if mins:
                text = f"⏳ Reservations open for you in **{mins}m {secs}s**."
            else:
                text = f"⏳ Reservations open for you in **{secs}s**."
            await interaction.response.send_message(text, ephemeral=True)
            return

        if now >= info["end_time"]:
            await interaction.response.send_message("Closed.", ephemeral=True)
            return

        for c_name, u in info["reservations"].items():
            if u["discord_id"] == interaction.user.id:
                await interaction.response.send_message("You already claimed a spot.", ephemeral=True)
                return

        c_title = " ".join(self.country.value.split()).title()
        r_user = self.roblox.value.strip()
        if not c_title or not r_user:
            await interaction.response.send_message("Nation and Roblox username can't be blank.", ephemeral=True)
            return

        if interaction.user.id in db["blacklists"].get(str(interaction.guild_id), []):
            await interaction.response.send_message("You are blacklisted.", ephemeral=True)
            return

        if "waitlist" not in info: 
            info["waitlist"] = []

        max_p = info.get("max_players", 0)

        if c_title in info["reservations"] or (max_p > 0 and len(info["reservations"]) >= max_p):
            for w in info["waitlist"]:
                if w["discord_id"] == interaction.user.id:
                    await interaction.response.send_message("Already on waitlist.", ephemeral=True)
                    return

            info["waitlist"].append({"discord_id": interaction.user.id, "roblox": r_user, "country": c_title})
            # Waitlisted users are not confirmed participants and must not hold the event role.
            await remove_event_role(interaction.guild, interaction.user.id)
            save_db(db)

            # Answer first: Discord only allows 3 seconds before the first reply.
            await interaction.response.send_message(f"Spot taken/full! Added to waitlist for {c_title}.", ephemeral=True)
            await refresh_event_message(info, interaction.guild)
            await event_log(interaction.guild, info, "waitlist", f"<@{interaction.user.id}> joined the waitlist for **{c_title}**")
            return

        matches = difflib.get_close_matches(c_title, list(info["reservations"].keys()), cutoff=0.88)
        if matches and matches[0].lower() != c_title.lower():
            await interaction.response.send_message(f"Name too similar to {matches[0]}.", ephemeral=True)
            return

        sps = [s.title() for s in info.get("superpowers", [])]
        if c_title in sps and not is_vip(interaction.user, interaction.guild_id):
            await interaction.response.send_message("That nation is a VIP Superpower.", ephemeral=True)
            return

        info["reservations"][c_title] = {"roblox": r_user, "discord_id": interaction.user.id}
        # They have a spot now, so any waitlist entry of theirs must go (no double spots).
        info["waitlist"] = [w for w in info["waitlist"] if w["discord_id"] != interaction.user.id]
        save_db(db)

        await interaction.response.send_message(f"Reserved **{c_title}**!", ephemeral=True)
        await add_event_role(interaction.guild, interaction.user.id)
        await refresh_event_message(info, interaction.guild)
        await event_log(interaction.guild, info, "reservation", f"<@{interaction.user.id}> reserved **{c_title}**")

class SwapModal(discord.ui.Modal, title="Propose Swap"):
    target = discord.ui.TextInput(label="Nation You Want", required=True, max_length=40)

    def __init__(self, key):
        super().__init__()
        self.key = key

    async def on_submit(self, interaction):
        if await disabled_guild_response(interaction):
            return
        info = db["events"].get(self.key)
        if not info or info.get("concluded"):
            await interaction.response.send_message("Event is over.", ephemeral=True)
            return

        t_c = " ".join(self.target.value.split()).title()
        u_c = next((c for c, u in info["reservations"].items() if u["discord_id"] == interaction.user.id), None)

        if not u_c: 
            await interaction.response.send_message("You don't have a spot.", ephemeral=True)
            return

        if t_c not in info["reservations"]: 
            await interaction.response.send_message("No one has that spot.", ephemeral=True)
            return

        if t_c == u_c: 
            await interaction.response.send_message("Can't swap with yourself.", ephemeral=True)
            return

        sps = [s.title() for s in info.get("superpowers", [])]
        if t_c in sps and not is_vip(interaction.user, interaction.guild_id):
            await interaction.response.send_message("That nation is a VIP Superpower.", ephemeral=True)
            return

        t_id = info["reservations"][t_c]["discord_id"]
        ch_id = get_config(interaction.guild_id).get("swap_channel")

        if not ch_id: 
            await interaction.response.send_message("Swap channel not set up.", ephemeral=True)
            return

        swap_ch = interaction.guild.get_channel(ch_id)
        if not swap_ch: 
            await interaction.response.send_message("Swap channel missing.", ephemeral=True)
            return

        emb = discord.Embed(title="🔄 Swap Request", description=f"<@{interaction.user.id}> wants to swap with <@{t_id}>!\n\nGives: **{u_c}**\nGets: **{t_c}**", color=0xF1C40F)
        view = SwapAcceptView(self.key, interaction.user.id, t_id, u_c, t_c)
        try:
            view.message = await swap_ch.send(content=f"<@{t_id}>", embed=emb, view=view)
        except discord.HTTPException as e:
            print("swap post failed:", e)
            await interaction.response.send_message("I can't post in the swap channel (check my permissions).", ephemeral=True)
            return
        swap_cooldowns[interaction.user.id] = time.time()
        await interaction.response.send_message(f"Proposal sent in <#{ch_id}>!", ephemeral=True)
        await event_log(interaction.guild, info, "swap", f"<@{interaction.user.id}> proposed a swap with <@{t_id}> ({u_c} ↔ {t_c})")

class SwapAcceptView(GuardedView):
    def __init__(self, key, s_id, t_id, s_c, t_c):
        super().__init__(timeout=300)
        self.key = key
        self.s_id = s_id
        self.t_id = t_id
        self.s_c = s_c
        self.t_c = t_c
        self.message = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message and self.message.embeds:
            emb = self.message.embeds[0]
            emb.color = 0x95A5A6
            emb.title = "⌛ Swap Expired"
            try:
                await self.message.edit(embed=emb, view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction, btn):
        if interaction.user.id != self.t_id:
            await interaction.response.send_message("Not your swap offer.", ephemeral=True)
            return

        info = db["events"].get(self.key)
        if not info or info.get("concluded"):
            await interaction.response.send_message("Event is over.", ephemeral=True)
            return

        res = info["reservations"]

        if self.s_c not in res or self.t_c not in res or res[self.s_c]["discord_id"] != self.s_id or res[self.t_c]["discord_id"] != self.t_id:
            await interaction.response.send_message("Swap invalid now.", ephemeral=True)
            return

        # Re-check VIP rules for BOTH sides at accept time.
        sps = [s.title() for s in info.get("superpowers", [])]
        s_mem = interaction.guild.get_member(self.s_id)
        if (self.t_c in sps and not is_vip(s_mem, interaction.guild_id)) or \
           (self.s_c in sps and not is_vip(interaction.user, interaction.guild_id)):
            await interaction.response.send_message("Swap blocked: a VIP Superpower can only be held by VIPs.", ephemeral=True)
            return

        res[self.s_c], res[self.t_c] = res[self.t_c], res[self.s_c]
        info["swaps_completed"] = int(info.get("swaps_completed", 0)) + 1
        save_db(db)

        for child in self.children: 
            child.disabled = True

        emb = interaction.message.embeds[0]
        emb.color = 0x2ECC71
        emb.title = "✅ Swap Completed"
        await interaction.response.edit_message(embed=emb, view=self)
        self.stop()
        await refresh_event_message(info, interaction.guild)
        await event_log(interaction.guild, info, "swap", f"<@{self.s_id}> and <@{self.t_id}> swapped **{self.s_c}** ↔ **{self.t_c}**")

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction, btn):
        if interaction.user.id != self.t_id: 
            await interaction.response.send_message("Not your offer.", ephemeral=True)
            return

        for child in self.children: 
            child.disabled = True

        emb = interaction.message.embeds[0]
        emb.color = 0xE74C3C
        emb.title = "❌ Swap Declined"
        await interaction.response.edit_message(embed=emb, view=self)
        self.stop()
        info = db["events"].get(self.key)
        if info:
            await event_log(interaction.guild, info, "swap", f"<@{self.t_id}> declined a swap from <@{self.s_id}>")

class KickSelect(discord.ui.Select):
    def __init__(self, key, res):
        self.key = key
        opts = [discord.SelectOption(label=c, value=c) for c in list(res.keys())[:25]]
        super().__init__(placeholder="Select player to kick...", options=opts)

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        info = db["events"].get(self.key)
        c_title = self.values[0]

        if not info or c_title not in info["reservations"]:
            await interaction.followup.send("That spot is already gone.", ephemeral=True)
            return
        if info.get("concluded"):
            await interaction.followup.send("This event has already concluded.", ephemeral=True)
            return

        uid = info["reservations"][c_title]["discord_id"]
        del info["reservations"][c_title]

        await remove_event_role(interaction.guild, uid)

        ch = bot.get_channel(info["channel_id"])
        await process_waitlist(info, c_title, interaction.guild, ch)

        save_db(db)
        await refresh_event_message(info, interaction.guild)
        await event_log(interaction.guild, info, "host", f"<@{interaction.user.id}> removed **{c_title}** (<@{uid}>)")
        await interaction.followup.send(f"Kicked **{c_title}**.", ephemeral=True)

class RateBtnView(GuardedView):
    """Persistent (survives restarts): the event is looked up from the stats message the button is on."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Rate Host", style=discord.ButtonStyle.success, custom_id="rate_host_btn")
    async def rate(self, i, b):
        found = next(((k, e) for k, e in db["events"].items() if e.get("rate_msg") == i.message.id), None)
        if not found:
            await i.response.send_message("This rating is no longer available.", ephemeral=True)
            return
        key, info = found
        await i.response.send_modal(RateModal(info["host_id"], key))

class HelperSelect(discord.ui.UserSelect):
    def __init__(self, key):
        super().__init__(placeholder="Pick the people who helped (replaces the list)", min_values=0, max_values=25, row=2)
        self.key = key

    async def callback(self, interaction):
        info = db["events"].get(self.key)
        if not info:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return
        ids = [u.id for u in self.values if not getattr(u, "bot", False)]
        info["helpers"] = ids
        save_db(db)
        names = ", ".join(f"<@{i}>" for i in ids)
        await interaction.response.send_message(f"Helpers saved: {names}" if ids else "Helpers cleared.", ephemeral=True)
        await refresh_event_message(info, interaction.guild)
        await event_log(interaction.guild, info, "host", f"<@{interaction.user.id}> set helpers: {names or 'none'}")

class HostActionsView(GuardedView):
    def __init__(self, key):
        super().__init__(timeout=300)
        self.key = key
        info = db["events"].get(key)
        if info and info["reservations"]:
            self.add_item(KickSelect(key, info["reservations"]))
        self.add_item(HelperSelect(key))

    async def interaction_check(self, interaction):
        if not await super().interaction_check(interaction):
            return False
        if is_authorized_manager(interaction.user):
            return True
        await interaction.response.send_message("Admins or managers only.", ephemeral=True)
        return False

    @discord.ui.button(label="Conclude Event", style=discord.ButtonStyle.danger, row=1)
    async def conclude(self, interaction, btn):
        await interaction.response.defer(ephemeral=True)
        info = db["events"].get(self.key)

        if not info:
            await interaction.followup.send("Event not found.", ephemeral=True)
            return

        if info.get("concluded"): 
            await interaction.followup.send("Already ended.", ephemeral=True)
            return

        now = int(time.time())
        info["concluded"] = True
        info["concluded_at"] = now
        # Remember who played, so only they can rate the host.
        info["participants"] = [u["discord_id"] for u in info["reservations"].values()]

        # The Event Participant role stays for the 1-hour rating window; the background timer
        # in lifecycle.py removes it afterwards.
        info["role_clear_at"] = now + 3600
        info["role_cleared"] = False

        await refresh_event_message(info, interaction.guild, remove_view=True)

        # Time the event ran live after reservations closed (unchanged behaviour).
        dur = round(max(0, now - info["end_time"]) / 3600, 1)
        p_count = len(info["reservations"])
        hid = check_host(info["host_id"])

        # Persist a historical snapshot so analytics and achievements can be rebuilt
        # even when the live event embed is long gone.
        info["player_count"] = p_count
        info["duration_seconds"] = max(0, now - info["start_time"])
        info["completed"] = True
        info["cancelled"] = False
        info.setdefault("swaps_completed", 0)
        info.setdefault("waitlist_fills", 0)

        db["hosts"][hid]["total_events"] += 1
        db["hosts"][hid]["total_players"] += p_count
        save_db(db)

        s_id = get_config(interaction.guild_id).get("stats_channel")
        s_ch = interaction.guild.get_channel(s_id) if s_id else None
        if s_ch:
            emb = discord.Embed(description=f"## 📊 Event Results\n{DIV}", color=0xF1C40F)
            emb.add_field(name="Event", value=f"**{info['display_name']}**", inline=False)
            emb.add_field(name="Host", value=f"<@{info['host_id']}>", inline=True)
            emb.add_field(name="Duration", value=f"{dur} Hours", inline=True)
            emb.add_field(name="Players", value=f"{p_count}", inline=True)
            if info.get("helpers"):
                emb.add_field(name="🤝 Helpers", value=", ".join(f"<@{i}>" for i in info["helpers"]), inline=False)
            try:
                stats_msg = await s_ch.send(embed=emb, view=RateBtnView())
                info["rate_msg"] = stats_msg.id
                save_db(db)
            except discord.HTTPException as e:
                print("stats post failed:", e)

        await check_and_announce_achievements(interaction.guild, info["host_id"], s_ch)
        await interaction.followup.send("Event concluded and stats posted.", ephemeral=True)
        await event_log(interaction.guild, info, "host", f"<@{interaction.user.id}> concluded the event · {p_count} players · {dur}h live")
        if info.get("helpers"):
            await event_log(interaction.guild, info, "host", "🤝 Helpers: " + ", ".join(f"<@{i}>" for i in info["helpers"]))

class EventButtonsView(GuardedView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim Spot", style=discord.ButtonStyle.success, custom_id="claim_btn")
    async def claim(self, interaction, btn):
        key, _ = find_event(interaction.message.id)
        if not key:
            await interaction.response.send_message("This event no longer exists.", ephemeral=True)
            return
        await interaction.response.send_modal(ClaimModal(key))

    @discord.ui.button(label="Swap Nation", style=discord.ButtonStyle.primary, custom_id="swap_btn")
    async def swap(self, interaction, btn):
        key, _ = find_event(interaction.message.id)
        if not key:
            await interaction.response.send_message("This event no longer exists.", ephemeral=True)
            return

        rem = 120 - (time.time() - swap_cooldowns.get(interaction.user.id, 0))
        if rem > 0:
            await interaction.response.send_message(f"Cooldown active. Wait {int(rem)}s.", ephemeral=True)
            return

        await interaction.response.send_modal(SwapModal(key))

    @discord.ui.button(label="Leave Event", style=discord.ButtonStyle.danger, custom_id="leave_btn")
    async def leave(self, interaction, btn):
        await interaction.response.defer(ephemeral=True)
        key, info = find_event(interaction.message.id)
        if not key:
            await interaction.followup.send("This event no longer exists.", ephemeral=True)
            return

        tgt = next((c for c, u in info["reservations"].items() if u["discord_id"] == interaction.user.id), None)
        if tgt:
            del info["reservations"][tgt]
            await remove_event_role(interaction.guild, interaction.user.id)

            ch = bot.get_channel(info["channel_id"])
            await process_waitlist(info, tgt, interaction.guild, ch)

            save_db(db)
            await refresh_event_message(info, interaction.guild)
            await interaction.followup.send("Spot removed.", ephemeral=True)
            await event_log(interaction.guild, info, "reservation", f"<@{interaction.user.id}> left **{tgt}**")
            return

        wait = info.get("waitlist", [])
        w_ent = next((w for w in wait if w["discord_id"] == interaction.user.id), None)
        if w_ent:
            wait.remove(w_ent)
            # Defensive cleanup: waitlisted users should never keep the event role.
            await remove_event_role(interaction.guild, interaction.user.id)
            save_db(db)
            await refresh_event_message(info, interaction.guild)
            await interaction.followup.send("Removed from waitlist.", ephemeral=True)
            await event_log(interaction.guild, info, "waitlist", f"<@{interaction.user.id}> left the waitlist")
            return

        await interaction.followup.send("You don't have a spot here.", ephemeral=True)

    @discord.ui.button(label="Host Actions", style=discord.ButtonStyle.secondary, custom_id="host_btn")
    async def host_menu(self, interaction, btn):
        key, _ = find_event(interaction.message.id)
        if not key:
            await interaction.response.send_message("This event no longer exists.", ephemeral=True)
            return
        if not is_authorized_manager(interaction.user):
            await interaction.response.send_message("Admins or managers only.", ephemeral=True)
            return

        await interaction.response.send_message("Host Panel:", view=HostActionsView(key), ephemeral=True)

async def ev_autocomplete(interaction: discord.Interaction, curr: str):
    ch = []
    for k, v in db["events"].items():
        if v.get("concluded") or not event_in_guild(v, interaction.guild_id):
            continue
        if curr.lower() in v["display_name"].lower():
            ch.append(app_commands.Choice(name=v["display_name"][:100], value=v["display_name"][:100]))
    return ch[:25]

@bot.tree.command(name="event", description="Create event banner")
@app_commands.guild_only()
async def create_ev(interaction: discord.Interaction, name: str, starts_in: str, duration: str, target: discord.TextChannel, max_p: int):
    await interaction.response.defer(ephemeral=True)

    if not is_authorized_manager(interaction.user):
        await interaction.followup.send("Managers only.", ephemeral=True)
        return

    name = " ".join(name.split())[:100]
    if not name:
        await interaction.followup.send("Give the event a name.", ephemeral=True)
        return

    _, existing = find_active_event(name, interaction.guild_id)
    if existing:
        await interaction.followup.send("An active event with that name already exists.", ephemeral=True)
        return

    s_off = parse_time(starts_in)
    d_sec = parse_time(duration)
    if s_off is None or d_sec is None:
        await interaction.followup.send(
            "I couldn't read that time. Use things like `10m`, `1h30m`, `2d` (or `now` for the start).", ephemeral=True)
        return
    if d_sec <= 0:
        d_sec = 1800
    max_p = max(0, max_p)

    key = new_event_key(name)
    s_ts = int(time.time()) + s_off
    e_ts = s_ts + d_sec

    db["events"][key] = {
        "display_name": name, "start_time": s_ts, "end_time": e_ts,
        "host_id": interaction.user.id, "channel_id": target.id, "guild_id": interaction.guild_id,
        "message_ids": [], "superpowers": [], "reservations": {}, "waitlist": [],
        "voted": [], "max_players": max_p, "concluded": False, "completed": False,
        "cancelled": False, "player_count": 0, "swaps_completed": 0, "waitlist_fills": 0,
        "helpers": [], "logs": []
    }

    st = trial_state(interaction.guild_id, interaction.user.id)
    db["events"][key]["trial"] = bool(st and st.get("status") == "trial")

    embs = build_embed(name, db["events"][key], interaction.guild)
    try:
        msg = await target.send(embeds=embs, view=EventButtonsView())
    except discord.HTTPException as e:
        del db["events"][key]
        print("event post failed:", e)
        await interaction.followup.send("I can't post in that channel (check my permissions).", ephemeral=True)
        return

    db["events"][key]["message_ids"] = [msg.id]
    save_db(db)

    await interaction.followup.send(f"Event created in {target.mention}.", ephemeral=True)
    await event_log(interaction.guild, db["events"][key], "host", f"Event created by <@{interaction.user.id}>")

@bot.tree.command(name="set_superpowers", description="Set superpowers")
@app_commands.guild_only()
@app_commands.autocomplete(event_name=ev_autocomplete)
async def set_sp(interaction: discord.Interaction, event_name: str, nations: str):
    if not is_authorized_manager(interaction.user): 
        await interaction.response.send_message("Managers only.", ephemeral=True)
        return

    key, info = find_active_event(event_name, interaction.guild_id)
    if not info:
        await interaction.response.send_message("Event not found.", ephemeral=True)
        return

    sps = [n.strip().title() for n in nations.split(",") if n.strip()]
    info["superpowers"] = sps
    save_db(db)

    await interaction.response.send_message(f"Superpowers set: {', '.join(sps)}", ephemeral=True)
    await refresh_event_message(info, interaction.guild)
    await event_log(interaction.guild, info, "host", f"<@{interaction.user.id}> set superpowers: {', '.join(sps)}")

@bot.tree.command(name="blacklist", description="Blacklist user")
@app_commands.guild_only()
async def blacklist(interaction: discord.Interaction, user: discord.Member):
    if not is_server_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return

    gid = str(interaction.guild_id)
    if gid not in db["blacklists"]: 
        db["blacklists"][gid] = []

    if user.id in db["blacklists"][gid]:
        db["blacklists"][gid].remove(user.id)
        msg = f"Unblacklisted {user.mention}."
    else:
        db["blacklists"][gid].append(user.id)
        msg = f"Blacklisted {user.mention}."

    save_db(db)
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="trust", description="Owner only: add/remove someone from the bot's admin list in THIS server")
@app_commands.guild_only()
async def trust_cmd(interaction: discord.Interaction, user: discord.User):
    if interaction.user.id not in BOT_OWNER_IDS:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    lst = db["trusted_users"].setdefault(str(interaction.guild_id), [])
    if user.id in lst:
        lst.remove(user.id)
        text = f"Removed {user.mention} from the admin list in this server."
    else:
        lst.append(user.id)
        text = f"Added {user.mention} to the admin list in this server."
    save_db(db)
    await interaction.response.send_message(text, ephemeral=True)

@bot.tree.command(name="trusted_list", description="Owner only: show who can use admin actions in this server")
@app_commands.guild_only()
async def trusted_list_cmd(interaction: discord.Interaction):
    if interaction.user.id not in BOT_OWNER_IDS:
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return
    here = db["trusted_users"].get(str(interaction.guild_id), [])
    fixed = list(dict.fromkeys(BOT_OWNER_IDS + ALLOWED_USER_IDS))
    text = ("**Everywhere (hardcoded):** " + (", ".join(f"<@{i}>" for i in fixed) or "none") +
            "\n**This server only:** " + (", ".join(f"<@{i}>" for i in here) or "none") +
            "\nServer admins can always use it too.")
    await interaction.response.send_message(text, ephemeral=True)
