import discord
from discord.ext import commands, tasks
import requests
from dotenv import load_dotenv
from datetime import datetime, time, timedelta, timezone # Importiere timezone
import os
import json
import traceback
import asyncio
from collections import defaultdict 
import pytz # FÜR KORREKTE BERLIN-ZEITZONE (CET/CEST)

# --- 1. SETUP ---
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

BERLIN_TZ = pytz.timezone('Europe/Berlin')
UTC_TZ = pytz.utc

# WICHTIG: voice_states & members für den Mover
intents = discord.Intents.default()
intents.message_content = True 
intents.voice_states = True 
intents.members = True 

bot = commands.Bot(command_prefix='!', intents=intents)

# --- KONFIGURATION NEUE FEATURES ---
AFK_CHANNEL_ID = 1451345520881701029  # <--- DEINE VOICE-ID HIER EINTRAGEN
AFK_TIMEOUT_MINUTES = 3
deaf_users = {} # Speicher für Ursprungschannels

# --- 2. ORIGINAL API-LOGIK (Unverändert) ---
def get_event_state(event):
    now_local = datetime.now(BERLIN_TZ)
    closest_future_slot_time = None
    FOUR_HOURS_IN_SECONDS = 4 * 60 * 60 
    
    for slot in event.get('times', []):
        try:
            start_str = slot['start'].replace('24:00', '00:00')
            end_str = slot['end'].replace('24:00', '00:00')
            start_t = datetime.strptime(start_str, "%H:%M").time()
            end_t = datetime.strptime(end_str, "%H:%M").time()

            for day_offset in [-1, 0, 1]: 
                utc_date = datetime.now(UTC_TZ).date() + timedelta(days=day_offset)
                current_slot_start = UTC_TZ.localize(datetime.combine(utc_date, start_t)).astimezone(BERLIN_TZ)
                current_slot_end = UTC_TZ.localize(datetime.combine(utc_date, end_t)).astimezone(BERLIN_TZ)
                if start_t >= end_t and day_offset != -1:
                    current_slot_end += timedelta(days=1)
                
                if current_slot_end < now_local: continue
                if current_slot_start <= now_local < current_slot_end:
                    time_remaining = current_slot_end - now_local
                    m, s = divmod(int(time_remaining.total_seconds()), 60)
                    h, m = divmod(m, 60)
                    return "ACTIVE", f"Endet in: {h}h {m}m" if h > 0 else f"Endet in: {m}m {s}s"
                
                if current_slot_start > now_local:
                    if closest_future_slot_time is None or current_slot_start < closest_future_slot_time:
                        closest_future_slot_time = current_slot_start
        except: continue
            
    if closest_future_slot_time:
        time_remaining = closest_future_slot_time - now_local
        if time_remaining.total_seconds() <= FOUR_HOURS_IN_SECONDS:
            m, s = divmod(int(time_remaining.total_seconds()), 60)
            h, m = divmod(m, 60)
            return "NEXT", f"Startet in: {h}h {m}m" if h > 0 else f"Startet in: {m}m"
    return "NONE", "Keine relevanten Events."

def get_arc_raiders_events():
    try:
        r = requests.get("https://metaforge.app/api/arc-raiders/event-timers", timeout=10)
        return r.json().get('data', [])
    except: return []

def format_single_event_embed(event_data):
    name = event_data.get('name', 'Event')
    state, time_info = get_event_state(event_data)
    color = discord.Color.green() if state == "ACTIVE" else discord.Color.orange() if state == "NEXT" else discord.Color.dark_grey()
    embed = discord.Embed(title=f"⚔️ {name}", description=f"Status: **{time_info}**", color=color)
    if event_data.get('icon'): embed.set_thumbnail(url=event_data['icon'])
    return embed

# --- 3. FIX: MAP-TIMER LOGIK ---
@bot.command(name='map-timer')
async def map_timer(ctx):
    events = get_arc_raiders_events()
    if not events: return await ctx.send("Konnte Daten nicht laden.")
    
    maps = defaultdict(lambda: {"active": [], "next": []})
    for e in events:
        m_name = e.get('map', 'Andere')
        state, t_info = get_event_state(e)
        if state == "ACTIVE": maps[m_name]["active"].append(f"• {e.get('name')} ({t_info.split(': ')[-1]})")
        elif state == "NEXT": maps[m_name]["next"].append(f"• {e.get('name')} ({t_info.split('in: ')[-1]})")

    embed = discord.Embed(title="🌍 Map-Timer Status", color=discord.Color.blue())
    for m, data in maps.items():
        val = ""
        if data["active"]: val += "**AKTIV:**\n" + "\n".join(data["active"]) + "\n"
        if data["next"]: val += "**BALD:**\n" + "\n".join(data["next"])
        embed.add_field(name=f"📍 {m}", value=val or "Keine Events.", inline=False)
    await ctx.send(embed=embed)

# --- 4. FORMATIERUNGS-FUNKTIONEN ---
def format_single_event_embed(event_data):
    name = event_data.get('name', 'Unbekanntes Event')
    map_location = event_data.get('map', 'Ort?')
    icon_url = event_data.get('icon')
    state, time_info = get_event_state(event_data) 
    tz_abbreviation = datetime.now(BERLIN_TZ).strftime('%Z')
    
    if state == "ACTIVE":
        color = discord.Color.green()
        status_text = "🟢 AKTIV"
        description = f"📍 Ort: {map_location}\n🔥 Status: **{time_info}**"
    elif state == "NEXT":
        color = discord.Color.orange()
        status_text = "🟡 KOMMT BALD"
        description = f"📍 Ort: {map_location}\n⏱️ Nächster Start: **{time_info}**"
    else:
        color = discord.Color.dark_grey()
        status_text = "⚪ NICHT RELEVANT"
        description = f"📍 Ort: {map_location}\n❌ Status: **{time_info}**"

    embed = discord.Embed(title=f"⚔️ {name} | {status_text}", description=description, color=color)
    if icon_url:
        embed.set_thumbnail(url=icon_url)
    
    current_berlin_time = datetime.now(BERLIN_TZ).strftime('%H:%M:%S')
    embed.set_footer(text=f"Daten von MetaForge | Aktuelle Berlin-Zeit ({tz_abbreviation}): {current_berlin_time}")
    return embed

def format_map_status_embed(map_data):
    tz_abbreviation = datetime.now(BERLIN_TZ).strftime('%Z')
    embed = discord.Embed(
        title=f"🌍 Map-Timer Status (Berlin-Zeit - {tz_abbreviation})",
        description="Übersicht der aktiven und bald startenden Events (unter 4h) pro Map.",
        color=discord.Color.blue()
    )
    
    for map_location in sorted(map_data.keys()):
        status = map_data[map_location]
        field_value = ""
        if status["active_events"]:
            field_value += f"🟢 **AKTIV:**\n" + "\n".join(status["active_events"]) + "\n"
        if status["next_events"]:
            field_value += f"🟡 **KOMMT BALD:**\n" + "\n".join(status["next_events"])
            
        embed.add_field(name=f"📍 {map_location}", value=field_value or "⚪ Keine Events.", inline=False)
        
    embed.set_footer(text=f"Aktuelle Berlin-Zeit ({tz_abbreviation}): {datetime.now(BERLIN_TZ).strftime('%H:%M:%S')}")
    return embed

# --- 5. NEU: AUTO-VOICE-MOVER (Verbessert) --- #
@tasks.loop(seconds=10)
async def check_voice_afk():
    now = datetime.now()
    for guild in bot.guilds:
        afk_channel = guild.get_channel(AFK_CHANNEL_ID)
        if not afk_channel: continue
        for member in guild.members:
            if member.bot or not member.voice or not member.voice.channel:
                if member.id in deaf_users: del deaf_users[member.id]
                continue

            is_deafened = member.voice.self_deaf or member.voice.deaf
            if is_deafened and member.voice.channel.id != AFK_CHANNEL_ID:
                if member.id not in deaf_users:
                    deaf_users[member.id] = {"timestamp": now, "origin_id": member.voice.channel.id}
                elif now - deaf_users[member.id]["timestamp"] >= timedelta(minutes=AFK_TIMEOUT_MINUTES):
                    try: await member.move_to(afk_channel)
                    except: pass
            elif not is_deafened and member.id in deaf_users:
                origin = guild.get_channel(deaf_users[member.id]["origin_id"])
                if member.voice.channel.id == AFK_CHANNEL_ID and origin:
                    try: await member.move_to(origin)
                    except: pass
                del deaf_users[member.id]

# --- 6. WEITERE BEFEHLE ---
@bot.command(name='clear')
@commands.has_permissions(manage_messages=True)
async def clear(ctx):
    await ctx.channel.purge(limit=11)
    await ctx.send("✅ 10 Nachrichten gelöscht.", delete_after=3)

@bot.event
async def on_ready():
    print(f'🤖 {bot.user.name} ist online!')
    await bot.change_presence(activity=discord.Activity(name="!timer | !map-timer", type=discord.ActivityType.watching))

@bot.command(name='timer')
async def show_timers(ctx):
    events_list = get_arc_raiders_events() 
    if not events_list:
        return await ctx.send("Konnte keine Daten abrufen.")
    
    tracked_events = {} 
    def get_priority(state):
        return 0 if state == "ACTIVE" else 1 if state == "NEXT" else 2

    for event in events_list:
        name = event.get('name')
        if not name: continue
        state, _ = get_event_state(event)
        prio = get_priority(state)
        if prio < 2 and (name not in tracked_events or prio < tracked_events[name][0]):
            tracked_events[name] = (prio, event)

    sorted_events = [v[1] for v in sorted(tracked_events.values(), key=lambda x: (x[0], x[1].get('name')))][:10]
    if not sorted_events:
        return await ctx.send("Aktuell keine Events im 4h-Fenster.")

    for event in sorted_events:
        try:
            await ctx.send(embed=format_single_event_embed(event))
            await asyncio.sleep(0.5)
        except: continue

@bot.command(name='map-timer')
async def show_map_status(ctx):
    map_data = get_map_data() 
    if map_data:
        await ctx.send(embed=format_map_status_embed(map_data))

@bot.command(name='queen')
async def show_queen_meta(ctx):
    if os.path.exists("Queen.png"):
        await ctx.send("👑 **Meta Equipment: Matriarch/Queen**", file=discord.File("Queen.png"))
    else:
        await ctx.send("Bilddatei nicht gefunden.")

@bot.command(name='info')
async def show_info(ctx):
    embed = discord.Embed(title="ℹ️ Befehle", color=discord.Color.green())
    embed.add_field(name="!timer", value="Events im 4h-Fenster.", inline=False)
    embed.add_field(name="!map-timer", value="Status pro Map.", inline=False)
    embed.add_field(name="!queen", value="Queen-Meta Guide.", inline=False)
    await ctx.send(embed=embed)

@bot.event
async def on_ready():
    print(f'🤖 {bot.user.name} online!')
    if not check_voice_afk.is_running():
        check_voice_afk.start()

if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)


