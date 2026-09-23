"""
==========================================================
 SMC PRO v3 — Interactive yfinance SMC Telegram Bot
 أزرار تفاعلية للتحكم بالزوج (XAUUSD / GBPUSD) والفريم (1m / 5m)
 جاهز للرفع والاستضافة على Render.com
==========================================================
"""

import os
import time
import threading
import datetime
import http.server
import socketserver
import numpy as np
import pandas as pd
import yfinance as yf
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ================== قراءة الإعدادات من البيئة ==================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8608249128:AAEzBeoDp6TOXgznLh8AFwUx56iASws9yC8")
TELEGRAM_CHATID = os.getenv("TELEGRAM_CHATID", "6062624259")

# حفظ إعدادات المستخدمين بناءً على ID الدردشة لتجنب التضارب
USER_SETTINGS = {
    "DEFAULT": {
        "SYMBOL": "XAUUSD",
        "INTERVAL": "1m"
    }
}

LOOKBACK       = 200
SWING_LEN      = 5
MIN_RR         = 2.0
SWEEP_LOOKBACK = 30
CHECK_EVERY    = 15  # 15 ثانية لتفادي حظر yfinance

# قاموس تحويل الرموز لصيغة yfinance
SYMBOL_MAP = {
    "XAUUSD": "GC=F",
    "GBPUSD": "GBPUSD=X"
}

HTF_MAPPING = {"1m": "5m", "5m": "15m"}

# تهيئة البوت
bot = telebot.TeleBot(TELEGRAM_TOKEN)

def get_user_config(chat_id):
    chat_str = str(chat_id)
    if chat_str not in USER_SETTINGS:
        USER_SETTINGS[chat_str] = {
            "SYMBOL": USER_SETTINGS["DEFAULT"]["SYMBOL"],
            "INTERVAL": USER_SETTINGS["DEFAULT"]["INTERVAL"]
        }
    return USER_SETTINGS[chat_str]

# ---------------- جلب البيانات عبر yfinance ----------------
def get_klines(symbol, interval, limit=200):
    yf_symbol = SYMBOL_MAP.get(symbol, symbol)
    period = "1d" if interval in ["1m", "2m", "5m"] else "5d"
    
    ticker = yf.Ticker(yf_symbol)
    df = ticker.history(period=period, interval=interval)
    
    if df.empty or len(df) < 10:
        raise ValueError(f"لم يتم العثور على بيانات كافية للرمز {symbol} ({yf_symbol})")
    
    df = df.dropna().tail(limit)
    k = df[['Open', 'High', 'Low', 'Close']].to_numpy()
    return k

# ---------------- السوينغات والمفاهيم ----------------
def swings(h, l, n):
    highs, lows = [], []
    for i in range(n, len(h)-n):
        if h[i] == max(h[i-n:i+n+1]): highs.append(i)
        if l[i] == min(l[i-n:i+n+1]): lows.append(i)
    return highs, lows

def htf_trend(h, l, c):
    hi, lo = swings(h, l, SWING_LEN)
    if len(hi) < 2 or len(lo) < 2: return "neutral"
    hh = h[hi[-1]] > h[hi[-2]]
    hl = l[lo[-1]] > l[lo[-2]]
    if hh and hl: return "bull"
    if not hh and not hl: return "bear"
    return "neutral"

def find_fvg(h, l):
    gaps = []
    for i in range(len(h)-2):
        if l[i+2] > h[i]: gaps.append(("bull", h[i], l[i+2]))
        if l[i] > h[i+2]: gaps.append(("bear", h[i+2], l[i]))
    return gaps[-5:]

def find_order_blocks(o, c, h, l, direction):
    obs = []
    for i in range(len(o)-6, 2, -1):
        body      = abs(c[i]   - o[i])
        next_body = abs(c[i+1] - o[i+1])
        if next_body > body * 1.8:
            if direction == "bull" and c[i] < o[i]: obs.append((l[i], h[i]))
            elif direction == "bear" and c[i] > o[i]: obs.append((l[i], h[i]))
    return obs[:3]

def swept_liquidity(o, h, l, c, lookback, swing_n=3):
    hi, lo = swings(h[:-1], l[:-1], swing_n)
    for idx in lo[::-1]:
        lv = l[idx]
        below = np.where(l[idx+1:] < lv)[0]
        if len(below):
            first = idx + 1 + below[0]
            for j in range(first, min(first+4, len(c))):
                if c[j] > lv and c[j] > o[j]:
                    return ("bull", lv, j)
    return None

def premium_discount(l_last, h_last, price):
    eq = (h_last + l_last) / 2
    rng = h_last - l_last
    if rng <= 0: return "neutral", eq
    if price < eq: return "discount", eq
    return "premium", eq

def candle_confirmation(o, h, l, c, direction):
    body  = abs(c[-1] - o[-1])
    rng   = h[-1] - l[-1]
    if rng == 0: return False
    strong = body / rng > 0.55
    if direction == "bull": return strong and c[-1] > o[-1]
    return strong and c[-1] < o[-1]

# ---------------- محرك التحليل ----------------
def analyze(k_entry, k_htf):
    o, h, l, c = k_entry[:,0], k_entry[:,1], k_entry[:,2], k_entry[:,3]
    price = c[-1]

    trend = htf_trend(k_htf[:,1], k_htf[:,2], k_htf[:,3])
    hi, lo = swings(h, l, SWING_LEN)
    if len(hi) < 2 or len(lo) < 2: 
        return (None, price, trend, "neutral")
    
    min_low = l[lo[-2]:lo[-1]].min() if lo[-2] < lo[-1] else l.min()
    zone, eq = premium_discount(min_low, h.max(), price)

    sweep = swept_liquidity(o, h, l, c, SWEEP_LOOKBACK)

    bull_zones = [(g[1], g[2]) for g in find_fvg(h, l) if g[0] == "bull"] \
                 + find_order_blocks(o, c, h, l, "bull")
    bear_zones = [(g[1], g[2]) for g in find_fvg(h, l) if g[0] == "bear"] \
                 + find_order_blocks(o, c, h, l, "bear")

    signal = sl = tp = None

    if trend == "bull" and zone == "discount" and sweep and sweep[0] == "bull":
        sweep_low, sweep_idx = sweep[1], sweep[2]
        for zl, zh in bull_zones:
            if zl <= price <= zh * 1.0015 and sweep_idx >= len(c) - SWEEP_LOOKBACK:
                if candle_confirmation(o, h, l, c, "bull"):
                    cand_sl = min(zl, sweep_low) * 0.9985
                    swing_high = h[hi[-1]]
                    cand_tp = swing_high
                    risk = price - cand_sl
                    reward = cand_tp - price
                    if risk > 0 and reward / risk >= MIN_RR:
                        signal, sl, tp = "BUY", cand_sl, cand_tp
                break

    if signal is None and trend == "bear" and zone == "premium":
        for idx in hi[::-1]:
            hv = h[idx]
            above = np.where(h[idx+1:] > hv)[0]
            if len(above):
                first = idx + 1 + above[0]
                for j in range(first, min(first+4, len(c))):
                    if c[j] < hv and c[j] < o[j]:
                        for zl, zh in bear_zones:
                            if zh >= price >= zl * 0.9985 and candle_confirmation(o, h, l, c, "bear"):
                                cand_sl = max(zh, hv) * 1.0015
                                swing_low = l[lo[-1]]
                                cand_tp = swing_low
                                risk = cand_sl - price
                                reward = price - cand_tp
                                if risk > 0 and reward / risk >= MIN_RR:
                                    signal, sl, tp = "SELL", cand_sl, cand_tp
                                break
                        if signal: break
                break

    if signal:
        return (signal, sl, tp, price, trend, zone)
    return (None, price, trend, zone)

# ---------------- إرسال التنبيهات ----------------
def alert(sig, price, sl, tp, rr, zone, symbol, chat_id):
    icon, name = ("🟢", "شراء BUY") if sig == "BUY" else ("🔴", "بيع SELL")
    msg = (f"\n{icon} **إشارة جديدة: {name}** على `{symbol}`\n\n"
           f"💵 **سعر الدخول:** `{price:.5f}`\n"
           f"🛑 **وقف الخسارة:** `{sl:.5f}` ({abs(price-sl)/price*100:.2f}%)\n"
           f"🎯 **الهدف:** `{tp:.5f}`\n"
           f"⚖️ **نسبة العائد/المخاطرة:** 1:{rr:.1f}\n"
           f"📍 **المنطقة:** {zone}")
    print(msg)
    try:
        bot.send_message(chat_id, msg, parse_mode="Markdown")
    except Exception as e:
        print("خطأ في إرسال التنبيه:", e)

# ---------------- تصميم أزرار التحكم ----------------
def build_settings_keyboard(chat_id):
    cfg = get_user_config(chat_id)
    markup = InlineKeyboardMarkup()
    
    sym_xau = "✅ XAUUSD" if cfg["SYMBOL"] == "XAUUSD" else "XAUUSD"
    sym_gbp = "✅ GBPUSD" if cfg["SYMBOL"] == "GBPUSD" else "GBPUSD"
    btn_xau = InlineKeyboardButton(sym_xau, callback_data="set_sym_XAUUSD")
    btn_gbp = InlineKeyboardButton(sym_gbp, callback_data="set_sym_GBPUSD")
    markup.row(btn_xau, btn_gbp)

    tf_1m = "✅ 1 دقيقة (1m)" if cfg["INTERVAL"] == "1m" else "1 دقيقة (1m)"
    tf_5m = "✅ 5 دقائق (5m)" if cfg["INTERVAL"] == "5m" else "5 دقائق (5m)"
    btn_1m = InlineKeyboardButton(tf_1m, callback_data="set_tf_1m")
    btn_5m = InlineKeyboardButton(tf_5m, callback_data="set_tf_5m")
    markup.row(btn_1m, btn_5m)

    btn_status = InlineKeyboardButton("📊 جلب التحليل اللحظي الآن", callback_data="run_status")
    markup.row(btn_status)

    return markup

# ---------------- أوامر التليجرام التفاعلية ----------------
@bot.message_handler(commands=['start', 'help', 'settings'])
def send_welcome(message):
    cfg = get_user_config(message.chat.id)
    htf = HTF_MAPPING[cfg["INTERVAL"]]
    welcome_text = (
        "⚙️ **لوحة تحكم بوت SMC Monitor (yfinance)**\n\n"
        f"🔹 **الزوج الحالي:** `{cfg['SYMBOL']}`\n"
        f"⏱ **فريم الدخول:** `{cfg['INTERVAL']}` | **الفريم الأعلى:** `{htf}`\n\n"
        "👇 *يمكنك تغيير الزوج والفريم مباشرة عبر الأزرار أدناه:*"
    )
    bot.reply_to(message, welcome_text, reply_markup=build_settings_keyboard(message.chat.id), parse_mode="Markdown")

@bot.message_handler(commands=['status'])
def send_status(message):
    fetch_and_send_status(message.chat.id)

def fetch_and_send_status(chat_id):
    try:
        cfg = get_user_config(chat_id)
        htf = HTF_MAPPING[cfg["INTERVAL"]]

        k  = get_klines(cfg["SYMBOL"], cfg["INTERVAL"], LOOKBACK)
        kh = get_klines(cfg["SYMBOL"], htf, LOOKBACK)
        
        res = analyze(k, kh)
        _, price, trend, zone = res[0], res[1], res[2], res[3]
        
        trend_ar = "صاعد 📈" if trend == "bull" else ("هابط 📉" if trend == "bear" else "محايد ⚖️")
        zone_ar  = "خصم (القاع) 🟢" if zone == "discount" else ("علاوة (القمة) 🔴" if zone == "premium" else "محايدة ⚪")

        status_msg = (
            f"📊 **التقرير اللحظي - {cfg['SYMBOL']}**\n"
            f"----------------------------------------\n"
            f"💰 **السعر الحالي:** `{price:.5f}`$\n"
            f"📈 **اتجاه HTF ({htf}):** {trend_ar}\n"
            f"📍 **المنطقة الحالية:** {zone_ar}\n"
            f"⏱ **فريم الدخول:** {cfg['INTERVAL']}\n"
            f"----------------------------------------\n"
            f"💡 *البوت يراقب التغيرات ويرسل التنبيه تلقائياً عند تحقق الدخول.*"
        )
        bot.send_message(chat_id, status_msg, parse_mode="Markdown", reply_markup=build_settings_keyboard(chat_id))
    except Exception as e:
        bot.send_message(chat_id, f"❌ حدث خطأ أثناء جلب البيانات: {e}")

# ---------------- معالجة النقر على الأزرار (Callback Queries) ----------------
@bot.callback_query_handler(func=lambda call: True)
def callback_listener(call):
    chat_id = call.message.chat.id
    cfg = get_user_config(chat_id)
    
    if call.data == "set_sym_XAUUSD":
        cfg["SYMBOL"] = "XAUUSD"
        bot.answer_callback_query(call.id, "تم التبديل إلى الذهب XAUUSD 🟡")
    elif call.data == "set_sym_GBPUSD":
        cfg["SYMBOL"] = "GBPUSD"
        bot.answer_callback_query(call.id, "تم التبديل إلى الباوند GBPUSD 💷")
    elif call.data == "set_tf_1m":
        cfg["INTERVAL"] = "1m"
        bot.answer_callback_query(call.id, "تم التبديل إلى فريم الدقيقة 1m ⏱")
    elif call.data == "set_tf_5m":
        cfg["INTERVAL"] = "5m"
        bot.answer_callback_query(call.id, "تم التبديل إلى فريم 5 دقائق 5m ⏱")
    elif call.data == "run_status":
        bot.answer_callback_query(call.id, "جاري تحديث البيانات...")
        fetch_and_send_status(chat_id)
        return

    htf = HTF_MAPPING[cfg["INTERVAL"]]
    new_text = (
        "⚙️ **لوحة تحكم بوت SMC Monitor**\n\n"
        f"🔹 **الزوج الحالي:** `{cfg['SYMBOL']}`\n"
        f"⏱ **فريم الدخول:** `{cfg['INTERVAL']}` | **الفريم الأعلى:** `{htf}`\n\n"
        "👇 *يمكنك تغيير الزوج والفريم مباشرة عبر الأزرار أدناه:*"
    )
    try:
        bot.edit_message_text(
            new_text, 
            chat_id, 
            call.message.message_id,
            reply_markup=build_settings_keyboard(chat_id), 
            parse_mode="Markdown"
        )
    except Exception:
        pass

# ---------------- حلقة مراقبة السوق (Multithreading) ----------------
def market_monitor_loop():
    print("=== SMC PRO v3 | البوت يعمل وتفاعلي مع خيارات المستخدم ===")
    last_signal_time = 0
    while True:
        try:
            target_chat_id = TELEGRAM_CHATID
            cfg = get_user_config(target_chat_id)
            htf = HTF_MAPPING[cfg["INTERVAL"]]

            k  = get_klines(cfg["SYMBOL"], cfg["INTERVAL"], LOOKBACK)
            kh = get_klines(cfg["SYMBOL"], htf, LOOKBACK)
            
            res = analyze(k, kh)
            now_str = datetime.datetime.now().strftime("%H:%M:%S")

            if res[0]:
                sig, sl, tp, price, trend, zone = res
                if time.time() - last_signal_time > 3600:
                    last_signal_time = time.time()
                    rr = abs(tp - price) / abs(price - sl)
                    alert(sig, price, sl, tp, rr, zone, cfg["SYMBOL"], target_chat_id)
            else:
                _, price, trend, zone = res
                print(f"[{now_str}] [{cfg['SYMBOL']} | {cfg['INTERVAL']}] السعر: {price:.5f} | الاتجاه: {trend} | المنطقة: {zone}")
            
            time.sleep(CHECK_EVERY)
        except Exception as e:
            print("خطأ في حلقة المراقبة:", e)
            time.sleep(10)

# ---------------- خادم وهمي لمنع إيقاف Render (Keep-Alive Server) ----------------
def run_dummy_server():
    port = int(os.getenv("PORT", 8080))
    handler = socketserver.TCPServer(("", port), http.server.SimpleHTTPRequestHandler)
    handler.serve_forever()

# ---------------- تشغيل التطبيق ----------------
if __name__ == "__main__":
    # تشغيل خادم HTTP في خلفية الخيط للتوافق مع منصات الاستضافة
    threading.Thread(target=run_dummy_server, daemon=True).start()
    
    # تشغيل مراقبة السوق في خيط مستقل
    monitor_thread = threading.Thread(target=market_monitor_loop, daemon=True)
    monitor_thread.start()

    print("جاري الاستماع للأوامر والأزرار من تليجرام...")
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            print(f"حدث خطأ في الاتصال بالبوت، جاري إعادة المحاولة: {e}")
            time.sleep(5)