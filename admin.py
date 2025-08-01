import discord
from discord import app_commands, ui, ButtonStyle, Interaction
from discord.ext import commands
import os
from dotenv import load_dotenv
from supabase import create_client, Client
import asyncio 
from typing import List
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware 
from pydantic import BaseModel
load_dotenv()

TOKEN = os.getenv('BOT_TOKEN')
S_URL = os.getenv('SUPABASE_URL')
S_KEY = os.getenv('SUPABASE_KEY')

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

async def lifespan(app: FastAPI):
    """Handles the bot's startup and shutdown."""
    print("API starting up...")
    asyncio.create_task(bot.start(TOKEN))
    yield
    print("API shutting down, closing bot connection...")
    await bot.close()

api = FastAPI(lifespan=lifespan)

api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Your Vue app's origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ServerSettings(BaseModel):
    prefix: str
    welcome_message: str
    log_channel: str
    auto_role: str

DEFAULT_SETTINGS = {
    "prefix": "!",
    "welcome_message": "Welcome to the server!",
    "log_channel": "bot-logs",
    "auto_role": "Member"
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
        await interaction.response.send_modal(ReportModal(category=category))

class SuggestionModal(ui.Modal, title="Make a Suggestion"):
    suggestion_input = ui.TextInput(label="What is your suggestion?", style=discord.TextStyle.paragraph, required=True, max_length=1000)
    async def on_submit(self, interaction: Interaction):
        try:
            suggestion_data = {
                'suggester_id': str(interaction.user.id), 'suggester_name': interaction.user.name,
                'suggestion': self.suggestion_input.value
            }
            supabase.table('suggestions').insert(suggestion_data).execute()
            await interaction.response.send_message("Thank you for your suggestion!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message("Sorry, there was an error submitting your suggestion.", ephemeral=True)
            print(f"Error inserting suggestion: {e}")

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


class PublicControls(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @ui.button(label="File a Report", style=ButtonStyle.danger, custom_id="file_report_button")
    async def file_report(self, interaction: Interaction, button: ui.Button):
        await interaction.response.send_message("Please select a category for your report.", view=CategorySelectView(), ephemeral=True)
    @ui.button(label="Open Support Ticket", style=ButtonStyle.primary, custom_id="open_ticket_button")
    async def open_ticket(self, interaction: Interaction, button: ui.Button):
        thread = await interaction.channel.create_thread(name=f"support-{interaction.user.name}", type=discord.ChannelType.private_thread)
        await interaction.response.send_message(f"A private support ticket has been created for you: {thread.mention}", ephemeral=True)
        await thread.send(f"Hello {interaction.user.mention}, how can the admin team assist you today?")
    @ui.button(label="Make a Suggestion", style=ButtonStyle.secondary, custom_id="make_suggestion_button")
    async def make_suggestion(self, interaction: Interaction, button: ui.Button):
        await interaction.response.send_modal(SuggestionModal())

class AdminControlsView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ui.Button(label="View Web Dashboard", style=ButtonStyle.link, url="http://localhost:5173/"))

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

# NEW: Endpoint to list all servers the bot is in
@api.get("/servers")
async def get_servers():
    """Returns a list of all servers (guilds) the bot is connected to."""
    await bot_ready.wait()
    server_list = []
    for guild in bot.guilds:
        server_list.append({
            "id": str(guild.id),
            "name": guild.name,
            "icon_url": str(guild.icon.url) if guild.icon else None
        })
    return server_list

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
    return [{'id': str(r.id), 'name': r.name, 'color': str(r.color)} for r in guild.roles if r.name != "@everyone"]


# NEW: Endpoint to get server settings
@api.get("/servers/{guild_id}/settings")
async def get_settings(guild_id: int):
    await bot_ready.wait()
    guild = await get_guild_or_404(guild_id)
    
    response = supabase.table('server_config').select('settings').eq('guild_id', str(guild_id)).single().execute()
    
    if response.data:
        return response.data['settings']
    return DEFAULT_SETTINGS

# NEW: Endpoint to save server settings
@api.post("/servers/{guild_id}/settings")
async def save_settings(guild_id: int, settings: ServerSettings):
    await bot_ready.wait()
    guild = await get_guild_or_404(guild_id)

    try:
        supabase.table('server_config').upsert({
            "guild_id": str(guild_id),
            "settings": settings.dict()
        }).execute()
        return {"message": "Settings saved successfully."}
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

# --- ON_GUILD_JOIN EVENT ---
# This function runs automatically whenever the bot is added to a new server.
@bot.event
async def on_guild_join(guild: discord.Guild):
    print(f"Joined a new server: {guild.name} (ID: {guild.id})")
    
    # --- Public Channel Setup ---
    public_channel = discord.utils.get(guild.text_channels, name="support-and-reports")
    if public_channel is None: public_channel = await guild.create_text_channel("support-and-reports")
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
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    
    try:
        supabase.table('activity_log').insert({
            "guild_id": str(message.guild.id),
            "user_id": str(message.author.id),
            "username": message.author.name,
            "action": "sent_message",
            "details": f"in #{message.channel.name}"
        }).execute()
    except Exception as e:
        print(f"Error logging message activity: {e}")

    await bot.process_commands(message)

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    action, details = None, None
    if not before.channel and after.channel:
        action = "joined_vc"
        details = f"#{after.channel.name}"
    elif before.channel and not after.channel:
        action = "left_vc"
        details = f"#{before.channel.name}"

    if action:
        try:
            supabase.table('activity_log').insert({
                "guild_id": str(member.guild.id),
                "user_id": str(member.id),
                "username": member.name,
                "action": action,
                "details": details
            }).execute()
        except Exception as e:
            print(f"Error logging voice activity: {e}")

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
