import asyncio
import io
import json
import os
import re
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN", "TOKEN_HERE")

CREDIT = "Made by Sb9c"
FOOTER_ICON = None            # optional: link εικόνας για το footer
THUMB = "https://i.imgur.com/YYYYYYY.png"
COLOR = 0x2B2D31
GREEN, RED, ORANGE = 0x23A55A, 0xDA373C, 0xF0B232

TICKET_TITLE = "Ticket Center"
TICKET_DESC = ("Για την άμεση εξυπηρέτησή σας μπορείτε να ανοίξετε ένα ticket "
               "ώστε να μιλήσετε με κάποιον ανώτερο και να λύσετε το πρόβλημά σας.")
CATEGORY_ID = 0               # ID κατηγορίας για τα tickets (0 = καμία)
DM_USER = True                # στέλνει transcript στον χρήστη όταν κλείνει το ticket
DATA_FILE = os.getenv("DATA_FILE", "data.json")

# label, emoji (μπορείς να βάλεις custom <:όνομα:ID>), custom_id, είδος
TICKET_TYPES = [
    ("Support", "📞", "tk_support", "support"),
    ("Speak with Owner", "👑", "tk_owner", "owner"),
    ("Report Mafia Member", "🛡️", "tk_report", "report"),
]

LOG_TYPES = {
    "bans": "Bans",
    "kicks": "Kicks",
    "timeouts": "Timeouts",
    "voice": "Move / Disconnect",
    "applications": "Applications",
    "tickets": "Ticket Transcripts",
}

APPLICATION_QUESTIONS = [
    ("Πόσες ώρες έχεις στο FiveM;", discord.TextStyle.short),
    ("Έχεις ξαναμπεί σε mafia στο RP;", discord.TextStyle.paragraph),
    ("Τι σημαίνει ιεραρχία για σένα;", discord.TextStyle.paragraph),
    ("Πόσες ώρες active καθημερινά;", discord.TextStyle.short),
]

VIEW = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                   attach_files=True, read_message_history=True)


# ------------------------------------------------------------ data / helpers
def load():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save(d):
    folder = os.path.dirname(DATA_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)


def log_cfg(guild_id):
    return load().get(str(guild_id), {}).get("logs", {})


def set_log(guild_id, key, channel_id):
    d = load()
    d.setdefault(str(guild_id), {}).setdefault("logs", {})[key] = channel_id
    save(d)


def get_log(guild, key):
    cid = log_cfg(guild.id).get(key)
    return guild.get_channel(cid) if cid else None


async def send_log(guild, key, embed, file=None):
    ch = get_log(guild, key)
    if ch:
        try:
            await ch.send(embed=embed, file=file)
        except discord.HTTPException:
            pass


def base_embed(title=None, desc=None, color=COLOR, ts=True):
    e = discord.Embed(title=title, description=desc, color=color,
                      timestamp=discord.utils.utcnow() if ts else None)
    e.set_footer(text=CREDIT, icon_url=FOOTER_ICON)
    return e


def is_admin(m):
    return m.guild_permissions.administrator


def admin(fn):
    fn = app_commands.default_permissions(administrator=True)(fn)
    return app_commands.guild_only()(fn)


def hierarchy_ok(i, member):
    if member.id == i.guild.owner_id:
        return False
    if i.user.id == i.guild.owner_id:
        return True
    return i.user.top_role > member.top_role


async def dm(user, text):
    try:
        await user.send(text)
    except discord.HTTPException:
        pass


def parse_duration(s):
    m = re.fullmatch(r"(\d+)\s*([smhdw])", s.strip().lower())
    if not m:
        return None
    secs = int(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m[2]]
    return timedelta(seconds=secs) if 0 < secs <= 28 * 86400 else None


def mod_embed(title, target, mod, reason, color, extra=None):
    e = base_embed(title, color=color)
    e.add_field(name="Χρήστης", value=f"{target.mention}\n`{target.id}`", inline=True)
    e.add_field(name="Moderator", value=mod.mention if mod else "Άγνωστος", inline=True)
    if extra:
        e.add_field(name="Διάρκεια", value=extra, inline=True)
    e.add_field(name="Αιτιολογία", value=reason or "Δεν δόθηκε", inline=False)
    return e


async def audit_entry(guild, action, target_id):
    try:
        async for e in guild.audit_logs(limit=6, action=action):
            if (e.target and e.target.id == target_id
                    and (discord.utils.utcnow() - e.created_at).total_seconds() < 20):
                return e
    except discord.HTTPException:
        pass
    return None


async def make_transcript(channel):
    out = [f"Transcript - #{channel.name}",
           f"Server: {channel.guild.name}",
           f"Date: {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC",
           "-" * 50]
    async for m in channel.history(limit=None, oldest_first=True):
        out.append(f"[{m.created_at:%Y-%m-%d %H:%M:%S}] {m.author} ({m.author.id}): {m.content}")
        for a in m.attachments:
            out.append(f"    attachment: {a.url}")
        for e in m.embeds:
            if e.title or e.description:
                out.append(f"    embed: {e.title or ''} {e.description or ''}")
    return "\n".join(out).encode("utf-8")


def ticket_owner(channel):
    m = re.search(r"owner:(\d+)", channel.topic or "")
    return int(m.group(1)) if m else None


# ------------------------------------------------------------ tickets
class TicketControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, i: discord.Interaction):
        if not is_admin(i.user):
            await i.response.send_message("Μόνο οι admins μπορούν να το χρησιμοποιήσουν.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="tk_close")
    async def close(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_message("Το ticket κλείνει...")
        ch = i.channel
        owner_id = ticket_owner(ch)
        data = await make_transcript(ch)
        name = f"transcript-{ch.name}.txt"

        emb = base_embed("Ticket Transcript",
                         f"Κανάλι: #{ch.name}\nΧρήστης: <@{owner_id}>\nΈκλεισε από: {i.user.mention}")
        await send_log(i.guild, "tickets", emb, discord.File(io.BytesIO(data), filename=name))

        if DM_USER and owner_id:
            member = i.guild.get_member(owner_id)
            if member:
                try:
                    await member.send(embed=emb, file=discord.File(io.BytesIO(data), filename=name))
                except discord.HTTPException:
                    pass

        await asyncio.sleep(3)
        await ch.delete()

    @discord.ui.button(label="Ping User", emoji="🔔", style=discord.ButtonStyle.secondary, custom_id="tk_ping")
    async def ping(self, i: discord.Interaction, b: discord.ui.Button):
        owner_id = ticket_owner(i.channel)
        if not owner_id:
            return await i.response.send_message("Δεν βρέθηκε ο χρήστης.", ephemeral=True)
        await i.response.send_message(f"<@{owner_id}> ο {i.user.mention} σε περιμένει στο ticket.")


class TicketButton(discord.ui.Button):
    def __init__(self, label, emoji, cid, kind):
        super().__init__(label=label, emoji=emoji, custom_id=cid, style=discord.ButtonStyle.secondary)
        self.kind = kind

    async def callback(self, i: discord.Interaction):
        guild, user = i.guild, i.user
        name = f"{self.kind}-{user.name}".lower().replace(" ", "-")[:90]
        old = discord.utils.get(guild.text_channels, name=name)
        if old:
            return await i.response.send_message(f"Έχεις ήδη ticket: {old.mention}", ephemeral=True)

        # ορατό μόνο σε: δημιουργό, bot και ρόλους με administrator
        perms = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: VIEW,
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                  manage_channels=True, read_message_history=True),
        }
        for role in guild.roles:
            if role.permissions.administrator and not role.is_default():
                perms[role] = VIEW

        category = guild.get_channel(CATEGORY_ID)
        ch = await guild.create_text_channel(name, category=category, overwrites=perms,
                                             topic=f"owner:{user.id} | {self.kind}")

        emb = base_embed(f"Ticket - {self.label}",
                         f"Γεια σου {user.mention}, περίγραψε το πρόβλημά σου "
                         f"και θα σε εξυπηρετήσουμε σύντομα.", ts=False)
        ping = f"<@{guild.owner_id}>" if self.kind == "owner" else ""
        await ch.send(f"{user.mention} {ping}", embed=emb, view=TicketControls())
        await i.response.send_message(f"Το ticket δημιουργήθηκε: {ch.mention}", ephemeral=True)


class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for label, emoji, cid, kind in TICKET_TYPES:
            self.add_item(TicketButton(label, emoji, cid, kind))


# ------------------------------------------------------------ applications
class ApplyModal(discord.ui.Modal, title="Αίτηση στη Μαφία"):
    def __init__(self):
        super().__init__()
        self.inputs = []
        for label, style in APPLICATION_QUESTIONS:
            ti = discord.ui.TextInput(label=label, style=style, max_length=500, required=True)
            self.inputs.append(ti)
            self.add_item(ti)

    async def on_submit(self, i: discord.Interaction):
        ch = get_log(i.guild, "applications")
        if not ch:
            return await i.response.send_message(
                "Δεν έχει οριστεί κανάλι αιτήσεων. Ενημέρωσε έναν admin.", ephemeral=True)

        emb = base_embed("Νέα Αίτηση")
        emb.set_author(name=str(i.user), icon_url=i.user.display_avatar.url)
        emb.add_field(name="Χρήστης", value=i.user.mention, inline=False)
        for ti in self.inputs:
            emb.add_field(name=ti.label, value=ti.value, inline=False)
        emb.set_footer(text=f"{CREDIT} | Applicant ID: {i.user.id}", icon_url=FOOTER_ICON)

        await ch.send(embed=emb, view=AppReview())
        await i.response.send_message("Η αίτησή σου στάλθηκε! Θα ενημερωθείς σύντομα.", ephemeral=True)


class AppPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Κάνε Αίτηση", emoji="📝", style=discord.ButtonStyle.secondary,
                       custom_id="app_open")
    async def open(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(ApplyModal())


class AppReview(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, i: discord.Interaction):
        if not is_admin(i.user):
            await i.response.send_message("Μόνο οι admins μπορούν να το κάνουν αυτό.", ephemeral=True)
            return False
        return True

    async def decide(self, i: discord.Interaction, accepted: bool):
        await i.response.defer()
        emb = i.message.embeds[0]
        uid = int(re.search(r"Applicant ID: (\d+)", emb.footer.text).group(1))
        member = i.guild.get_member(uid)
        if member:
            await dm(member, "Η αίτησή σου έγινε **αποδεκτή**! Καλώς όρισες." if accepted
                     else "Η αίτησή σου δεν έγινε αποδεκτή αυτή τη φορά.")
        emb.color = GREEN if accepted else RED
        emb.add_field(name="Απόφαση",
                      value=f"{'Αποδεκτή' if accepted else 'Απορρίφθηκε'} από {i.user.mention}",
                      inline=False)
        await i.edit_original_response(embed=emb, view=None)

    @discord.ui.button(label="Αποδοχή", style=discord.ButtonStyle.success, custom_id="app_accept")
    async def accept(self, i: discord.Interaction, b: discord.ui.Button):
        await self.decide(i, True)

    @discord.ui.button(label="Απόρριψη", style=discord.ButtonStyle.danger, custom_id="app_deny")
    async def deny(self, i: discord.Interaction, b: discord.ui.Button):
        await self.decide(i, False)


# ------------------------------------------------------------ log setup
def logs_embed(guild):
    cfg = log_cfg(guild.id)
    lines = []
    for key, label in LOG_TYPES.items():
        ch = guild.get_channel(cfg.get(key, 0))
        lines.append(f"**{label}:** {ch.mention if ch else 'δεν έχει οριστεί'}")
    return base_embed("Ρύθμιση Logs",
                      "Διάλεξε τύπο log και μετά το κανάλι του.\n\n" + "\n".join(lines), ts=False)


class LogSetup(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.key = None

    async def interaction_check(self, i: discord.Interaction):
        if not is_admin(i.user):
            await i.response.send_message("Μόνο οι admins μπορούν να το κάνουν αυτό.", ephemeral=True)
            return False
        return True

    async def on_error(self, i: discord.Interaction, error: Exception, item):
        traceback.print_exception(type(error), error, error.__traceback__)
        try:
            if i.response.is_done():
                await i.followup.send(f"Σφάλμα: {error}", ephemeral=True)
            else:
                await i.response.send_message(f"Σφάλμα: {error}", ephemeral=True)
        except discord.HTTPException:
            pass

    @discord.ui.select(placeholder="Διάλεξε τύπο log", row=0,
                       options=[discord.SelectOption(label=v, value=k) for k, v in LOG_TYPES.items()])
    async def pick_type(self, i: discord.Interaction, s: discord.ui.Select):
        self.key = s.values[0]
        await i.response.defer()

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="Διάλεξε κανάλι", row=1)
    async def pick_channel(self, i: discord.Interaction, s: discord.ui.ChannelSelect):
        if not self.key:
            return await i.response.send_message("Διάλεξε πρώτα τύπο log.", ephemeral=True)
        set_log(i.guild.id, self.key, s.values[0].id)
        await i.response.edit_message(embed=logs_embed(i.guild), view=self)


# ------------------------------------------------------------ bot
class Tree(app_commands.CommandTree):
    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.guild and is_admin(i.user):
            return True
        await i.response.send_message("Μόνο οι admins μπορούν να χρησιμοποιήσουν το bot.", ephemeral=True)
        return False

    async def on_error(self, i: discord.Interaction, error):
        traceback.print_exception(type(error), error, error.__traceback__)
        msg = f"Σφάλμα: {error}"
        try:
            if i.response.is_done():
                await i.followup.send(msg, ephemeral=True)
            else:
                await i.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents, tree_cls=Tree,
                         activity=discord.Game(CREDIT))
        self.skip = set()

    async def setup_hook(self):
        self.add_view(TicketPanel())
        self.add_view(TicketControls())
        self.add_view(AppPanel())
        self.add_view(AppReview())
        await self.tree.sync()


bot = Bot()


def take_skip(kind, uid):
    if (kind, uid) in bot.skip:
        bot.skip.discard((kind, uid))
        return True
    return False


# ------------------------------------------------------------ commands: setup
@bot.tree.command(name="setup_logs_channels", description="Δείχνει και ρυθμίζει τα κανάλια των logs")
@admin
async def setup_logs_channels(i: discord.Interaction):
    await i.response.send_message(embed=logs_embed(i.guild), view=LogSetup(), ephemeral=True)


@bot.tree.command(name="set_log_channel", description="Ορίζει κανάλι για συγκεκριμένο τύπο log")
@app_commands.describe(log_type="Τύπος log", channel="Το κανάλι")
@app_commands.choices(log_type=[app_commands.Choice(name=v, value=k) for k, v in LOG_TYPES.items()])
@admin
async def set_log_channel(i: discord.Interaction, log_type: app_commands.Choice[str],
                          channel: discord.TextChannel):
    set_log(i.guild.id, log_type.value, channel.id)
    await i.response.send_message(f"Τα logs **{log_type.name}** θα πηγαίνουν στο {channel.mention}.",
                                  ephemeral=True)


@bot.tree.command(name="ticketpanel", description="Στέλνει το ticket panel")
@app_commands.describe(channel="Κανάλι (προεπιλογή: αυτό)")
@admin
async def ticketpanel(i: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    channel = channel or i.channel
    e = discord.Embed(title=TICKET_TITLE, description=f"> {TICKET_DESC}", color=COLOR)
    e.set_thumbnail(url=THUMB)
    e.set_footer(text=CREDIT, icon_url=FOOTER_ICON)
    await channel.send(embed=e, view=TicketPanel())
    await i.response.send_message("Στάλθηκε ✅", ephemeral=True)


@bot.tree.command(name="applypanel", description="Στέλνει το panel αιτήσεων")
@app_commands.describe(channel="Κανάλι (προεπιλογή: αυτό)")
@admin
async def applypanel(i: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    channel = channel or i.channel
    e = base_embed("Αιτήσεις",
                   "> Θες να γίνεις μέλος μας; Πάτα το κουμπί και απάντησε στις ερωτήσεις.", ts=False)
    e.set_thumbnail(url=THUMB)
    await channel.send(embed=e, view=AppPanel())
    await i.response.send_message("Στάλθηκε ✅", ephemeral=True)


# ------------------------------------------------------------ commands: moderation
@bot.tree.command(name="ban", description="Ban χρήστη")
@app_commands.describe(user="Ο χρήστης", reason="Αιτιολογία", delete_days="Μέρες μηνυμάτων προς διαγραφή (0-7)")
@admin
async def ban(i: discord.Interaction, user: discord.User, reason: str = "Δεν δόθηκε αιτιολογία",
              delete_days: app_commands.Range[int, 0, 7] = 0):
    member = i.guild.get_member(user.id)
    if member and not hierarchy_ok(i, member):
        return await i.response.send_message("Δεν μπορείς να κάνεις ban αυτόν τον χρήστη.", ephemeral=True)
    if member:
        await dm(user, f"Έγινες ban από τον **{i.guild.name}**.\nΑιτιολογία: {reason}")
    bot.skip.add(("ban", user.id))
    try:
        await i.guild.ban(user, reason=f"{reason} | από {i.user}",
                          delete_message_seconds=delete_days * 86400)
    except discord.HTTPException as ex:
        bot.skip.discard(("ban", user.id))
        return await i.response.send_message(f"Αποτυχία: {ex}", ephemeral=True)
    await send_log(i.guild, "bans", mod_embed("Ban", user, i.user, reason, RED))
    await i.response.send_message(f"Έγινε ban ο {user.mention}.", ephemeral=True)


@bot.tree.command(name="unban", description="Unban χρήστη με ID")
@app_commands.describe(user_id="Το ID του χρήστη", reason="Αιτιολογία")
@admin
async def unban(i: discord.Interaction, user_id: str, reason: str = "Δεν δόθηκε αιτιολογία"):
    if not user_id.isdigit():
        return await i.response.send_message("Βάλε έγκυρο ID.", ephemeral=True)
    try:
        user = await bot.fetch_user(int(user_id))
        await i.guild.unban(user, reason=f"{reason} | από {i.user}")
    except discord.HTTPException as ex:
        return await i.response.send_message(f"Αποτυχία: {ex}", ephemeral=True)
    await send_log(i.guild, "bans", mod_embed("Unban", user, i.user, reason, GREEN))
    await i.response.send_message(f"Έγινε unban ο {user.mention}.", ephemeral=True)


@bot.tree.command(name="kick", description="Kick μέλους")
@app_commands.describe(member="Το μέλος", reason="Αιτιολογία")
@admin
async def kick(i: discord.Interaction, member: discord.Member, reason: str = "Δεν δόθηκε αιτιολογία"):
    if not hierarchy_ok(i, member):
        return await i.response.send_message("Δεν μπορείς να κάνεις kick αυτό το μέλος.", ephemeral=True)
    await dm(member, f"Έγινες kick από τον **{i.guild.name}**.\nΑιτιολογία: {reason}")
    bot.skip.add(("kick", member.id))
    try:
        await member.kick(reason=f"{reason} | από {i.user}")
    except discord.HTTPException as ex:
        bot.skip.discard(("kick", member.id))
        return await i.response.send_message(f"Αποτυχία: {ex}", ephemeral=True)
    await send_log(i.guild, "kicks", mod_embed("Kick", member, i.user, reason, ORANGE))
    await i.response.send_message(f"Έγινε kick ο {member.mention}.", ephemeral=True)


@bot.tree.command(name="timeout", description="Timeout μέλους")
@app_commands.describe(member="Το μέλος", duration="Διάρκεια: 30s, 10m, 2h, 1d, 1w (μέχρι 28d)",
                       reason="Αιτιολογία")
@admin
async def timeout(i: discord.Interaction, member: discord.Member, duration: str,
                  reason: str = "Δεν δόθηκε αιτιολογία"):
    delta = parse_duration(duration)
    if not delta:
        return await i.response.send_message("Λάθος διάρκεια. Παραδείγματα: 30m, 2h, 1d.", ephemeral=True)
    if not hierarchy_ok(i, member):
        return await i.response.send_message("Δεν μπορείς να κάνεις timeout αυτό το μέλος.", ephemeral=True)
    bot.skip.add(("timeout", member.id))
    try:
        await member.timeout(delta, reason=f"{reason} | από {i.user}")
    except discord.HTTPException as ex:
        bot.skip.discard(("timeout", member.id))
        return await i.response.send_message(f"Αποτυχία: {ex}", ephemeral=True)
    await dm(member, f"Έγινες timeout στον **{i.guild.name}** για {duration}.\nΑιτιολογία: {reason}")
    await send_log(i.guild, "timeouts", mod_embed("Timeout", member, i.user, reason, ORANGE, extra=duration))
    await i.response.send_message(f"Timeout στον {member.mention} για {duration}.", ephemeral=True)


@bot.tree.command(name="untimeout", description="Αφαίρεση timeout")
@app_commands.describe(member="Το μέλος", reason="Αιτιολογία")
@admin
async def untimeout(i: discord.Interaction, member: discord.Member, reason: str = "Δεν δόθηκε αιτιολογία"):
    bot.skip.add(("timeout", member.id))
    try:
        await member.timeout(None, reason=f"{reason} | από {i.user}")
    except discord.HTTPException as ex:
        bot.skip.discard(("timeout", member.id))
        return await i.response.send_message(f"Αποτυχία: {ex}", ephemeral=True)
    await send_log(i.guild, "timeouts", mod_embed("Timeout Removed", member, i.user, reason, GREEN))
    await i.response.send_message(f"Αφαιρέθηκε το timeout του {member.mention}.", ephemeral=True)


# ------------------------------------------------------------ commands: announce / say
class AnnounceModal(discord.ui.Modal, title="Νέα Ανακοίνωση"):
    heading = discord.ui.TextInput(label="Τίτλος", max_length=100)
    body = discord.ui.TextInput(label="Μήνυμα", style=discord.TextStyle.paragraph, max_length=2000)

    def __init__(self, channel, ping):
        super().__init__()
        self.channel = channel
        self.ping = ping

    async def on_submit(self, i: discord.Interaction):
        emb = base_embed(self.heading.value, self.body.value, ts=True)
        content = {"everyone": "@everyone", "here": "@here"}.get(self.ping)
        await self.channel.send(content=content, embed=emb,
                                allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=True))
        await i.response.send_message("Η ανακοίνωση στάλθηκε ✅", ephemeral=True)


@bot.tree.command(name="announce", description="Στέλνει ανακοίνωση (embed)")
@app_commands.describe(channel="Κανάλι (προεπιλογή: αυτό)", ping="Ποιον να κάνει ping")
@app_commands.choices(ping=[app_commands.Choice(name="Κανένα", value="none"),
                            app_commands.Choice(name="@here", value="here"),
                            app_commands.Choice(name="@everyone", value="everyone")])
@admin
async def announce(i: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                   ping: Optional[app_commands.Choice[str]] = None):
    await i.response.send_modal(AnnounceModal(channel or i.channel, ping.value if ping else "none"))


@bot.tree.command(name="say", description="Το bot στέλνει ένα μήνυμα (χρησιμοποίησε \\n για νέα γραμμή)")
@app_commands.describe(message="Το μήνυμα", channel="Κανάλι (προεπιλογή: αυτό)")
@admin
async def say(i: discord.Interaction, message: str, channel: Optional[discord.TextChannel] = None):
    channel = channel or i.channel
    await channel.send(message.replace("\\n", "\n"),
                       allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=True))
    await i.response.send_message("Στάλθηκε ✅", ephemeral=True)


# ------------------------------------------------------------ events / logs
@bot.event
async def on_member_ban(guild, user):
    if take_skip("ban", user.id):
        return
    e = await audit_entry(guild, discord.AuditLogAction.ban, user.id)
    await send_log(guild, "bans", mod_embed("Ban", user, e.user if e else None,
                                            e.reason if e else None, RED))


@bot.event
async def on_member_remove(member):
    if take_skip("kick", member.id):
        return
    e = await audit_entry(member.guild, discord.AuditLogAction.kick, member.id)
    if e:
        await send_log(member.guild, "kicks", mod_embed("Kick", member, e.user, e.reason, ORANGE))


@bot.event
async def on_member_update(before, after):
    if before.timed_out_until == after.timed_out_until:
        return
    if take_skip("timeout", after.id):
        return
    e = await audit_entry(after.guild, discord.AuditLogAction.member_update, after.id)
    applied = after.timed_out_until and after.timed_out_until > discord.utils.utcnow()
    if applied:
        emb = mod_embed("Timeout", after, e.user if e else None, e.reason if e else None, ORANGE,
                        extra=f"μέχρι <t:{int(after.timed_out_until.timestamp())}:R>")
    else:
        emb = mod_embed("Timeout Removed", after, e.user if e else None, e.reason if e else None, GREEN)
    await send_log(after.guild, "timeouts", emb)


@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel == after.channel:
        return
    if before.channel is None:
        emb = base_embed("Voice Join", f"{member.mention} μπήκε στο {after.channel.mention}", GREEN)
    elif after.channel is None:
        emb = base_embed("Voice Disconnect", f"{member.mention} αποσυνδέθηκε από το {before.channel.mention}", RED)
    else:
        emb = base_embed("Voice Move",
                         f"{member.mention} μετακινήθηκε\n{before.channel.mention} → {after.channel.mention}",
                         ORANGE)
    emb.set_thumbnail(url=member.display_avatar.url)
    await send_log(member.guild, "voice", emb)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


bot.run(TOKEN)
