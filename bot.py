import discord
from discord.ext import commands, tasks
import requests
from datetime import datetime, timedelta
import os
import asyncio
import pytz 

# --- 1. KONFIGURATION ---

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
UTC_TZ = pytz.utc
BERLIN_TZ = pytz.timezone('Europe/Berlin')
MUTE_AFK_CHANNEL_ID = 1341339526992236604
mute_tracker = {}

intents = discord.Intents.default()
intents.message_content = True 
intents.voice_states = True 
intents.members = True      
bot = commands.Bot(command_prefix='!', intents=intents)

# --- 2. ZEIT- & FORMATIERUNGS-LOGIK ---

def get_event_state(event):
    """Berechnet Status und Zeit (UTC -> Berlin)."""
    now_local = datetime.now(BERLIN_TZ)
    closest_future = None
    FOUR_HOURS = 4 * 60 * 60 
    
    for slot in event.get('times', []):
        try:
            s_str = slot['start'].replace('24:00', '00:00')
            e_str = slot['end'].replace('24:00', '00:00')
            s_time = datetime.strptime(s_str, "%H:%M").time()
            e_time = datetime.strptime(e_str, "%H:%M").time()

            for day_offset in [-1, 0, 1]: 
                utc_date = datetime.now(UTC_TZ).date() + timedelta(days=day_offset)
                start_utc = UTC_TZ.localize(datetime.combine(utc_date, s_time))
                end_utc = UTC_TZ.localize(datetime.combine(utc_date, e_time))
                
                start_berlin = start_utc.astimezone(BERLIN_TZ)
                end_berlin = end_utc.astimezone(BERLIN_TZ)

                if s_time >= e_time and day_offset != -1:
                    end_berlin += timedelta(days=1)
                
                if end_berlin < now_local: continue
                    
                if start_berlin <= now_local < end_berlin:
                    diff = end_berlin - now_local
                    m, _ = divmod(int(diff.total_seconds()), 60)
                    h, m = divmod(m, 60)
                    t_str = f"{h}h {m}m" if h > 0 else f"{m}m {diff.seconds%60}s"
                    return "ACTIVE", f"Endet in: {t_str}"
                
                if start_berlin > now_local:
                    if closest_future is None or start_berlin < closest_future:
                        closest_future = start_berlin
        except: continue
            
    if closest_future:
        diff = closest_future - now_local
        if diff.total_seconds() <= FOUR_HOURS:
            m, _ = divmod(int(diff.total_seconds()), 60)
            h, m = divmod(m, 60)
            t_str = f"{h}h {m}m" if h > 0 else f"{m}m"
            return "NEXT", f"Startet in: {t_str} (um {closest_future.strftime('%H:%M')} {closest_future.strftime('%Z')})"
    
    return "NONE", "Keine Events."

def format_event_embed(event, state, info):
    """Erstellt das schicke Embed (wie im alten Code)."""
    tz_abbr = datetime.now(BERLIN_TZ).strftime('%Z')
    current_time = datetime.now(BERLIN_TZ).strftime('%H:%M:%S')
    
    if state == "ACTIVE":
        color = discord.Color.green()
        status_text = "🟢 AKTIV"
        desc = f"📍 Ort: {event.get('map')}\n🔥 Status: **{info}**"
    else: # NEXT
        color = discord.Color.orange()
        status_text = "🟡 KOMMT BALD"
        desc = f"📍 Ort: {event.get('map')}\n⏱️ Nächster Start: **{info}**"

    embed = discord.Embed(title=f"⚔️ {event.get('name')} | {status_text}", description=desc, color=color)
    if event.get('icon'): embed.set_thumbnail(url=event.get('icon'))
    embed.set_footer(text=f"Daten von MetaForge | Aktuelle Berlin-Zeit ({tz_abbr}): {current_time}")
    return embed

# --- 3. MUTE-SYSTEM & COMMANDS ---

@tasks.loop(seconds=10)
async def check_mute_timeout():
    now = datetime.now()
    for uid, data in list(mute_tracker.items()):
        if data["moved"]: continue
        if (now - data["start"]).total_seconds() >= 180:
            for g in bot.guilds:
                m = g.get_member(uid)
                if m and m.voice and m.voice.channel and m.voice.channel.id != MUTE_AFK_CHANNEL_ID:
                    try: 
                        await m.move_to(bot.get_channel(MUTE_AFK_CHANNEL_ID))
                        mute_tracker[uid]["moved"] = True
                    except: pass

@bot.event
async def on_voice_state_update(m, before, after):
    if m.bot: return
    is_muted = after.self_mute or after.mute
    was_muted = before.self_mute or before.mute

    if is_muted and not was_muted:
        if after.channel and after.channel.id != MUTE_AFK_CHANNEL_ID:
            mute_tracker[m.id] = {"start": datetime.now(), "orig": after.channel.id, "moved": False}
    elif not is_muted and was_muted:
        if m.id in mute_tracker:
            data = mute_tracker[m.id]
            if data["moved"] and after.channel and after.channel.id == MUTE_AFK_CHANNEL_ID:
                try: await m.move_to(bot.get_channel(data["orig"]))
                except: pass
            mute_tracker.pop(m.id, None)
    if before.channel and not after.channel: mute_tracker.pop(m.id, None)

@bot.event
async def on_ready():
    if not check_mute_timeout.is_running(): check_mute_timeout.start()

@bot.command(name='timer')
async def timer(ctx):
    try:
        data = requests.get("https://metaforge.app/api/arc-raiders/event-timers", timeout=10).json().get('data', [])
    except: return await ctx.send("API-Fehler.")
    
    events_to_show = []
    for e in data:
        state, info = get_event_state(e)
        if state in ["ACTIVE", "NEXT"]:
            events_to_show.append((0 if state=="ACTIVE" else 1, e['name'], e, state, info))
            
    if not events_to_show: return await ctx.send("Keine Events in den nächsten 4h.")
    
    # Sortieren und auf 10 begrenzen
    events_to_show.sort()
    top_events = events_to_show[:10]
    
    await ctx.send(f"**Lade Statusblöcke für {len(top_events)} aktive/bald startende Events...**")
    for _, _, event, state, info in top_events:
        await ctx.send(embed=format_event_embed(event, state, info))
        await asyncio.sleep(0.5)

if DISCORD_TOKEN: bot.run(DISCORD_TOKEN)
