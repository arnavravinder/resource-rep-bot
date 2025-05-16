import os
import logging
import asyncio
import datetime
from typing import Dict, List, Optional
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('resource_bot')

ADMIN_USERS = [123456789012345678]
COOLDOWN_SECONDS = 60 * 60
rep_cooldowns = {}
afk_users = {}

try:
    cred = credentials.Certificate({
        "type": "service_account",
        "project_id": os.getenv('FIREBASE_PROJECT_ID'),
        "private_key_id": os.getenv('FIREBASE_PRIVATE_KEY_ID'),
        "private_key": os.getenv('FIREBASE_PRIVATE_KEY').replace("\\n", "\n"),
        "client_email": os.getenv('FIREBASE_CLIENT_EMAIL'),
        "client_id": os.getenv('FIREBASE_CLIENT_ID'),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": os.getenv('FIREBASE_CLIENT_CERT_URL')
    })
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    resources_collection = db.collection('resources')
    channels_collection = db.collection('channels')
    warnings_collection = db.collection('warnings')
    afk_collection = db.collection('afk')
    logger.info("Firebase initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Firebase: {e}")
    raise

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

resource_triggers = ['thanks', 'ty', 'tysm', 'thank you', 'appreciated', 'thx', 'helpful']

def contains_trigger_word(content: str) -> bool:
    content_lower = content.lower()
    for trigger in resource_triggers:
        start_pos = 0
        while True:
            pos = content_lower.find(trigger, start_pos)
            if pos == -1:
                break
            before_pos = pos - 1
            after_pos = pos + len(trigger)
            before_ok = before_pos < 0 or content_lower[before_pos].isspace() or not content_lower[before_pos].isalnum()
            after_ok = after_pos >= len(content_lower) or content_lower[after_pos].isspace() or not content_lower[after_pos].isalnum()
            if before_ok and after_ok:
                return True
            start_pos = pos + 1
    return False

async def add_resource(guild_id: str, user_id: str, channel_id: str, channel_name: str, given_by: str) -> bool:
    try:
        user_doc_id = f"{guild_id}_{user_id}"
        user_ref = resources_collection.document(user_doc_id)
        channel_doc_id = f"{guild_id}_{channel_id}"
        channel_ref = channels_collection.document(channel_doc_id)
        
        user_doc = user_ref.get()
        if not user_doc.exists:
            user_ref.set({
                'guild_id': guild_id,
                'user_id': user_id,
                'count': 1,
                'channels': {channel_id: {'name': channel_name, 'count': 1}},
                'given_by': {given_by: 1}
            })
        else:
            user_data = user_doc.to_dict()
            new_count = user_data.get('count', 0) + 1
            channels = user_data.get('channels', {})
            if channel_id in channels:
                channels[channel_id]['count'] = channels[channel_id].get('count', 0) + 1
                channels[channel_id]['name'] = channel_name
            else:
                channels[channel_id] = {'name': channel_name, 'count': 1}
            given_by_dict = user_data.get('given_by', {})
            given_by_dict[given_by] = given_by_dict.get(given_by, 0) + 1
            user_ref.update({
                'count': new_count,
                'channels': channels,
                'given_by': given_by_dict
            })
        
        channel_doc = channel_ref.get()
        if not channel_doc.exists:
            channel_ref.set({
                'guild_id': guild_id,
                'channel_id': channel_id,
                'channel_name': channel_name,
                'users': {user_id: 1},
                'total_resources': 1
            })
        else:
            channel_data = channel_doc.to_dict()
            new_total = channel_data.get('total_resources', 0) + 1
            users = channel_data.get('users', {})
            users[user_id] = users.get(user_id, 0) + 1
            channel_ref.update({
                'channel_name': channel_name,
                'total_resources': new_total,
                'users': users
            })
        
        return True
    except Exception as e:
        logger.error(f"Error adding resource: {e}")
        return False

async def get_profile(guild_id: str, user_id: str) -> Dict:
    try:
        doc_id = f"{guild_id}_{user_id}"
        doc = resources_collection.document(doc_id).get()
        if not doc.exists:
            return {'user_id': user_id, 'guild_id': guild_id, 'count': 0, 'channels': {}, 'given_by': {}}
        return doc.to_dict()
    except Exception as e:
        logger.error(f"Error getting profile: {e}")
        return {'user_id': user_id, 'guild_id': guild_id, 'count': 0, 'channels': {}, 'given_by': {}}

async def get_leaderboard(guild_id: str, limit: int = 10, channel_id: Optional[str] = None) -> List[Dict]:
    try:
        if channel_id:
            channel_doc_id = f"{guild_id}_{channel_id}"
            channel_doc = channels_collection.document(channel_doc_id).get()
            if not channel_doc.exists:
                return []
            channel_data = channel_doc.to_dict()
            users = channel_data.get('users', {})
            user_list = [{'user_id': user_id, 'count': count} for user_id, count in users.items()]
            user_list.sort(key=lambda x: x['count'], reverse=True)
            return user_list[:limit]
        else:
            query = (resources_collection
                    .where('guild_id', '==', guild_id)
                    .order_by('count', direction=firestore.Query.DESCENDING)
                    .limit(limit))
            docs = query.stream()
            return [doc.to_dict() for doc in docs]
    except Exception as e:
        logger.error(f"Error getting leaderboard: {e}")
        return []

async def add_warning(guild_id: str, user_id: str, reason: str, mod_id: str) -> bool:
    try:
        warning_id = f"{guild_id}_{user_id}_{datetime.datetime.now().timestamp()}"
        warnings_collection.document(warning_id).set({
            'guild_id': guild_id,
            'user_id': user_id,
            'reason': reason,
            'mod_id': mod_id,
            'timestamp': firestore.SERVER_TIMESTAMP
        })
        return True
    except Exception as e:
        logger.error(f"Error adding warning: {e}")
        return False

async def get_warnings(guild_id: str, user_id: str) -> List[Dict]:
    try:
        query = (warnings_collection
                .where('guild_id', '==', guild_id)
                .where('user_id', '==', user_id))
        docs = query.stream()
        return [doc.to_dict() for doc in docs]
    except Exception as e:
        logger.error(f"Error getting warnings: {e}")
        return []

async def clear_warnings(guild_id: str, user_id: str) -> bool:
    try:
        query = (warnings_collection
                .where('guild_id', '==', guild_id)
                .where('user_id', '==', user_id))
        docs = query.stream()
        batch = db.batch()
        for doc in docs:
            batch.delete(doc.reference)
        batch.commit()
        return True
    except Exception as e:
        logger.error(f"Error clearing warnings: {e}")
        return False

async def set_afk(guild_id: str, user_id: str, reason: str = None) -> bool:
    try:
        doc_id = f"{guild_id}_{user_id}"
        afk_collection.document(doc_id).set({
            'guild_id': guild_id,
            'user_id': user_id,
            'reason': reason,
            'timestamp': firestore.SERVER_TIMESTAMP
        })
        afk_users[user_id] = reason or "No reason provided"
        return True
    except Exception as e:
        logger.error(f"Error setting AFK: {e}")
        return False

async def remove_afk(guild_id: str, user_id: str) -> bool:
    try:
        doc_id = f"{guild_id}_{user_id}"
        afk_collection.document(doc_id).delete()
        if user_id in afk_users:
            del afk_users[user_id]
        return True
    except Exception as e:
        logger.error(f"Error removing AFK: {e}")
        return False

def is_on_cooldown(user_id: int) -> bool:
    if user_id not in rep_cooldowns:
        return False
    last_time = rep_cooldowns[user_id]
    now = datetime.datetime.now().timestamp()
    return (now - last_time) < COOLDOWN_SECONDS

def update_cooldown(user_id: int):
    rep_cooldowns[user_id] = datetime.datetime.now().timestamp()

def format_cooldown(seconds: int) -> str:
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if seconds > 0 and not hours:
        parts.append(f"{seconds}s")
    return " ".join(parts)

class LeaderboardView(View):
    def __init__(self, bot, guild_id: str, channel_id: Optional[str] = None):
        super().__init__(timeout=60)
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.page = 1
        self.entries_per_page = 10
        
    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: Button):
        if self.page > 1:
            self.page -= 1
            await interaction.response.defer()
            await self.update_leaderboard(interaction)
        else:
            await interaction.response.send_message("Already on first page", ephemeral=True)
    
    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: Button):
        self.page += 1
        await interaction.response.defer()
        leaderboard = await get_leaderboard(self.guild_id, limit=self.entries_per_page * self.page, channel_id=self.channel_id)
        if len(leaderboard) < self.entries_per_page * (self.page - 1) + 1:
            self.page -= 1
            await interaction.followup.send("End of leaderboard reached", ephemeral=True)
        else:
            await self.update_leaderboard(interaction)
    
    async def update_leaderboard(self, interaction: discord.Interaction):
        embed = discord.Embed(title="📚 Resource Repository Leaderboard", color=discord.Color.gold())
        leaderboard = await get_leaderboard(self.guild_id, limit=self.entries_per_page * self.page, channel_id=self.channel_id)
        offset = (self.page - 1) * self.entries_per_page
        page_entries = leaderboard[offset:offset + self.entries_per_page] if offset < len(leaderboard) else []
        
        if not page_entries:
            embed.add_field(name="No entries found", value="Be the first to contribute!", inline=False)
        else:
            for i, entry in enumerate(page_entries, offset + 1):
                user_id = entry.get('user_id')
                count = entry.get('count', 0)
                member = interaction.guild.get_member(int(user_id)) if user_id else None
                name = member.display_name if member else f"User {user_id}"
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                embed.add_field(name=f"{medal} {name}", value=f"**{count}** contribution{'s' if count != 1 else ''}", inline=False)
        
        embed.set_footer(text=f"Page {self.page} • Updated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
        await interaction.edit_original_response(embed=embed, view=self)

@bot.event
async def on_ready():
    logger.info(f"Bot is online as {bot.user.name}")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="resource sharing"))
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    
    user_id = str(message.author.id)
    guild_id = str(message.guild.id)
    
    if user_id in afk_users:
        await remove_afk(guild_id, user_id)
        await message.channel.send(f"Welcome back, {message.author.mention}! I've removed your AFK status.", delete_after=10)
    
    for user in message.mentions:
        mention_id = str(user.id)
        if mention_id in afk_users:
            reason = afk_users[mention_id]
            await message.channel.send(f"{user.display_name} is currently AFK: {reason}")
    
    await bot.process_commands(message)
    
    if not message.mentions:
        return
        
    if not contains_trigger_word(message.content):
        return
    
    if is_on_cooldown(message.author.id):
        return
        
    valid_mentions = [user for user in message.mentions if user.id != message.author.id and not user.bot]
    if not valid_mentions:
        return
        
    successful_mentions = []
    for user in valid_mentions:
        result = await add_resource(
            str(message.guild.id),
            str(user.id),
            str(message.channel.id),
            message.channel.name,
            str(message.author.id)
        )
        if result:
            successful_mentions.append(user)
    
    if successful_mentions:
        mentions_text = ", ".join(user.mention for user in successful_mentions)
        await message.channel.send(f"📚 {message.author.mention} acknowledged {mentions_text} for their helpful contribution!")
        update_cooldown(message.author.id)

@bot.tree.command(name="sync", description="Sync commands to this server")
async def sync_command(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You need administrator permission to use this command", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await bot.tree.sync(guild=discord.Object(id=interaction.guild.id))
        await interaction.followup.send("Commands synced to this server!")
    except Exception as e:
        await interaction.followup.send(f"Failed to sync commands: {str(e)}")

@bot.tree.command(name="rep", description="Acknowledge someone's helpful contribution")
@app_commands.describe(user="User to acknowledge", reason="Optional reason")
async def rep_command(interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = None):
    if user.id == interaction.user.id:
        await interaction.response.send_message("You cannot acknowledge yourself", ephemeral=True)
        return
    if user.bot:
        await interaction.response.send_message("You cannot acknowledge bots", ephemeral=True)
        return
    if is_on_cooldown(interaction.user.id):
        await interaction.response.send_message(f"You're on cooldown. Try again later.", ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=False)
    result = await add_resource(
        str(interaction.guild_id),
        str(user.id),
        str(interaction.channel_id),
        interaction.channel.name,
        str(interaction.user.id)
    )
    
    if result:
        update_cooldown(interaction.user.id)
        reason_text = f" for: {reason}" if reason else ""
        await interaction.followup.send(f"📚 {interaction.user.mention} acknowledged {user.mention}'s contribution{reason_text}!")
    else:
        await interaction.followup.send("Failed to add resource. Please try again later.", ephemeral=True)

@bot.tree.command(name="profile", description="View a user's contribution profile")
@app_commands.describe(user="User to view (default: yourself)")
async def profile_command(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    target_user = user or interaction.user
    await interaction.response.defer(ephemeral=False)
    
    profile = await get_profile(str(interaction.guild_id), str(target_user.id))
    
    embed = discord.Embed(title=f"📚 Resource Profile: {target_user.display_name}", color=target_user.color or discord.Color.blue())
    embed.set_thumbnail(url=target_user.display_avatar.url)
    
    rep_count = profile.get('count', 0)
    embed.add_field(name="Total Contributions", value=f"**{rep_count}** acknowledgment{'s' if rep_count != 1 else ''}", inline=False)
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="leaderboard", description="View contribution leaderboard")
async def leaderboard_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    view = LeaderboardView(bot, str(interaction.guild_id))
    embed = discord.Embed(title="📚 Resource Repository Leaderboard", color=discord.Color.gold())
    
    leaderboard = await get_leaderboard(str(interaction.guild_id), limit=10)
    
    if not leaderboard:
        embed.add_field(name="No entries found", value="Be the first to contribute!", inline=False)
    else:
        for i, entry in enumerate(leaderboard, 1):
            user_id = entry.get('user_id')
            count = entry.get('count', 0)
            member = interaction.guild.get_member(int(user_id)) if user_id else None
            name = member.display_name if member else f"User {user_id}"
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            embed.add_field(name=f"{medal} {name}", value=f"**{count}** contribution{'s' if count != 1 else ''}", inline=False)
    
    embed.set_footer(text=f"Page 1 • Updated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    await interaction.followup.send(embed=embed, view=view)

@bot.tree.command(name="afk", description="Set your AFK status")
@app_commands.describe(reason="Reason for being AFK")
async def afk_command(interaction: discord.Interaction, reason: Optional[str] = None):
    await interaction.response.defer(ephemeral=False)
    
    user_id = str(interaction.user.id)
    guild_id = str(interaction.guild_id)
    
    await set_afk(guild_id, user_id, reason)
    
    reason_text = f": {reason}" if reason else ""
    await interaction.followup.send(f"🔄 {interaction.user.mention} is now AFK{reason_text}")
    
    try:
        if interaction.guild.me.guild_permissions.manage_nicknames and interaction.user.guild_permissions.change_nickname:
            current_nick = interaction.user.display_name
            if not current_nick.startswith("[AFK] "):
                new_nick = f"[AFK] {current_nick}"[:32]  # Discord nickname limit
                await interaction.user.edit(nick=new_nick)
    except:
        pass

@bot.tree.command(name="warn", description="Warn a user (Mod only)")
@app_commands.describe(user="User to warn", reason="Reason for warning")
async def warn_command(interaction: discord.Interaction, user: discord.Member, reason: str):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You don't have permission to use this command", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=False)
    
    result = await add_warning(str(interaction.guild_id), str(user.id), reason, str(interaction.user.id))
    
    if result:
        embed = discord.Embed(title="⚠️ Warning Issued", color=discord.Color.yellow())
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=f"Warned on {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
        
        await interaction.followup.send(embed=embed)
        
        try:
            await user.send(f"You were warned in {interaction.guild.name} for: {reason}")
        except:
            pass
    else:
        await interaction.followup.send("Failed to warn user. Please try again.", ephemeral=True)

@bot.tree.command(name="warnings", description="View a user's warnings")
@app_commands.describe(user="User to check")
async def warnings_command(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.moderate_members and interaction.user.id != user.id:
        await interaction.response.send_message("You don't have permission to view others' warnings", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=False)
    
    warnings = await get_warnings(str(interaction.guild_id), str(user.id))
    
    embed = discord.Embed(title=f"Warnings for {user.display_name}", color=discord.Color.orange())
    
    if not warnings:
        embed.description = "This user has no warnings! 🎉"
    else:
        for i, warning in enumerate(warnings, 1):
            mod_id = warning.get('mod_id')
            mod = interaction.guild.get_member(int(mod_id)) if mod_id else None
            mod_name = mod.display_name if mod else "Unknown Moderator"
            
            timestamp = warning.get('timestamp')
            if isinstance(timestamp, firestore.SERVER_TIMESTAMP):
                time_str = "Recent"
            else:
                time_str = timestamp.strftime('%Y-%m-%d %H:%M') if timestamp else "Unknown"
                
            embed.add_field(
                name=f"Warning #{i}",
                value=f"**Reason:** {warning.get('reason')}\n**By:** {mod_name}\n**When:** {time_str}",
                inline=False
            )
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="clearwarnings", description="Clear all warnings for a user (Mod only)")
@app_commands.describe(user="User to clear warnings for")
async def clearwarnings_command(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You don't have permission to use this command", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=False)
    
    result = await clear_warnings(str(interaction.guild_id), str(user.id))
    
    if result:
        await interaction.followup.send(f"All warnings for {user.mention} have been cleared.")
    else:
        await interaction.followup.send("Failed to clear warnings. Please try again.", ephemeral=True)

@bot.tree.command(name="kick", description="Kick a user from the server (Mod only)")
@app_commands.describe(user="User to kick", reason="Reason for kick")
async def kick_command(interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = None):
    if not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message("You don't have permission to use this command", ephemeral=True)
        return
    
    if user.top_role >= interaction.user.top_role:
        await interaction.response.send_message("You cannot kick someone with a role higher than or equal to yours", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=False)
    
    try:
        await user.send(f"You were kicked from {interaction.guild.name}" + (f" for: {reason}" if reason else ""))
    except:
        pass
    
    try:
        await user.kick(reason=reason)
        
        embed = discord.Embed(title="👢 User Kicked", color=discord.Color.red())
        embed.add_field(name="User", value=f"{user.name}#{user.discriminator}", inline=True)
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
            
        await interaction.followup.send(embed=embed)
    except:
        await interaction.followup.send("Failed to kick user. Check my permissions and try again.", ephemeral=True)

@bot.tree.command(name="ban", description="Ban a user from the server (Mod only)")
@app_commands.describe(user="User to ban", reason="Reason for ban", delete_days="Number of days of messages to delete (0-7)")
async def ban_command(interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = None, delete_days: Optional[int] = 1):
    if not interaction.user.guild_permissions.ban_members:
        await interaction.response.send_message("You don't have permission to use this command", ephemeral=True)
        return
    
    if user.top_role >= interaction.user.top_role:
        await interaction.response.send_message("You cannot ban someone with a role higher than or equal to yours", ephemeral=True)
        return
    
    if delete_days < 0 or delete_days > 7:
        await interaction.response.send_message("Delete days must be between 0 and 7", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=False)
    
    try:
        await user.send(f"You were banned from {interaction.guild.name}" + (f" for: {reason}" if reason else ""))
    except:
        pass
    
    try:
        await user.ban(reason=reason, delete_message_days=delete_days)
        
        embed = discord.Embed(title="🔨 User Banned", color=discord.Color.dark_red())
        embed.add_field(name="User", value=f"{user.name}#{user.discriminator}", inline=True)
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
            
        await interaction.followup.send(embed=embed)
    except:
        await interaction.followup.send("Failed to ban user. Check my permissions and try again.", ephemeral=True)

@bot.tree.command(name="unban", description="Unban a user (Mod only)")
@app_commands.describe(user_id="ID of the user to unban")
async def unban_command(interaction: discord.Interaction, user_id: str):
    if not interaction.user.guild_permissions.ban_members:
        await interaction.response.send_message("You don't have permission to use this command", ephemeral=True)
        return
    
    try:
        user_id = int(user_id)
    except ValueError:
        await interaction.response.send_message("Invalid user ID. Please provide a valid user ID.", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=False)
    
    try:
        user = await bot.fetch_user(user_id)
        await interaction.guild.unban(user)
        
        embed = discord.Embed(title="🔓 User Unbanned", color=discord.Color.green())
        embed.add_field(name="User", value=f"{user.name}#{user.discriminator}", inline=True)
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
            
        await interaction.followup.send(embed=embed)
    except discord.NotFound:
        await interaction.followup.send("User not found. Please check the ID and try again.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to unban users.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"An error occurred: {str(e)}", ephemeral=True)

@bot.tree.command(name="timeout", description="Timeout a user (Mod only)")
@app_commands.describe(user="User to timeout", duration="Duration in minutes", reason="Reason for timeout")
async def timeout_command(interaction: discord.Interaction, user: discord.Member, duration: int, reason: Optional[str] = None):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You don't have permission to use this command", ephemeral=True)
        return
    
    if user.top_role >= interaction.user.top_role:
        await interaction.response.send_message("You cannot timeout someone with a role higher than or equal to yours", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=False)
    
    try:
        timeout_duration = datetime.timedelta(minutes=duration)
        await user.timeout_for(timeout_duration, reason=reason)
        
        embed = discord.Embed(title="⏰ User Timed Out", color=discord.Color.orange())
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        embed.add_field(name="Duration", value=f"{duration} minute{'s' if duration != 1 else ''}", inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
            
        await interaction.followup.send(embed=embed)
        
        try:
            await user.send(f"You have been timed out in {interaction.guild.name} for {duration} minute{'s' if duration != 1 else ''}" + (f": {reason}" if reason else "."))
        except:
            pass
    except:
        await interaction.followup.send("Failed to timeout user. Check my permissions and try again.", ephemeral=True)

@bot.tree.command(name="clear", description="Clear messages in a channel (Mod only)")
@app_commands.describe(amount="Number of messages to delete (1-100)")
async def clear_command(interaction: discord.Interaction, amount: int):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("You don't have permission to use this command", ephemeral=True)
        return
    
    if amount < 1 or amount > 100:
        await interaction.response.send_message("Amount must be between 1 and 100", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"Deleted {len(deleted)} message{'s' if len(deleted) != 1 else ''}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to delete messages.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"An error occurred: {str(e)}", ephemeral=True)

if __name__ == "__main__":
    bot.run(os.getenv('DISCORD_TOKEN'))