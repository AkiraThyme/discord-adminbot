import discord
from discord import app_commands, ui, ButtonStyle, Interaction
from discord.ext import commands
import os
from dotenv import load_dotenv
from supabase import create_client, Client
from postgrest.exceptions import APIError
import asyncio 
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, Security
from fastapi.middleware.cors import CORSMiddleware 
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import time
from datetime import timedelta
from contextlib import asynccontextmanager
load_dotenv()

TOKEN = os.getenv('BOT_TOKEN')
S_URL = os.getenv('SUPABASE_URL')
S_KEY = os.getenv('SUPABASE_KEY')
REPORT_ISSUE_CHANNEL_ID = int('1312408228281319454')

supabase: Client = create_client(S_URL, S_KEY)

# --- ADD THESE TWO LINES ---
print("--- Checking Supabase Connection ---")
print(f"Connecting to URL: {S_URL}")
# -----------------------------

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
intents.presences = True

bot = commands.Bot(command_prefix="/", intents=intents)
bot_ready = asyncio.Event()

# --- Auth / Security Helpers ---
security = HTTPBearer(auto_error=False)

def _extract_discord_id_from_supabase_user(user: Any) -> Optional[str]:
    if user is None:
        return None
    # Common places where Supabase (Discord provider) stores the provider id
    try:
        metadata = getattr(user, "user_metadata", None) or {}
        discord_id = (
            metadata.get("provider_id")
            or metadata.get("sub")
            or metadata.get("id")
        )
        if discord_id:
            return str(discord_id)
    except Exception:
        pass

    try:
        identities = getattr(user, "identities", None) or []
        for identity in identities:
            provider = getattr(identity, "provider", None) or (
                identity.get("provider") if isinstance(identity, dict) else None
            )
            if provider == "discord":
                data = getattr(identity, "identity_data", None) or (
                    identity.get("identity_data") if isinstance(identity, dict) else {}
                )
                discord_id = data.get("sub") or data.get("id") or data.get("provider_id")
                if discord_id:
                    return str(discord_id)
    except Exception:
        pass

    # Optional: mapping table fallback by Supabase auth user id
    try:
        user_id = getattr(user, "id", None)
        if user_id:
            resp = (
                supabase
                .table("moderators")
                .select("discord_id")
                .eq("user_id", str(user_id))
                .maybe_single()
                .execute()
            )
            data = getattr(resp, "data", None) or resp
            if isinstance(data, dict) and data.get("discord_id"):
                return str(data["discord_id"]) 
    except Exception:
        pass
    return None

async def get_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Security(security)) -> Any:
    """Validate Supabase JWT from Authorization header and return Supabase user object."""
    if credentials is None or (credentials.scheme or "").lower() != "bearer":
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = credentials.credentials
    try:
        user_response = supabase.auth.get_user(token)
        user = getattr(user_response, "user", None) or getattr(user_response, "data", None)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

async def get_moderator_context(guild_id: int, credentials: Optional[HTTPAuthorizationCredentials] = Security(security)) -> Dict[str, Any]:
    """Resolve authenticated moderator and their Discord guild roles for the given guild."""
    user = await get_current_user(credentials)
    discord_id = _extract_discord_id_from_supabase_user(user)
    if not discord_id:
        raise HTTPException(status_code=403, detail="No linked Discord account found")

    await bot_ready.wait()
    guild = await get_guild_or_404(guild_id)
    try:
        member = await guild.fetch_member(int(discord_id))
    except Exception:
        member = None
    if not member:
        raise HTTPException(status_code=403, detail="Discord member not found in this server")

    # Collect role names and key permission flags
    role_names = [r.name for r in member.roles if r.name != "@everyone"]
    perms = member.guild_permissions
    permissions = {
        "manage_messages": perms.manage_messages,
        "kick_members": perms.kick_members,
        "ban_members": perms.ban_members,
        "administrator": perms.administrator,
        "manage_guild": perms.manage_guild,
    }

    return {
        "supabase_user": {
            "id": str(getattr(user, "id", "")),
            "email": getattr(user, "email", None),
        },
        "discord": {
            "id": str(discord_id),
            "username": str(member.name),
            "roles": role_names,
            "permissions": permissions,
        },
    }

async def moderator_context_dependency(guild_id: int, credentials: Optional[HTTPAuthorizationCredentials] = Security(security)) -> Dict[str, Any]:
    return await get_moderator_context(guild_id, credentials)

# --- Simple cooldowns and rule texts ---
TICKET_COOLDOWN_SECONDS = 120
REPORT_COOLDOWN_SECONDS = 0
_ticket_cooldowns: Dict[int, float] = {}
_report_cooldowns: Dict[int, float] = {}

# Ticket inactivity auto-close (20 minutes)
TICKET_INACTIVITY_SECONDS = 20 * 60
_ticket_timers: Dict[int, asyncio.Task] = {}

def _get_ticket_log_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    try:
        settings = (
            supabase
            .table('server_config')
            .select('settings')
            .eq('guild_id', str(guild.id))
            .single()
            .execute()
            .data['settings']
        )
        log_id = settings.get('ticket_log_channel_id') if isinstance(settings, dict) else None
    except Exception:
        log_id = None
    return guild.get_channel(int(log_id)) if log_id else discord.utils.get(guild.text_channels, name="ticket-logs")

def cancel_ticket_timer(thread_id: int) -> None:
    task = _ticket_timers.pop(thread_id, None)
    if task and not task.done():
        task.cancel()

def schedule_ticket_timer(thread: discord.Thread) -> None:
    cancel_ticket_timer(thread.id)

    async def _timer():
        try:
            await asyncio.sleep(TICKET_INACTIVITY_SECONDS)
            if thread and not thread.archived:
                await thread.edit(archived=True, locked=True)
                log_channel = _get_ticket_log_channel(thread.guild)
                if log_channel:
                    await log_channel.send(f"⏲️ Ticket `{thread.name}` auto-closed after 20 minutes of inactivity.")
        except asyncio.CancelledError:
            pass
        except Exception:
            # Avoid crashing on background errors
            pass

    _ticket_timers[thread.id] = asyncio.create_task(_timer())

REPORT_RULES = [
    "Provide clear evidence when possible (screenshots, message links).",
    "False or malicious reports may result in action against you.",
    "Emergency issues should be pinged to on-duty moderators if allowed.",
]

TICKET_RULES = [
    "Open only one active ticket at a time.",
    "Use tickets for private matters requiring staff assistance.",
    f"Ticket creation cooldown: {int(TICKET_COOLDOWN_SECONDS)}s.",
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("--- API LIFESPAN: Logging bot in... ---")
    await bot.login(TOKEN)
    
    print("--- API LIFESPAN: Connecting bot to the gateway... ---")
    asyncio.create_task(bot.connect(reconnect=True))
    
    await bot.wait_until_ready()
    print("--- API LIFESPAN: Bot is ready. API is now live. ---")
    
    yield
    
    print("--- API LIFESPAN: Shutting down... ---")
    await bot.close()


api = FastAPI(lifespan=lifespan)

origins = [
    "https://swarmdiscordbot.vercel.app",
    "http://localhost:5173"
]

api.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@api.get("/auth/me")
async def auth_me(guild_id: Optional[int] = None, creds: Optional[HTTPAuthorizationCredentials] = Security(security)):
    """Return authenticated Supabase user and, if guild_id is provided, mapped Discord context."""
    user = await get_current_user(creds)
    payload: Dict[str, Any] = {
        "supabase_user": {
            "id": str(getattr(user, "id", "")),
            "email": getattr(user, "email", None),
        }
    }
    if guild_id is not None:
        ctx = await get_moderator_context(guild_id, creds)
        payload.update({"discord": ctx["discord"]})
    else:
        # Provide discord id if available even without guild
        discord_id = _extract_discord_id_from_supabase_user(user)
        if discord_id:
            payload["discord"] = {"id": str(discord_id)}
    return payload

class ServerSettings(BaseModel):
    prefix: str
    welcome_message: str
    log_channel: str
    auto_role: str
    ticket_log_channel_id: Optional[str] = None
    suggestion_log_channel_id: Optional[str] = None

DEFAULT_SETTINGS = {
    "prefix": "!",
    "welcome_message": "Welcome to the server!",
    "log_channel": "bot-logs",
    "auto_role": "Member"
}

def fetch_settings_from_discord(guild_id):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return DEFAULT_SETTINGS
    
    print(guild)
    
    return {
        "name": guild.name,
        "id": guild.id,
        "prefix": "!",
    }

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            await connection.send_json(message)

manager = ConnectionManager()


class AdminReportControls(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def handle_action(self, interaction: Interaction, action: str):
        for item in self.children:
            item.disabled = True
        
        original_embed = interaction.message.embeds[0]
        original_embed.color = discord.Color.dark_grey()
        original_embed.title = f"🚨 Report Handled: {action.upper()}"
        original_embed.add_field(name="Handled By", value=interaction.user.mention)
        
        await interaction.message.edit(embed=original_embed, view=self)
        await interaction.response.send_message(f"Report has been marked as '{action}'.", ephemeral=True)

    @ui.button(label="Ban User", style=ButtonStyle.danger, custom_id="ban_user_report")
    async def ban_user(self, interaction: Interaction, button: ui.Button):
        original_embed = interaction.message.embeds[0]
        footer_text = original_embed.footer.text
        
        try:
            reported_user_id = int(footer_text.split("ID: ")[1].strip(")"))
            member_to_ban = await interaction.guild.fetch_member(reported_user_id)
            
            if member_to_ban:
                await member_to_ban.ban(reason=f"Banned following a report filed by an admin.")
                await self.handle_action(interaction, "Banned")
            else:
                await interaction.response.send_message("Could not find this user in the server.", ephemeral=True)
        except (IndexError, ValueError, discord.NotFound):
            await interaction.response.send_message("Error: Could not extract a valid User ID from the report.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("Error: I don't have permission to ban this user.", ephemeral=True)

    @ui.button(label="Mark as Resolved", style=ButtonStyle.success, custom_id="resolve_report")
    async def resolve(self, interaction: Interaction, button: ui.Button):
        await self.handle_action(interaction, "Resolved")

class ReportModal(ui.Modal, title="File a Report"):
    """
    The pop-up form. It now only contains the text fields
    and receives the category when it's created.
    """
    reported_user_input = ui.TextInput(label="Username to report", required=True)
    reason = ui.TextInput(label="Reason for the report", style=discord.TextStyle.paragraph, required=True)

    def __init__(self, category: str):
        super().__init__()
        self.report_category = category

    async def on_submit(self, interaction: Interaction):
        try:
            report_data = {
                'reporter_id': str(interaction.user.id),
                'reporter_name': interaction.user.name,
                'reported_user': self.reported_user_input.value,
                'reason': self.reason.value,
                'category': self.report_category
            }
            supabase.table('reports').insert(report_data).execute()
            await interaction.response.send_message(f"Thank you, your report against **{self.reported_user_input.value}** has been filed.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message("Sorry, there was an error processing your report.", ephemeral=True)
            print(f"Error inserting report into Supabase: {e}")
            return

        try:
            admin_channel = discord.utils.get(interaction.guild.text_channels, name="admin-controls")
            if not admin_channel:
                return

            reported_member = discord.utils.find(
                lambda m: str(m).lower() == self.reported_user_input.value.lower() or m.name.lower() == self.reported_user_input.value.lower(),
                interaction.guild.members
            )

            report_embed = discord.Embed(title="🚨 New Report Filed", color=discord.Color.orange())
            report_embed.add_field(name="Category", value=self.report_category or "Not specified", inline=False)
            report_embed.add_field(name="Reported User", value=self.reported_user_input.value, inline=False)
            report_embed.add_field(name="Reason", value=self.reason.value, inline=False)
            
            view_to_send = ui.View()
            if reported_member:
                report_embed.set_footer(text=f"Reported User ID: {reported_member.id}")
                view_to_send = AdminReportControls()
            else:
                report_embed.set_footer(text="User not found automatically. Action must be manual.")

            await admin_channel.send(embed=report_embed, view=view_to_send)
        except Exception as e:
            print(f"Failed to send admin notification: {e}")

class CategorySelectView(ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @ui.select(
        placeholder="Choose the report category...",
        options=[
            discord.SelectOption(label="Harassment"),
            discord.SelectOption(label="Spam"),
            discord.SelectOption(label="Inappropriate Content"),
            discord.SelectOption(label="Other"),
        ]
    )
    async def select_category(self, interaction: Interaction, select: ui.Select):
        category = select.values[0]
        select.disabled = True
        # Cooldown check
        # Show rules confirmation
        rules_text = "\n".join([f"- {r}" for r in REPORT_RULES])
        view = ui.View()
        async def accept_callback(i: Interaction):
            await i.response.send_modal(ReportModal(category=category))
        async def cancel_callback(i: Interaction):
            await i.response.send_message("Report canceled.", ephemeral=True)
        accept_btn = ui.Button(label="I Understand", style=ButtonStyle.success)
        cancel_btn = ui.Button(label="Cancel", style=ButtonStyle.secondary)
        accept_btn.callback = accept_callback
        cancel_btn.callback = cancel_callback
        view.add_item(accept_btn)
        view.add_item(cancel_btn)
        await interaction.response.send_message(f"Before filing a report, please review:\n{rules_text}", view=view, ephemeral=True)

class SuggestionModal(ui.Modal, title="Make a Suggestion"):
    suggestion_input = ui.TextInput(label="What is your suggestion?", style=discord.TextStyle.paragraph, required=True, max_length=1000)
    async def on_submit(self, interaction: Interaction):
        # Suggestion collection and logging intentionally disabled per configuration
        await interaction.response.send_message("Suggestions are currently disabled.", ephemeral=True)

class BroadcastModal(ui.Modal, title="Broadcast a Message"):
    channel_name = ui.TextInput(label="Channel Name (e.g., announcements)", placeholder="general", required=True)
    message_content = ui.TextInput(label="Your Message", style=discord.TextStyle.paragraph, required=True, max_length=2000)
    async def on_submit(self, interaction: Interaction):
        channel = discord.utils.get(interaction.guild.text_channels, name=self.channel_name.value)
        if channel:
            await channel.send(self.message_content.value)
            await interaction.response.send_message(f"Message sent to #{channel.name}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"Could not find a channel named `{self.channel_name.value}`.", ephemeral=True)


class TicketControls(ui.View):
    def __init__(self, opener_id: int):
        super().__init__(timeout=None)
        self.opener_id = opener_id

    @ui.button(label="Close Ticket", style=ButtonStyle.success, custom_id="ticket_close")
    async def close_ticket(self, interaction: Interaction, button: ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await interaction.response.send_message("This action can only be used inside a ticket thread.", ephemeral=True)
            return

        is_opener = interaction.user.id == self.opener_id
        perms = interaction.user.guild_permissions
        is_mod = perms.manage_threads or perms.manage_channels or perms.administrator
        if not (is_opener or is_mod):
            await interaction.response.send_message("You don't have permission to close this ticket.", ephemeral=True)
            return

        try:
            await channel.edit(archived=True, locked=True)
            await interaction.response.send_message("✅ Ticket closed.", ephemeral=True)
            # Prefer configured channel id if available
            try:
                settings = supabase.table('server_config').select('settings').eq('guild_id', str(interaction.guild.id)).single().execute().data['settings']
                log_id = settings.get('ticket_log_channel_id') if isinstance(settings, dict) else None
            except Exception:
                log_id = None
            log_channel = interaction.guild.get_channel(int(log_id)) if log_id else discord.utils.get(interaction.guild.text_channels, name="ticket-logs")
            if log_channel:
                await log_channel.send(f"Ticket `{channel.name}` closed by {interaction.user.mention}")
            cancel_ticket_timer(channel.id)
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to manage this thread.", ephemeral=True)
        except Exception:
            await interaction.response.send_message("Failed to close this ticket.", ephemeral=True)

    @ui.button(label="Cancel Ticket (Admin)", style=ButtonStyle.danger, custom_id="ticket_cancel")
    async def cancel_ticket(self, interaction: Interaction, button: ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await interaction.response.send_message("This action can only be used inside a ticket thread.", ephemeral=True)
            return

        perms = interaction.user.guild_permissions
        if not (perms.manage_threads or perms.manage_channels or perms.administrator):
            await interaction.response.send_message("You don't have permission to cancel this ticket.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            try:
                settings = supabase.table('server_config').select('settings').eq('guild_id', str(interaction.guild.id)).single().execute().data['settings']
                log_id = settings.get('ticket_log_channel_id') if isinstance(settings, dict) else None
            except Exception:
                log_id = None
            log_channel = interaction.guild.get_channel(int(log_id)) if log_id else discord.utils.get(interaction.guild.text_channels, name="ticket-logs")
            if log_channel:
                await log_channel.send(f"🗑️ Ticket `{channel.name}` cancelled by {interaction.user.mention}")
            await channel.delete()
            await interaction.followup.send("🗑️ Ticket thread deleted.", ephemeral=True)
            cancel_ticket_timer(channel.id)
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to delete this thread.", ephemeral=True)
        except Exception:
            await interaction.followup.send("Failed to cancel this ticket.", ephemeral=True)


class PublicControls(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @ui.button(label="File a Report", style=ButtonStyle.danger, custom_id="file_report_button")
    async def file_report(self, interaction: Interaction, button: ui.Button):
        await interaction.response.send_message("Please select a category for your report.", view=CategorySelectView(), ephemeral=True)
    @ui.button(label="Open Support Ticket", style=ButtonStyle.primary, custom_id="open_ticket_button")
    async def open_ticket(self, interaction: Interaction, button: ui.Button):
        # Enforce cooldown
        now = time.time()
        last = _ticket_cooldowns.get(interaction.user.id, 0)
        if now - last < TICKET_COOLDOWN_SECONDS:
            remaining = int(TICKET_COOLDOWN_SECONDS - (now - last))
            await interaction.response.send_message(f"Please wait {remaining}s before opening another ticket.", ephemeral=True)
            return

        # Enforce one active ticket per user in this channel category
        for thread in interaction.channel.threads:
            if thread.owner_id == bot.user.id and not thread.archived and interaction.user.name in thread.name:
                await interaction.response.send_message("You already have an active ticket in this channel.", ephemeral=True)
                return

        rules_text = "\n".join([f"- {r}" for r in TICKET_RULES])
        view = ui.View()
        async def accept_callback(i: Interaction):
            _ticket_cooldowns[interaction.user.id] = time.time()
            thread = await interaction.channel.create_thread(name=f"support-{interaction.user.name}", type=discord.ChannelType.private_thread)
            schedule_ticket_timer(thread)
            await i.response.send_message(f"A private support ticket has been created for you: {thread.mention}", ephemeral=True)
            await thread.send(f"Hello {interaction.user.mention}, how can the admin team assist you today?", view=TicketControls(opener_id=interaction.user.id))
        async def cancel_callback(i: Interaction):
            await i.response.send_message("Ticket creation canceled.", ephemeral=True)
        accept_btn = ui.Button(label="I Understand", style=ButtonStyle.success)
        cancel_btn = ui.Button(label="Cancel", style=ButtonStyle.secondary)
        accept_btn.callback = accept_callback
        cancel_btn.callback = cancel_callback
        view.add_item(accept_btn)
        view.add_item(cancel_btn)
        await interaction.response.send_message(f"Before opening a ticket, please review:\n{rules_text}", view=view, ephemeral=True)

class AdminControlsView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ui.Button(label="View Web Dashboard", style=ButtonStyle.link, url="http://swarmdiscordbot.vercel.app"))

    @ui.button(label="Lockdown Server", style=ButtonStyle.danger, custom_id="lockdown_server_button")
    async def lockdown_server(self, interaction: Interaction, button: ui.Button):
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
            return

        await interaction.response.defer()
        
        everyone_role = interaction.guild.default_role
        current_perms = everyone_role.permissions

        if current_perms.send_messages:
            new_perms = everyone_role.permissions
            new_perms.update(send_messages=False)
            await everyone_role.edit(permissions=new_perms)
            
            button.label = "Unlock Server"
            button.style = ButtonStyle.success
            await interaction.message.edit(view=self)
            await interaction.followup.send("🚨 Server has been **locked down**.", ephemeral=True)
        else:
            new_perms = everyone_role.permissions
            new_perms.update(send_messages=True)
            await everyone_role.edit(permissions=new_perms)
            
            button.label = "Lockdown Server"
            button.style = ButtonStyle.danger
            await interaction.message.edit(view=self)
            await interaction.followup.send("✅ Server is no longer in lockdown.", ephemeral=True)

    @ui.button(label="Broadcast Message", style=ButtonStyle.secondary, custom_id="broadcast_message_button")
    async def broadcast_message(self, interaction: Interaction, button: ui.Button):
        if not interaction.user.guild_permissions.mention_everyone:
            await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
            return
        await interaction.response.send_modal(BroadcastModal())

# --- API ENDPOINTS ---

@api.websocket("/ws/status")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    print("Dashboard WebSocket connected.")
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        print("Dashboard WebSocket disconnected.")

@api.get("/servers")
async def get_servers(creds: Optional[HTTPAuthorizationCredentials] = Security(security)):
    await bot_ready.wait()

    user = await get_current_user(creds)
    discord_id = _extract_discord_id_from_supabase_user(user)
    if not discord_id:
        raise HTTPException(status_code=403, detail="No linked discord account found.")

    visible: list[dict] = []
    for guild in bot.guilds:
        try:
            # Fast check: owner
            if guild.owner_id == int(discord_id):
                visible.append({
                    "id": str(guild.id),
                    "name": guild.name,
                    "icon_url": str(guild.icon.url) if guild.icon else None
                })
                continue

            try:
                member = await guild.fetch_member(int(discord_id))
            except Exception:
                member = None

            if member:
                perms = member.guild_permissions
                if perms.administrator or perms.manage_guild:
                    visible.append({
                        "id": str(guild.id),
                        "name": guild.name,
                        "icon_url": str(guild.icon.url) if guild.icon else None
                    })
        except Exception:
            continue

    return visible

# Helper function to get a guild and handle errors
async def get_guild_or_404(guild_id: int):
    """Tries to get a guild from cache, falls back to fetching from the API."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        try:
            guild = await bot.fetch_guild(guild_id)
        except discord.NotFound:
            raise HTTPException(status_code=404, detail=f"Server with ID {guild_id} not found.")
    return guild

@api.get("/servers/{guild_id}/members")
async def get_members(guild_id: int):
    await bot_ready.wait()
    guild = await get_guild_or_404(guild_id)
    return [{'id': str(m.id), 'name': m.name, 'nick': m.nick, 'status': str(m.status), 'avatar_url': str(m.display_avatar.url), 'roles': [r.name for r in m.roles if r.name != "@everyone"]} for m in guild.members]

@api.get("/servers/{guild_id}/channels")
async def get_channels(guild_id: int):
    await bot_ready.wait()
    guild = await get_guild_or_404(guild_id)
    return [{'id': str(c.id), 'name': c.name, 'type': str(c.type)} for c in guild.channels]

@api.get("/servers/{guild_id}/roles")
async def get_roles(guild_id: int):
    await bot_ready.wait()
    guild = await get_guild_or_404(guild_id)
    return [{'id': str(r.id), 'name': r.name, 'color': str(r.color)} for r in guild.roles]


# NEW: Endpoint to get server settings
@api.get("/servers/{guild_id}/settings")
async def get_settings(guild_id: int):
    try:
        response = supabase.table('server_config').select('settings').eq('guild_id', str(guild_id)).single().execute()
        return response.data['settings']
    except APIError as e:
        if e.code == 'PGRST116':
            settings = fetch_settings_from_discord(guild_id)
            supabase.table('server_config').insert({
                "guild_id": str(guild_id),
                "settings": settings
            }).execute()
            return settings
        else:
            raise HTTPException(status_code=500, detail=str(e))

# NEW: Endpoint to save server settings
@api.post("/servers/{guild_id}/settings")
async def save_settings(guild_id: int, settings: ServerSettings, ctx: Dict[str, Any] = Depends(moderator_context_dependency)):
    await bot_ready.wait()
    guild = await get_guild_or_404(guild_id)

    try:
        # Optional: verify that caller has manage_guild or administrator
        permissions = ctx.get("discord", {}).get("permissions", {})
        if not (permissions.get("manage_guild") or permissions.get("administrator")):
            raise HTTPException(status_code=403, detail="Insufficient permissions to update settings")

        supabase.table('server_config').upsert({
            "guild_id": str(guild_id),
            "settings": settings.dict()
        }).execute()
        return {"message": "Settings saved successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- Logging Channel Endpoints ---
class LoggingSettings(BaseModel):
    ticket_log_channel_id: Optional[str] = None
    suggestion_log_channel_id: Optional[str] = None

@api.get("/servers/{guild_id}/logging")
async def get_logging_settings(guild_id: int):
    try:
        resp = supabase.table('server_config').select('settings').eq('guild_id', str(guild_id)).maybe_single().execute()
        settings = (resp.data or {}).get('settings') if isinstance(resp, object) else None
        if not isinstance(settings, dict):
            settings = {}
        return {
            "ticket_log_channel_id": settings.get("ticket_log_channel_id"),
            "suggestion_log_channel_id": settings.get("suggestion_log_channel_id"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api.post("/servers/{guild_id}/logging")
async def set_logging_settings(guild_id: int, payload: LoggingSettings, ctx: Dict[str, Any] = Depends(moderator_context_dependency)):
    await bot_ready.wait()
    guild = await get_guild_or_404(guild_id)

    # Require manage_guild or administrator
    permissions = ctx.get("discord", {}).get("permissions", {})
    if not (permissions.get("manage_guild") or permissions.get("administrator")):
        raise HTTPException(status_code=403, detail="Insufficient permissions to update logging settings")

    # Validate that provided channel IDs exist in the guild (if present)
    def validate_channel_id(channel_id: Optional[str]) -> Optional[str]:
        if not channel_id:
            return None
        try:
            channel = guild.get_channel(int(channel_id))
            if channel is None:
                raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found in this server")
            return str(channel.id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Channel id must be numeric")

    ticket_id = validate_channel_id(payload.ticket_log_channel_id)
    suggestion_id = validate_channel_id(payload.suggestion_log_channel_id)

    # Merge into existing settings
    try:
        existing_resp = supabase.table('server_config').select('settings').eq('guild_id', str(guild_id)).maybe_single().execute()
        current = (existing_resp.data or {}).get('settings') if isinstance(existing_resp, object) else None
        if not isinstance(current, dict):
            current = {}
        if ticket_id is not None:
            current['ticket_log_channel_id'] = ticket_id
        if suggestion_id is not None:
            current['suggestion_log_channel_id'] = suggestion_id

        supabase.table('server_config').upsert({
            "guild_id": str(guild_id),
            "settings": current
        }).execute()
        return {"message": "Logging settings updated", "settings": current}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api.get("/servers/{guild_id}/members/{member_id}/activity")
async def get_member_activity(guild_id: int, member_id: int):
    await bot_ready.wait()
    await get_guild_or_404(guild_id) 
    
    try:
        response = supabase.table('activity_log').select('*').eq('user_id', str(member_id)).order('created_at', desc=True).limit(25).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api.get("/servers/{guild_id}/channels/{channel_id}/members")
async def get_channel_members(guild_id: int, channel_id: int):
    await bot_ready.wait()
    guild = await get_guild_or_404(guild_id)
    channel = guild.get_channel(channel_id)

    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found.")

    if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
        return [] # Return empty list for categories or other non-member channels

    return [{'id': str(m.id), 'name': m.name, 'nick': m.nick, 'avatar_url': str(m.display_avatar.url)} for m in channel.members]

# NEW: Endpoint to get recent messages from a specific channel
@api.get("/servers/{guild_id}/channels/{channel_id}/activity")
async def get_channel_activity(guild_id: int, channel_id: int):
    await bot_ready.wait()
    guild = await get_guild_or_404(guild_id)
    channel = guild.get_channel(channel_id)

    if not channel or not isinstance(channel, discord.TextChannel):
        raise HTTPException(status_code=404, detail="Text channel not found.")

    activity = []
    try:
        async for message in channel.history(limit=25):
            activity.append({
                "id": str(message.id),
                "author_name": message.author.name,
                "author_id": str(message.author.id),
                "content": message.content,
                "timestamp": message.created_at.isoformat()
            })
        return activity
    except discord.Forbidden:
        raise HTTPException(status_code=403, detail="Bot does not have permission to read this channel's history.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api.post("/servers/{guild_id}/members/{member_id}/warn")
async def warn_member(guild_id: int, member_id: int, reason: str = "No reason provided", ctx: Dict[str, Any] = Depends(moderator_context_dependency)):
    await bot_ready.wait()
    guild = await get_guild_or_404(guild_id)
    member = guild.get_member(member_id)
    if not member:
        raise HTTPException(status_code=404, detail="Member not found.")

    # Require at least manage_messages to issue a warning
    permissions = ctx.get("discord", {}).get("permissions", {})
    if not (permissions.get("manage_messages") or permissions.get("kick_members") or permissions.get("ban_members") or permissions.get("administrator")):
        raise HTTPException(status_code=403, detail="Insufficient permissions to warn members")
    moderator_id = int(ctx["discord"]["id"]) 
    try:
        supabase.table('warnings').insert({
            "guild_id": str(guild_id),
            "user_id": str(member_id),
            "moderator_id": str(moderator_id),
            "reason": reason
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to log warning: {e}")
    try:
        await member.send(f"You have been warned in {guild.name} for: {reason}")
    except Exception:
        pass
    return {"message": "User warned successfully."}

@api.post("/servers/{guild_id}/members/{member_id}/ban")
async def ban_member(guild_id: int, member_id: int, reason: str = "No reason provided", ctx: Dict[str, Any] = Depends(moderator_context_dependency)):
    await bot_ready.wait()
    guild = await get_guild_or_404(guild_id)
    member = guild.get_member(member_id)
    if not member:
        raise HTTPException(status_code=404, detail="Member not found.")
    # Require ban_members or administrator
    permissions = ctx.get("discord", {}).get("permissions", {})
    if not (permissions.get("ban_members") or permissions.get("administrator")):
        raise HTTPException(status_code=403, detail="Insufficient permissions to ban members")
    try:
        await member.ban(reason=reason)
        # Log ban in Supabase
        supabase.table('bans').insert({
            "guild_id": str(guild_id),
            "user_id": str(member_id),
            "reason": reason
        }).execute()
        return {"message": "User banned successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to ban user: {e}")

@api.get("/auth/me")
async def auth_me(guild_id: Optional[int] = None, creds: Optional[HTTPAuthorizationCredentials] = Security(security)):
    user = await get_current_user(creds)
    payload: Dict[str, Any] = {
        "supabase_user": {
            "id": str(getattr(user, "id", "")),
            "email": getattr(user, "email", None),
        }
    }
    if guild_id is not None:
        ctx = await get_moderator_context(guild_id, creds)
        payload.update({"discord": ctx["discord"]})
    else:
        discord_id = _extract_discord_id_from_supabase_user(user)
        if discord_id:
            payload["discord"] = {"id": str(discord_id)}
    return payload

@api.post("/servers/{guild_id}/roles/sync")
async def sync_roles(guild_id: int, ctx: Dict[str, Any] = Depends(moderator_context_dependency)):
    await bot_ready.wait()
    guild = await get_guild_or_404(guild_id)

    rows = []
    for r in guild.roles:
        rows.append({
            "guild_id": str(guild_id),
            "role_id": str(r.id),
            "name": r.name,
            "color": str(r.color),
            "position": int(r.position),
            "permissions": int(r.permissions.value),
            "mentionable": bool(r.mentionable),
            "managed": bool(r.managed),
            "updated_at": discord.utils.utcnow().isoformat()
        })

    # Upsert by composite primary key (guild_id, role_id)
    supabase.table("server_roles").upsert(rows).execute()
    return {"synced": len(rows)}

@api.head("/health")
@api.get("/health")
def health_check():
    return {"status": "ok"}

# --- ON_GUILD_JOIN EVENT ---
# This function runs automatically whenever the bot is added to a new server.
@bot.event
async def on_guild_join(guild: discord.Guild):
    print(f"Joined a new server: {guild.name} (ID: {guild.id})")
    
    # --- Public Channel Setup ---
    public_channel = guild.get_channel(REPORT_ISSUE_CHANNEL_ID)
    if public_channel is None:
        # Fallback by name if the specific channel id is missing in this guild
        public_channel = discord.utils.get(guild.text_channels, name="report-an-issue")
    if public_channel is None:
        public_channel = await guild.create_text_channel("report-an-issue")
    await public_channel.purge(check=lambda m: m.author.id == bot.user.id)
    public_embed = discord.Embed(title="Support & Reports", description="Use the buttons below to interact with the support system.", color=discord.Color.blue())
    await public_channel.send(embed=public_embed, view=PublicControls())

    # --- Admin Channel Setup ---
    admin_channel = discord.utils.get(guild.text_channels, name="admin-controls")
    if admin_channel is None:
        overwrites = { guild.default_role: discord.PermissionOverwrite(read_messages=False), guild.me: discord.PermissionOverwrite(read_messages=True) }
        for role in guild.roles:
            if role.permissions.administrator: overwrites[role] = discord.PermissionOverwrite(read_messages=True)
        admin_channel = await guild.create_text_channel("admin-controls", overwrites=overwrites)
    await admin_channel.purge(check=lambda m: m.author.id == bot.user.id)
    admin_embed = discord.Embed(title="Admin Control Panel", description="Use the buttons below to manage the server.", color=discord.Color.red())
    await admin_channel.send(embed=admin_embed, view=AdminControlsView())

    # --- Logging Channels Setup ---
    # Ticket logs
    ticket_logs = discord.utils.get(guild.text_channels, name="ticket-logs")
    if ticket_logs is None:
        try:
            ticket_logs = await guild.create_text_channel("ticket-logs")
        except discord.Forbidden:
            ticket_logs = None

    # Suggestion logs disabled by request (no creation)

# --- ON_READY EVENT ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    
    # Add the persistent view so its buttons work after a restart.
    bot.add_view(PublicControls())
    bot.add_view(AdminReportControls())
    bot.add_view(AdminControlsView())
    
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    print('------')

    bot_ready.set()
    print("Bot is ready and API endpoints are now active.")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    
    try:
        supabase.table('server_activity_logs').insert({
            "guild_id": str(message.guild.id) if message.guild else None,
            "user_id": str(message.author.id),
            "username": message.author.name,
            "action": "sent_message",
            "content": message.content,
            "details": f"in #{message.channel.name}" if hasattr(message, "channel") else None
        }).execute()
    except Exception as e:
        print(f"Error logging message activity: {e}")

    try:
        if isinstance(message.channel, discord.Thread) and message.channel.owner_id == bot.user.id:
            schedule_ticket_timer(message.channel)
    except Exception:
        pass

    await bot.process_commands(message)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return

    guild_id = str(member.guild.id)
    if before.channel is None and after.channel is not None:
        supabase.table("server_activity_logs").insert({
            "guild_id": guild_id,
            "user_id": str(member.id),
            "activity_type": "voice_join",
            "channel_id": str(after.channel.id),
        }).execute()

    # User left a voice channel
    elif before.channel is not None and after.channel is None:
        supabase.table("server_activity_logs").insert({
            "guild_id": guild_id,
            "user_id": str(member.id),
            "activity_type": "voice_leave",
            "channel_id": str(before.channel.id),
        }).execute()

    # User switched channels
    elif before.channel != after.channel:
        supabase.table("server_activity_logs").insert([{
            "guild_id": guild_id,
            "user_id": str(member.id),
            "activity_type": "voice_leave",
            "channel_id": str(before.channel.id),
        }, {
            "guild_id": guild_id,
            "user_id": str(member.id),
            "activity_type": "voice_join",
            "channel_id": str(after.channel.id),
        }]).execute()

@bot.event
async def on_presence_update(before, after):
    """Fires when a member's status changes and broadcasts it."""
    if before.status != after.status:
        print(f"Presence Update: {after.name} is now {after.status}")
        payload = {
            "type": "presence_update",
            "user_id": str(after.id),
            "status": str(after.status)
        }
        await manager.broadcast(payload)

# --- ADMIN SLASH COMMANDS (Your existing commands) ---
@bot.tree.command(name="kick", description="Kicks a member from the server.")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: Interaction, member: discord.Member, *, reason: str = "No reason provided"):
    await member.kick(reason=reason)
    await interaction.response.send_message(f'Kicked {member.mention}. Reason: {reason}')

@bot.tree.command(name="ban", description="Bans a member from the server.")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: Interaction, member: discord.Member, *, reason: str = "No reason provided"):
    await member.ban(reason=reason)
    await interaction.response.send_message(f'Banned {member.mention}. Reason: {reason}')

@bot.tree.command(name="clear", description="Clears a specified number of messages.")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: Interaction, amount: int):
    await interaction.response.defer(ephemeral=True) 
    deleted_messages = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f'Cleared {len(deleted_messages)} messages.', ephemeral=True)


# --- ERROR HANDLING ---
async def on_app_command_error(interaction: Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You don't have the required permissions to run this command.", ephemeral=True)
    else:
        await interaction.response.send_message("An unexpected error occurred.", ephemeral=True)
        print(f"An error occurred: {error}")

bot.tree.on_error = on_app_command_error
