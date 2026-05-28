import os, time, json, logging, threading
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify

# ===== CONFIG =====
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TIMEFRAME = "5m"
COMP_PCT = 0.6
ZONE_PCT = 0.3
POWER_MIN = 0.6
LOOKLEFT = 30
ZONE_LOOKBACK = 10
SLOPE200_LEN = 5
SLOPE20_LEN = 3
SLOPE_THRESH = 0.05
TARGET_R = 5.0
TRAIL_PCT = 0.5
MIN_TRAIL_MFE = 0.01
SWING_VALUES = [(1, "S1"), (3, "S3")]
MIN_STOP_PCT = 0.0005
MAX_STOP_PCT = 0.05

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot_render")

bots = {}
bot_start = time.time()

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
INTERVAL_MAP = {"1m":1,"3m":3,"5m":5,"15m":15,"30m":30,"1h":60,"2h":120,"4h":240,"6h":360,"12h":720,"1d":"D","1w":"W","1M":"M"}

def calc_slope(arr, idx, lookback):
    if idx < lookback or np.isnan(arr[idx]):
        return None
    return (arr[idx] - arr[idx - lookback]) / arr[idx - lookback] * 100

def get_swing_stop(highs, lows, idx, side, lookback):
    start = max(0, idx - lookback + 1)
    if side == 'LONG':
        return float(np.min(lows[start:idx + 1]))
    return float(np.max(highs[start:idx + 1]))

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
        if r.status_code != 200:
            log.warning(f"Telegram: {r.status_code}")
    except Exception as e:
        log.warning(f"Telegram: {e}")

def fetch_klines(symbol, limit=1000):
    for host in ["api.binance.us", "api.binance.com", "fapi.binance.com"]:
        try:
            path = "/fapi/v1/klines" if "fapi" in host else "/api/v3/klines"
            r = requests.get(f"https://{host}{path}", params={"symbol":symbol,"interval":TIMEFRAME,"limit":limit}, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                return r.json()
        except:
            pass
    bybit_int = INTERVAL_MAP.get(TIMEFRAME, 5)
    for _ in range(3):
        try:
            r = requests.get("https://api.bybit.com/v5/market/kline", params={"category":"spot","symbol":symbol,"interval":bybit_int,"limit":limit}, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                d = r.json()
                if d.get("retCode") == 0:
                    k = d["result"]["list"]; k.reverse()
                    return [[int(v[0]),float(v[1]),float(v[2]),float(v[3]),float(v[4]),float(v[5]),float(v[6]),0,0,0,0] for v in k]
        except:
            time.sleep(1)
    raise Exception(f"Falha ao buscar {symbol}")

def check_signals(opens, highs, lows, closes, times, sma20, sma200, i):
    if np.isnan(sma20[i]) or np.isnan(sma200[i]):
        return None
    body = abs(closes[i] - opens[i])
    tr = highs[i] - lows[i]
    if tr == 0 or body / tr < POWER_MIN:
        return None
    lado = "LONG" if closes[i] > opens[i] else "SHORT"
    price = closes[i]
    slope200 = calc_slope(sma200, i, SLOPE200_LEN)
    flat200 = slope200 is not None and abs(slope200) < SLOPE_THRESH
    slope20 = calc_slope(sma20, i, SLOPE20_LEN)
    trending20 = slope20 is not None and abs(slope20) > SLOPE_THRESH
    z_up = sma200[i] * (1 + ZONE_PCT/100)
    z_dn = sma200[i] * (1 - ZONE_PCT/100)
    near = (z_dn <= price <= z_up) or (z_dn <= lows[i] <= z_up) or (z_dn <= opens[i] <= z_up)
    surge = near and ((lado=='LONG' and price>sma200[i] and price>sma20[i]) or (lado=='SHORT' and price<sma200[i] and price<sma20[i]))
    look_idx = max(200, i - LOOKLEFT)
    nothing = False
    if i - look_idx >= 3:
        nothing = price>float(np.max(highs[look_idx:i])) if lado=='LONG' else price<float(np.min(lows[look_idx:i]))
    lo = float(np.min(lows[max(0,i-ZONE_LOOKBACK+1):i+1]))
    hi = float(np.max(highs[max(0,i-ZONE_LOOKBACK+1):i+1]))
    rng_pct = (hi-lo)/lo*100 if lo>0 else 999
    p20 = abs(price-sma20[i])/sma20[i]*100
    compressed = rng_pct < COMP_PCT and p20 < COMP_PCT*0.5
    comp_signal = compressed and ((lado=='LONG' and price>sma20[i]) or (lado=='SHORT' and price<sma20[i]))
    baleia = surge and flat200 and nothing
    if baleia:
        return ('BALEIA_'+('L' if lado=='LONG' else 'S'), lado, price, times[i])
    if surge and not flat200 and trending20:
        return ('SURGE_'+('L' if lado=='LONG' else 'S'), lado, price, times[i])
    if comp_signal:
        return ('COMP_'+('L' if lado=='LONG' else 'S'), lado, price, times[i])
    return None

def bot_loop(symbol, lookback, swing_label):
    sym_short = symbol.replace("USDT", "")
    position = None
    last_signal = {}
    last_heartbeat = 0

    log.info(f"{sym_short} {swing_label} iniciado")

    while True:
        try:
            now = datetime.now(timezone.utc)
            now_ts = int(now.timestamp())

            if now_ts - last_heartbeat >= 3600:
                last_heartbeat = now_ts
                price_str = "?"
                try:
                    k = fetch_klines(symbol, 2)
                    price_str = f"${float(k[-1][4]):,.0f}"
                except:
                    pass
                status = f"Em {position['side']}" if position else "Aguardando"
                send_telegram(f"\U0001F7E2 {sym_short} {swing_label} {price_str} | {status}")

            klines = fetch_klines(symbol, 1000)
            opens = np.array([float(k[1]) for k in klines])
            highs = np.array([float(k[2]) for k in klines])
            lows = np.array([float(k[3]) for k in klines])
            closes = np.array([float(k[4]) for k in klines])
            times = [datetime.fromtimestamp(k[0]/1000, tz=timezone.utc) for k in klines]

            if len(closes) < 500:
                time.sleep(60)
                continue

            i = len(closes) - 1
            sma20 = np.full(len(closes), np.nan)
            sma200 = np.full(len(closes), np.nan)
            for j in range(max(19,199), len(closes)):
                sma20[j] = np.mean(closes[j-19:j+1])
                sma200[j] = np.mean(closes[j-199:j+1])

            # Gerenciamento de posicao
            if position is not None:
                r = {'low':lows[i],'high':highs[i],'close':closes[i],'sma200':sma200[i]}
                exit_price = None
                motivo = None
                if position['side'] == 'LONG':
                    if r['high'] >= position['target']:
                        exit_price = position['target']; motivo = 'TARGET'
                    if exit_price is None and r['low'] <= position['stop']:
                        exit_price = position['stop']
                        motivo = 'TRAIL_WIN' if position['stop']>position['entry'] else 'STOP'
                    if exit_price is None and r['close'] < r['sma200']:
                        exit_price = r['close']; motivo = 'REVERSAL'
                    if exit_price is None:
                        position['peak'] = max(position['peak'], r['high'])
                        mfe = (position['peak']-position['entry'])/position['entry']
                        if mfe >= MIN_TRAIL_MFE:
                            new_stop = position['entry'] + TRAIL_PCT*(position['peak']-position['entry'])
                            if new_stop > position['stop']:
                                position['stop'] = new_stop
                                log.info(f"{sym_short} Trail: {position['stop']:.2f}")
                else:
                    if r['low'] <= position['target']:
                        exit_price = position['target']; motivo = 'TARGET'
                    if exit_price is None and r['high'] >= position['stop']:
                        exit_price = position['stop']
                        motivo = 'TRAIL_WIN' if position['stop']<position['entry'] else 'STOP'
                    if exit_price is None and r['close'] > r['sma200']:
                        exit_price = r['close']; motivo = 'REVERSAL'
                    if exit_price is None:
                        position['valley'] = min(position['valley'], r['low'])
                        maf = (position['entry']-position['valley'])/position['entry']
                        if maf >= MIN_TRAIL_MFE:
                            new_stop = position['entry'] - TRAIL_PCT*(position['entry']-position['valley'])
                            if new_stop < position['stop']:
                                position['stop'] = new_stop
                                log.info(f"{sym_short} Trail: {position['stop']:.2f}")

                if exit_price is not None:
                    pnl = (exit_price-position['entry'])/position['entry']*100 if position['side']=='LONG' else (position['entry']-exit_price)/position['entry']*100
                    hold = now - position['entry_ts']
                    h_str = f"{int(hold.total_seconds()//3600)}h{int((hold.total_seconds()%3600)//60)}m"
                    tick = "\U0001F7E2" if position['side']=='LONG' else "\U0001F534"
                    result = "\U0001F4C8 GANHOU" if pnl > 0 else "\U0001F4C9 PERDEU"
                    msg = f"{tick} <b>{sym_short} {swing_label} SAIDA {position['side']}</b> {result}\n<b>Motivo:</b> {motivo}\n<b>Entrada:</b> ${position['entry']:,.0f}\n<b>Saida:</b> ${exit_price:,.0f}\n<b>PnL:</b> {pnl:+.2f}%\n<b>Duracao:</b> {h_str}"
                    log.info(f"{sym_short} SAIDA: {motivo} | {pnl:+.2f}%")
                    send_telegram(msg)
                    position = None

            # Sinais (so se sem posicao)
            if position is None:
                sinal = check_signals(opens, highs, lows, closes, times, sma20, sma200, i)
                if sinal is not None:
                    tipo, lado, price, sig_time = sinal
                    ls = last_signal.get(lado)
                    if ls is None or (times[i] - ls).total_seconds() >= 3600:
                        prev = i - 1
                        entry = (opens[prev] + closes[prev]) / 2
                        stop = get_swing_stop(highs, lows, prev, lado, lookback)
                        sd = abs(entry - stop) / entry
                        if sd >= MIN_STOP_PCT and sd <= MAX_STOP_PCT:
                            target = entry + TARGET_R*(entry-stop) if lado=='LONG' else entry - TARGET_R*(stop-entry)
                            position = {
                                'side':lado,'entry':entry,'stop':stop,'target':target,
                                'sinal':tipo,'peak':entry if lado=='LONG' else None,
                                'valley':entry if lado=='SHORT' else None,'entry_ts':times[i]
                            }
                            tick = "\U0001F7E2" if lado=='LONG' else "\U0001F534"
                            msg = f"{tick} <b>{sym_short} {swing_label} SINAL {lado}</b> - {tipo}\n{sig_time.strftime('%d/%m %H:%M')}\n<b>Entrada:</b> ${entry:,.0f}\n<b>Stop:</b> ${stop:,.0f} ({sd*100:.2f}%)\n<b>Alvo:</b> ${target:,.0f}"
                            log.info(f"{sym_short} ENTRADA: {tipo} @ {entry:.0f}")
                            send_telegram(msg)
                            last_signal[lado] = times[i]

            # Sleep ate proximo candle
            next_candle = (now + timedelta(minutes=5)).replace(second=10, microsecond=0)
            next_candle = next_candle.replace(minute=(next_candle.minute // 5) * 5)
            sleep = (next_candle - now).total_seconds()
            if sleep > 0:
                time.sleep(min(sleep, 30 if position else 60))

        except Exception as e:
            log.error(f"{sym_short} Erro: {e}", exc_info=True)
            time.sleep(60)

# Flask web server (para Render)
app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health():
    uptime = int(time.time() - bot_start)
    has_tg = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    status = {}
    for sym in SYMBOLS:
        s = sym.replace("USDT", "")
        for lb, sl in SWING_VALUES:
            key = f"{sym}_{sl}"
            status[key] = "rodando" if bots.get(key, {}).get('started') else "iniciando"
    return jsonify({
        "status": "ok",
        "uptime": f"{uptime//3600}h{(uptime%3600)//60}m",
        "telegram_configured": has_tg,
        "bots": status
    })

@app.route('/test-tg')
def test_telegram():
    has_tg = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    if has_tg:
        send_telegram("\U0001F7E2 <b>Teste do bot</b> - BTC/ETH/SOL funcionando!")
        return jsonify({"sent": True})
    return jsonify({"sent": False, "error": "Telegram nao configurado"})

@app.before_request
def ensure_bots():
    if not hasattr(app, '_bots_started'):
        app._bots_started = True
        for sym in SYMBOLS:
            for lookback, swing_label in SWING_VALUES:
                key = f"{sym}_{swing_label}"
                bots[key] = {'started': True}
                t = threading.Thread(target=bot_loop, args=(sym, lookback, swing_label), daemon=True)
                t.start()
                log.info(f"Thread {key} iniciada")

if __name__ == '__main__':
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
