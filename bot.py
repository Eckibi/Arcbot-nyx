import discord
from discord.ext import commands, tasks
import requests
from datetime import datetime, timedelta
import os
import traceback 
import asyncio
from collections import defaultdict 
import pytz 

# --- 1. KONFIGURATION & TOKEN ---

# Zieht den Token aus den Render Environment Variables
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

# Zeitzonen-Definitionen (UTC für API, Berlin für Anzeige/Vergleich)
UTC_TZ = pytz.utc
BERLIN_TZ = pytz.timezone('Europe/Berlin')

# Mute-System Konfiguration
# BITTE HIER DIE ID DEINES AFK/WAITING VOIP-CHANNELS EINTRAGEN
MUTE_AFK_CHANNEL_ID = 1451345520881701029

# Tracking für Mute-Dauer
mute_tracker = {}

# Bot Setup mit benötigten Berechtigungen (Intents)
intents = discord.Intents.default()
intents.message_content = True 
intents.voice_states = True 
intents.members = True      
bot = commands.Bot(command_prefix='!', intents=intents)

# --- 2. EVENT TIMER LOGIK (UTC -> Berlin) ---

def get_event_state(event):
    """Interpretiert API-Zeiten als UTC und wandelt sie in Berlin-Zeit um."""
    now_local = datetime.now(BERLIN_TZ)
    closest_future_slot_time = None
    FOUR_HOURS_IN_SECONDS = 4 * 60 * 60 
    
    for slot in event.get('times', []):
        try:
            # 24:00 Korrektur für datetime-Objekte
            start_str = slot['start'].replace('24:00', '00:00')
            end_str = slot['end'].replace('24:00', '00:00')
                
            start_t = datetime.strptime(start_str, "%H:%M").time()
            end_t = datetime.strptime(end_str, "%H:%M").time()

            for day_offset in [-1, 0, 1]: 
                utc_date = datetime.now(UTC_TZ).date() + timedelta(days=day_offset)
                
                # API Zeit als UTC festlegen
                start_utc = UTC_TZ.localize(datetime.combine(utc_date, start_t))
                end_utc = UTC_TZ.localize(datetime.combine(utc_date, end_t))
                
                # In Berlin Zeit umrechnen (Sommer/Winterzeit automatisch)
                current_slot_start = start_utc.astimezone(BERLIN_TZ)
                current_slot_end = end_utc.astimezone(BERLIN_TZ)

                if start_t >= end_t and day_offset != -1:
                    current_slot_end += timedelta(days=1)
                
                if current_slot_end < now_local:
                    continue
                    
                if current_slot_start <= now_local < current_slot_end:
                    diff = current_slot_end - now_local
                    m, _ = divmod(int(diff.total_seconds()), 60)
                    h, m = divmod(m, 60)
                    time_str = f"{h}h {m}m" if h > 0 else f"{m}m"
                    return "ACTIVE", f"Endet in: {time_str}"
                
                if current_slot_start > now_local:
                    if closest_future_slot_time is None or current_slot_start < closest_future_slot_time:
                        closest_future_slot_time = current_slot_start
        except: continue
            
    if closest_future_slot_time:
        diff = closest_future_slot_time - now_local
        if diff.total_seconds() <= FOUR_HOURS_IN_SECONDS:
            m, _ = divmod(int(diff.total_seconds()), 60)
            h, m = divmod(m, 60)
            t_str = f"{h}h {m}m" if h > 0 else f"{m}m"
            return "NEXT", f"Startet in: {t_str} (um {closest_future_slot_time.strftime('%H:%M')} {closest_future_slot_time.strftime('%Z')})"
    
    return "NONE", "Keine Events."

# --- 3. MUTE-SYSTEM (Move & Back) ---

@tasks.loop(seconds=10)
async def check_mute_timeout():
    """Prüft, ob User seit 3 Minuten stumm sind."""
    now = datetime.now()
    for user_id in list(mute_tracker.keys()):
        data = mute_tracker[user_id]
        if data["already_moved"]: continue

        if (now - data["start_time"]).total_seconds() >= 180: # 3 Minuten
            for guild in bot.guilds:
                member = guild.get_member(user_id)
                if member and member.voice and member.voice.channel:
                    if member.voice.channel.id != MUTE_AFK_CHANNEL_ID:
                        try:
                            target = bot.get_channel(MUTE_AFK_CHANNEL_ID)
                            if target:
                                await member.move_to(target)
                                mute_tracker[user_id]["already_moved"] = True
                                print(f"[Mute] {member.name} verschoben.")
                        except Exception as e:
                            print(f"Fehler Move: {e}")
                    break

@bot.event
async def on_voice_state_update(member, before, after):
    """Regelt das Tracking und das Zurückschieben beim Entstummen."""
    if member.bot: return

    is_muted = after.self_mute or after.mute
    was_muted = before.self_mute or before.mute

    # Fall 1: User mutet sich
    if is_muted and not was_muted:
        if after.channel and after.channel.id != MUTE_AFK_CHANNEL_ID:
            mute_tracker[member.id] = {
                "start_time": datetime.now(),
                "orig_channel_id": after.channel.id,
                "already_moved": False
            }

    # Fall 2: User entstummt sich
    elif not is_muted and was_muted:
        if member.id in mute_tracker:
            data = mute_tracker[member.id]
            # Schiebe zurück, falls er verschoben wurde
            if data["already_moved"] and after.channel and after.channel.id == MUTE_AFK_CHANNEL_ID:
                try:
                    orig = bot.get_channel(data["orig_channel_id"])
                    if orig: 
                        await member.move_to(orig)
                        print(f"[Mute] {member.name} zurückgeschoben.")
                except: pass
            mute_tracker.pop(member.id, None)

    # Fall 3: User verlässt Voice
    if before.channel and not after.channel:
        mute_tracker.pop(member.id, None)

# --- 4. COMMANDS ---

def fetch_api_data():
    try:
        r = requests.get("https://metaforge.app/api/arc-raiders/event-timers", timeout=10)
        return r.json().get('data', [])
    except: return []

@bot.event
async def on_ready():
    print(f'🤖 {bot.user.name} ist online!')
    if not check_mute_timeout.is_running():
        check_mute_timeout.start()

@bot.command(name='timer')
async def timer(ctx):
    data = fetch_api_data()
    found = False
    for e in data:
        state, info = get_event_state(e)
        if state in ["ACTIVE", "NEXT"]:
            found = True
            embed = discord.Embed(title=f"{e['name']} ({state})", description=info, color=0x2ecc71 if state=="ACTIVE" else 0xe67e22)
            if e.get('icon'): embed.set_thumbnail(url=e['icon'])
            await ctx.send(embed=embed)
    if not found: await ctx.send("Aktuell keine Events in Sicht.")

@bot.command(name='map-timer')
async def map_timer(ctx):
    data = fetch_api_data()
    m_dict = defaultdict(lambda: {"active": [], "next": []})
    for e in data:
        state, info = get_event_state(e)
        if state == "ACTIVE": m_dict[e['map']]["active"].append(f"• {e['name']} ({info.split(': ')[-1]})")
        elif state == "NEXT": m_dict[e['map']]["next"].append(f"• {e['name']} ({info.split('in: ')[-1]})")
    
    embed = discord.Embed(title="🌍 Map-Status Übersicht", color=0x3498db)
    for m, d in m_dict.items():
        v = ""
        if d["active"]: v += "**Aktiv:**\n" + "\n".join(d["active"]) + "\n"
        if d["next"]: v += "**Demnächst:**\n" + "\n".join(d["next"])
        embed.add_field(name=f"📍 {m}", value=v or "Keine aktiven Events", inline=False)
    await ctx.send(embed=embed)

@bot.command(name='queen')
async def queen(ctx):
    if os.path.exists("Queen.png"):
        await ctx.send("👑 **Meta Equipment: Matriarch**", file=discord.File("Queen.png"))
    else: await ctx.send("Datei 'Queen.png' fehlt im Verzeichnis.")

@bot.command(name='info')
async def info(ctx):
    await ctx.send("**Befehle:** !timer, !map-timer, !queen")

# Bot Start
if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)
else:
    print("FEHLER: DISCORD_TOKEN in Render Environment Variables nicht gefunden!")

