import discord
from discord.ext import commands, tasks
import requests
from dotenv import load_dotenv
from datetime import datetime, timedelta
import os
import traceback 
import asyncio
from collections import defaultdict 
import pytz 

# --- 1. KONFIGURATION & ZEITZONEN ---
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

# Zeitzonen-Definitionen
UTC_TZ = pytz.utc
BERLIN_TZ = pytz.timezone('Europe/Berlin')

# Mute-System Konfiguration
# BITTE HIER DIE ID DEINES VOID/AFK CHANNELS EINTRAGEN
MUTE_AFK_CHANNEL_ID = 000000000000000000 

# Tracking für Mute-Dauer
# Format: {user_id: {"start_time": datetime, "orig_channel_id": int, "already_moved": bool}}
mute_tracker = {}

# Bot Setup
intents = discord.Intents.default()
intents.message_content = True 
intents.voice_states = True # WICHTIG für das Mute-System
intents.members = True      # WICHTIG um Member zu finden
bot = commands.Bot(command_prefix='!', intents=intents)

# --- 2. HILFSFUNKTION: EVENT TIMER (UTC -> Berlin) ---
def get_event_state(event):
    now_local = datetime.now(BERLIN_TZ)
    closest_future_slot_time = None
    FOUR_HOURS_IN_SECONDS = 4 * 60 * 60 
    
    for slot in event.get('times', []):
        try:
            start_str = slot['start']
            end_str = slot['end']

            if start_str == '24:00': start_str = '00:00'
            if end_str == '24:00': end_str = '00:00' 
                
            start_t = datetime.strptime(start_str, "%H:%M").time()
            end_t = datetime.strptime(end_str, "%H:%M").time()

            for day_offset in [-1, 0, 1]: 
                utc_date = datetime.now(UTC_TZ).date() + timedelta(days=day_offset)
                
                # API Zeit als UTC interpretieren
                current_slot_start_utc = UTC_TZ.localize(datetime.combine(utc_date, start_t))
                current_slot_end_utc = UTC_TZ.localize(datetime.combine(utc_date, end_t))
                
                # In Berlin Zeit umrechnen
                current_slot_start = current_slot_start_utc.astimezone(BERLIN_TZ)
                current_slot_end = current_slot_end_utc.astimezone(BERLIN_TZ)

                if start_t >= end_t and day_offset != -1:
                    current_slot_end += timedelta(days=1)
                
                if current_slot_end < now_local:
                    continue
                    
                if current_slot_start <= now_local < current_slot_end:
                    time_remaining = current_slot_end - now_local
                    minutes, _ = divmod(int(time_remaining.total_seconds()), 60)
                    hours, minutes = divmod(minutes, 60)
                    time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
                    return "ACTIVE", f"Endet in: {time_str}"
                
                if current_slot_start > now_local:
                    if closest_future_slot_time is None or current_slot_start < closest_future_slot_time:
                        closest_future_slot_time = current_slot_start
        except:
            continue
            
    if closest_future_slot_time:
        time_remaining = closest_future_slot_time - now_local
        if time_remaining.total_seconds() <= FOUR_HOURS_IN_SECONDS:
            minutes, _ = divmod(int(time_remaining.total_seconds()), 60)
            hours, minutes = divmod(minutes, 60)
            time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
            return "NEXT", f"Startet in: {time_str} (um {closest_future_slot_time.strftime('%H:%M')} {closest_future_slot_time.strftime('%Z')})"
    
    return "NONE", "Keine Events in Kürze."

# --- 3. MUTE-SYSTEM TASKS & EVENTS ---

@tasks.loop(seconds=10)
async def check_mute_timeout():
    """Prüft alle 10 Sekunden, wer zu lange stumm ist."""
    now = datetime.now()
    for user_id in list(mute_tracker.keys()):
        data = mute_tracker[user_id]
        if data["already_moved"]: continue

        duration = (now - data["start_time"]).total_seconds()
        if duration >= 180: # 3 Minuten
            for guild in bot.guilds:
                member = guild.get_member(user_id)
                if member and member.voice and member.voice.channel:
                    if member.voice.channel.id != MUTE_AFK_CHANNEL_ID:
                        try:
                            target = bot.get_channel(MUTE_AFK_CHANNEL_ID)
                            await member.move_to(target)
                            mute_tracker[user_id]["already_moved"] = True
                            print(f"[Mute] {member.name} in AFK verschoben.")
                        except: pass
                    break

@bot.event
async def on_voice_state_update(member, before, after):
    """Überwacht Stummschaltung und verschiebt zurück beim Entstummen."""
    if member.bot: return

    is_muted = after.self_mute or after.mute
    was_muted = before.self_mute or before.mute

    # Fall: Neu stumm
    if is_muted and not was_muted:
        if after.channel and after.channel.id != MUTE_AFK_CHANNEL_ID:
            mute_tracker[member.id] = {
                "start_time": datetime.now(),
                "orig_channel_id": after.channel.id,
                "already_moved": False
            }

    # Fall: Entstummt
    elif not is_muted and was_muted:
        if member.id in mute_tracker:
            data = mute_tracker[member.id]
            if data["already_moved"] and after.channel and after.channel.id == MUTE_AFK_CHANNEL_ID:
                try:
                    orig = bot.get_channel(data["orig_channel_id"])
                    if orig: await member.move_to(orig)
                except: pass
            mute_tracker.pop(member.id, None)

    # Fall: Verlässt Voice
    if before.channel and not after.channel:
        mute_tracker.pop(member.id, None)

# --- 4. API & BOT COMMANDS ---

def get_arc_raiders_events():
    try:
        r = requests.get("https://metaforge.app/api/arc-raiders/event-timers", timeout=10)
        return r.json().get('data', [])
    except: return []

@bot.event
async def on_ready():
    print(f'🤖 {bot.user.name} online!')
    if not check_mute_timeout.is_running():
        check_mute_timeout.start()

@bot.command(name='timer')
async def show_timers(ctx):
    events = get_arc_raiders_events()
    if not events: return await ctx.send("Fehler beim Laden der Daten.")
    
    active_found = False
    for event in events:
        state, time_info = get_event_state(event)
        if state in ["ACTIVE", "NEXT"]:
            active_found = True
            color = discord.Color.green() if state == "ACTIVE" else discord.Color.orange()
            embed = discord.Embed(title=f"{event['name']} - {state}", description=time_info, color=color)
            if event.get('icon'): embed.set_thumbnail(url=event['icon'])
            await ctx.send(embed=embed)
            await asyncio.sleep(0.5)
    
    if not active_found: await ctx.send("Aktuell keine Events in den nächsten 4h.")

@bot.command(name='map-timer')
async def show_map_status(ctx):
    events = get_arc_raiders_events()
    maps = defaultdict(lambda: {"active": [], "next": []})
    
    for e in events:
        state, info = get_event_state(e)
        if state == "ACTIVE": maps[e['map']]["active"].append(f"• {e['name']} ({info.split(': ')[-1]})")
        elif state == "NEXT": maps[e['map']]["next"].append(f"• {e['name']} ({info.split('in: ')[-1]})")

    embed = discord.Embed(title="🌍 Map Status", color=discord.Color.blue())
    for m, data in maps.items():
        val = ""
        if data["active"]: val += "**Aktiv:**\n" + "\n".join(data["active"]) + "\n"
        if data["next"]: val += "**Demnächst:**\n" + "\n".join(data["next"])
        embed.add_field(name=f"📍 {m}", value=val or "Keine Events", inline=False)
    await ctx.send(embed=embed)

@bot.command(name='queen')
async def show_queen(ctx):
    if os.path.exists("Queen.png"):
        await ctx.send("👑 **Meta Equipment: Matriarch**", file=discord.File("Queen.png"))
    else: await ctx.send("Datei Queen.png fehlt.")

if DISCORD_TOKEN: bot.run(DISCORD_TOKEN)
