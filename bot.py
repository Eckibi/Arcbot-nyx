# Importiere notwendige Bibliotheken
import discord
from discord.ext import commands
import requests
from dotenv import load_dotenv
from datetime import datetime, time, timedelta, timezone
import os
import json
import traceback 
import asyncio
from collections import defaultdict 

# --- 1. VORBEREITUNG & ZEITZONE ---
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

# Zeitzone für Berlin festlegen (CET = UTC+1)
BERLIN_TZ_OFFSET = timedelta(hours=1) 
BERLIN_TZ = timezone(BERLIN_TZ_OFFSET)

# Definiere die Discord Intents
intents = discord.Intents.default()
intents.message_content = True 
bot = commands.Bot(command_prefix='!', intents=intents)

# --- 2. HILFSFUNKTION FÜR ZEITBERECHNUNG (4h-Fenster) ---
def get_event_state(event):
    """
    Bestimmt, ob ein Event aktiv ist, oder wann die nächste Instanz startet (max. 4h im Voraus).
    Berücksichtigt Events, die über Mitternacht hinausgehen (durch day_offset-Korrektur).
    """
    now_utc = datetime.now(timezone.utc)
    closest_future_slot_time = None
    
    FOUR_HOURS_IN_SECONDS = 4 * 60 * 60 
    
    for slot in event.get('times', []):
        try:
            start_str = slot['start']
            end_str = slot['end']

            # Korrektur des 24:00 Fehlers
            if start_str == '24:00':
                start_str = '00:00'
            if end_str == '24:00':
                end_str = '00:00' 
                
            start_t = datetime.strptime(start_str, "%H:%M").time()
            end_t = datetime.strptime(end_str, "%H:%M").time()

            # KORREKTUR: Prüft GESTERN (-1), HEUTE (0) und MORGEN (1)
            # Dies ist entscheidend, um Events zu finden, die z.B. um 02:00 Uhr am nächsten Tag starten.
            for day_offset in [-1, 0, 1]:
                start_date = now_utc.date() + timedelta(days=day_offset)
                
                current_slot_start = datetime.combine(start_date, start_t, tzinfo=timezone.utc)
                current_slot_end = datetime.combine(start_date, end_t, tzinfo=timezone.utc)

                if start_t >= end_t:
                    current_slot_end += timedelta(days=1)
                
                if current_slot_end < now_utc:
                    continue
                    
                if current_slot_start <= now_utc < current_slot_end:
                    time_remaining = current_slot_end - now_utc
                    minutes, seconds = divmod(int(time_remaining.total_seconds()), 60)
                    hours, minutes = divmod(minutes, 60)
                    
                    if hours > 0:
                        time_str = f"{hours}h {minutes}m"
                    else:
                        time_str = f"{minutes}m {seconds}s"
                    
                    return "ACTIVE", f"Endet in: {time_str}"
                
                if current_slot_start > now_utc:
                    if closest_future_slot_time is None or current_slot_start < closest_future_slot_time:
                        closest_future_slot_time = current_slot_start
        
        except Exception as e:
            continue
            
    if closest_future_slot_time:
        time_remaining = closest_future_slot_time - now_utc
        
        if time_remaining.total_seconds() > FOUR_HOURS_IN_SECONDS: 
            return "NONE", "Startet erst später oder morgen."
            
        minutes, seconds = divmod(int(time_remaining.total_seconds()), 60)
        hours, minutes = divmod(minutes, 60)
        
        if hours > 0:
            time_str = f"{hours}h {minutes}m"
        elif minutes > 0:
            time_str = f"{minutes}m {seconds}s"
        else:
            time_str = f"{seconds}s"
            
        berlin_time = closest_future_slot_time.astimezone(BERLIN_TZ)
        absolute_time = berlin_time.strftime("%H:%M")
        
        return "NEXT", f"Startet in: {time_str} (um {absolute_time} CET)"
    
    return "NONE", "Alle Slots für heute sind vorbei oder starten erst in über 4 Stunden."


# --- 3. API-FUNKTIONEN ---
def get_arc_raiders_events():
    """Ruft die Event-Daten ab und gibt die Liste der Events zurück."""
    API_URL = "https://metaforge.app/api/arc-raiders/event-timers" 
    try:
        response = requests.get(API_URL, timeout=10)
        response.raise_for_status() 
        data = response.json()
        return data.get('data', []) 
    except requests.exceptions.RequestException as e:
        print(f"Fehler beim Abrufen der Event-API-Daten: {e}")
        return []

# HINWEIS: Diese Funktion gruppiert Events unter EINEM Map-Namen. Alle Events werden gelistet.
def get_map_data():
    """Ruft die Event-Daten ab und gruppiert aktive/nächste Events pro Map."""
    
    events_list = get_arc_raiders_events()
    
    map_status = defaultdict(lambda: {"active_events": [], "next_events": []})
    
    for event in events_list:
        name = event.get('name')
        map_location = event.get('map')
        
        if not name or not map_location:
            continue
            
        state, time_info = get_event_state(event)
        
        if state in ["ACTIVE", "NEXT"]:
            # Fügt das Event der Liste für diese Map hinzu.
            # Dadurch erscheinen alle Events unter dem einmaligen Map-Namen.
            if state == "ACTIVE":
                time_display = time_info.split(': ')[-1]
                map_status[map_location]["active_events"].append(f"• {name} (Endet in {time_display})")
            elif state == "NEXT":
                time_display = time_info.split('Startet in: ')[-1]
                map_status[map_location]["next_events"].append(f"• {name} ({time_display})")
            
    return dict(map_status)


# --- 4. FORMATIERUNGS-FUNKTIONEN ---

def format_single_event_embed(event_data):
    """Erstellt einen Embed nur für ein einzelnes Event (für !timer)."""
    name = event_data.get('name', 'Unbekanntes Event')
    map_location = event_data.get('map', 'Ort?')
    icon_url = event_data.get('icon')
    
    state, time_info = get_event_state(event_data) 
    
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


    embed = discord.Embed(
        title=f"⚔️ {name} | {status_text}",
        description=description,
        color=color
    )
    
    if icon_url:
        embed.set_thumbnail(url=icon_url)
    
    current_berlin_time = datetime.now(BERLIN_TZ).strftime('%H:%M:%S')
    embed.set_footer(text=f"Daten von MetaForge | Aktuelle Berlin-Zeit (CET): {current_berlin_time}")
    return embed

def format_map_status_embed(map_data):
    
    embed = discord.Embed(
        title="🌍 Map-Timer Status (Berlin-Zeit)",
        description="Übersicht der aktiven und bald startenden Events (unter 4h) pro Map-Location.",
        color=discord.Color.blue()
    )
    
    sorted_maps = sorted(map_data.keys())
    
    # Hier wird jeder Map-Name einmal als Feld-Name verwendet
    for map_location in sorted_maps:
        status = map_data[map_location]
        field_value = ""
        
        # Alle Events unter diesem Map-Namen werden hier aufgelistet
        if status["active_events"]:
            active_list = "\n".join(status["active_events"])
            field_value += f"🟢 **AKTIV:**\n{active_list}\n"
            
        if status["next_events"]:
            next_list = "\n".join(status["next_events"])
            field_value += f"🟡 **KOMMT BALD:**\n{next_list}\n"
            
        if not field_value:
            field_value = "⚪ Keine Events aktiv oder in Kürze geplant."
            
        embed.add_field(
            name=f"📍 {map_location}",
            value=field_value.strip(),
            inline=False
        )
        
    current_berlin_time = datetime.now(BERLIN_TZ).strftime('%H:%M:%S')
    embed.set_footer(text=f"Daten von MetaForge | Aktuelle Berlin-Zeit (CET): {current_berlin_time}")
    return embed


# -------------------------------------------------------------
# --- 5. BOT-BEFEHLE ---
# -------------------------------------------------------------
@bot.event
async def on_ready():
    """Wird ausgeführt, sobald der Bot erfolgreich verbunden ist und setzt den Status."""
    print(f'🤖 {bot.user.name} ist online und bereit!')
    print("----------------------------------------")
    
    activity = discord.Activity(
        name="!timer | !map-timer | !queen", 
        type=discord.ActivityType.watching
    )
    await bot.change_presence(activity=activity)


# Befehl: !timer
@bot.command(name='timer')
async def show_timers(ctx):
    events_list = get_arc_raiders_events() 
    
    if not events_list:
        await ctx.send("Konnte keine Event-Daten abrufen. API ist möglicherweise nicht erreichbar.")
        return
    
    tracked_events = {} 
    def get_priority(state):
        if state == "ACTIVE": return 0
        if state == "NEXT": return 1
        return 2

    for event in events_list:
        name = event.get('name')
        if not name: continue
        
        state, _ = get_event_state(event)
        priority = get_priority(state)
        
        if name not in tracked_events:
            tracked_events[name] = (priority, event)
        else:
            current_priority, _ = tracked_events[name]
            if priority < current_priority:
                tracked_events[name] = (priority, event)

    events_to_display = []
    sorted_tracked_events = sorted(tracked_events.values(), key=lambda x: (x[0], x[1].get('name')))

    for priority, event in sorted_tracked_events:
        if priority < 2: 
            events_to_display.append(event)
            
    limited_events_list = events_to_display[:10]
    
    if not limited_events_list:
        await ctx.send("Zurzeit sind alle Events vorbei oder starten erst in über 4 Stunden.")
        return

    await ctx.send(f"**Lade Statusblöcke für {len(limited_events_list)} aktive/bald startende Events (im 4h-Fenster)...**")
    
    for event in limited_events_list:
        try:
            event_embed = format_single_event_embed(event)
            await ctx.send(embed=event_embed)
            await asyncio.sleep(0.5)
            
        except Exception as e:
            print(f"Fehler beim Senden des Embeds für {event.get('name')}: {e}")
            traceback.print_exc() 

# Befehl: !map-timer
@bot.command(name='map-timer')
async def show_map_status(ctx):
    """Zeigt den aggregierten Status aller Maps basierend nur auf Event-Timern an."""
    
    map_data = get_map_data() 
    
    if not map_data:
        await ctx.send("Konnte keine Event-Daten abrufen.")
        return
    
    map_embed = format_map_status_embed(map_data)
    await ctx.send(embed=map_embed)

# Befehl: !queen
@bot.command(name='queen')
async def show_queen_meta(ctx):
    """Sendet ein Bild von 'Queen.png' mit einem Meta-Equipment-Hinweis für Matriarch/Queen."""
    
    image_path = "Queen.png"
    
    if os.path.exists(image_path):
        try:
            discord_file = discord.File(image_path, filename="Queen.png")
            
            await ctx.send(
                "👑 **Meta Equipment für: Matriarch/Queen** 👑", 
                file=discord_file
            )
        except Exception as e:
            await ctx.send(f"Fehler beim Senden des Bildes: {e}")
            print(f"Fehler beim Senden von Queen.png: {e}")
            traceback.print_exc()
    else:
        await ctx.send(f"Fehler: Die Datei '{image_path}' wurde nicht gefunden. Bitte stellen Sie sicher, dass sie im selben Ordner wie der Bot liegt.")

# Befehl: !info
@bot.command(name='info')
async def show_info(ctx):
    """Listet alle verfügbaren Commands auf."""
    
    info_embed = discord.Embed(
        title="ℹ️ Command-Übersicht",
        description="Alle verfügbaren Befehle für den Arc-Bot:",
        color=discord.Color.green()
    )
    
    info_embed.add_field(
        name="!timer", 
        value="Zeigt den aktuellen Status und die nächsten Startzeiten (< 4h) der **Events** an. (Berlin-Zeit)", 
        inline=False
    )
    
    info_embed.add_field(
        name="!map-timer", 
        value="Zeigt den aggregierten **Status jeder Map** (basierend auf aktiven/kommenden Events < 4h) an. (Berlin-Zeit)", 
        inline=False
    )
    
    info_embed.add_field(
        name="!queen", 
        value="Zeigt Meta-Equipment für den Matriarch/Queen-Boss an (mit Bild).", 
        inline=False
    )
    
    info_embed.add_field(
        name="!info", 
        value="Zeigt diese Command-Liste an.", 
        inline=False
    )
    
    info_embed.set_footer(
        text="Weitere Commands für andere APIs (z.B. Bauzeiten, Map-Status) sind in Planung."
    )
    
    await ctx.send(embed=info_embed)

# --- 7. BOT STARTEN ---
if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)
else:
    print("FEHLER: Der DISCORD_TOKEN wurde nicht in der .env-Datei gefunden.")
