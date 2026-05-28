import os, sys, subprocess
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "numpy", "pandas", "requests", "python-dotenv"])

import os, time, json, logging, threading, http.server
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

# === PARAMETROS ===
SYMBOL = "BTCUSDT"
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
SWING_LOOKBACK = 12
MIN_STOP_PCT = 0.0005
MAX_STOP_PCT = 0.05

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
LOG_FILE = "bot_trailing_sinais.csv"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout, force=True)
log = logging.getLogger("bot_trailing")

position = None  # {'side','entry','stop','target','peak','valley','qty','entry_ts','sinal'}

def calc_slope(arr, idx, lookback):
    if idx < lookback or np.isnan(arr[idx]):
        return None
    return (arr[idx] - arr[idx - lookback]) / arr[idx - lookback] * 100

def get_swing_stop_idx(highs, lows, idx, side, lookback=SWING_LOOKBACK):
    start = max(0, idx - lookback + 1)
    if side == 'LONG':
        return float(np.min(lows[start:idx + 1]))
    else:
        return float(np.max(highs[start:idx + 1]))

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
        if r.status_code != 200:
            log.warning(f"Telegram: {r.status_code} {r.text[:200]}")
    except Exception as e:
        log.warning(f"Telegram error: {e}")

def build_entry_msg(side, entry, stop, target, signal_time, sinal, sma20, sma200):
    tick = "\U0001F7E2" if side == "LONG" else "\U0001F534"
    stop_dist = abs(entry - stop) / entry * 100
    return (
        f"{tick} <b>SINAL {side}</b> - {sinal}\n"
        f"{signal_time}\n"
        f"<b>Entrada:</b> ${entry:,.0f}\n"
        f"<b>Stop:</b> ${stop:,.0f} ({stop_dist:.2f}%)\n"
        f"<b>Alvo:</b> ${target:,.0f}"
    )

def build_exit_msg(side, entry, exit_price, pnl_pct, motivo, hold_time):
    tick = "\U0001F7E2" if side == "LONG" else "\U0001F534"
    result = "\U0001F4C8 GANHOU" if pnl_pct > 0 else "\U0001F4C9 PERDEU"
    return (
        f"{tick} <b>SAIDA {side}</b> - {result}\n"
        f"<b>Motivo:</b> {motivo}\n"
        f"<b>Entrada:</b> ${entry:,.0f}\n"
        f"<b>Saida:</b> ${exit_price:,.0f}\n"
        f"<b>PnL:</b> {pnl_pct:+.2f}%\n"
        f"<b>Duracao:</b> {hold_time}"
    )

def log_signal(d):
    fe = os.path.isfile(LOG_FILE)
    pd.DataFrame([d]).to_csv(LOG_FILE, mode="a", header=not fe, index=False)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "application/json"}
INTERVAL_MAP = {"1m":1,"3m":3,"5m":5,"15m":15,"30m":30,"1h":60,"2h":120,"4h":240,"6h":360,"12h":720,"1d":"D","1w":"W","1M":"M"}

def fetch_klines(limit=1000):
    for host in ["api.binance.us", "api.binance.com", "fapi.binance.com"]:
        try:
            path = "/fapi/v1/klines" if "fapi" in host else "/api/v3/klines"
            r = requests.get(f"https://{host}{path}", params={"symbol":SYMBOL,"interval":TIMEFRAME,"limit":limit}, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                return r.json()
        except:
            pass
    bybit_interval = INTERVAL_MAP.get(TIMEFRAME, 5)
    for _ in range(3):
        try:
            r = requests.get("https://api.bybit.com/v5/market/kline", params={
                "category":"spot","symbol":SYMBOL,"interval":bybit_interval,"limit":limit
            }, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get("retCode") == 0:
                    klines = data["result"]["list"]
                    klines.reverse()
                    return [[int(k[0]),float(k[1]),float(k[2]),float(k[3]),float(k[4]),float(k[5]),float(k[6]),0,0,0,0] for k in klines]
        except:
            time.sleep(1)
    raise Exception("Falha ao buscar dados")

def check_signals(opens, highs, lows, closes, times, sma20, sma200, i):
    if np.isnan(sma20[i]) or np.isnan(sma200[i]):
        return None
    body = abs(closes[i] - opens[i])
    tr = highs[i] - lows[i]
    if tr == 0 or body / tr < POWER_MIN:
        return None
    side = "LONG" if closes[i] > opens[i] else "SHORT"
    price = closes[i]
    slope200 = calc_slope(sma200, i, SLOPE200_LEN)
    flat200 = slope200 is not None and abs(slope200) < SLOPE_THRESH
    slope20 = calc_slope(sma20, i, SLOPE20_LEN)
    trending20 = slope20 is not None and abs(slope20) > SLOPE_THRESH
    z_up = sma200[i] * (1 + ZONE_PCT / 100)
    z_dn = sma200[i] * (1 - ZONE_PCT / 100)
    near = (z_dn <= price <= z_up) or (z_dn <= lows[i] <= z_up) or (z_dn <= opens[i] <= z_up)
    surge = near and (
        (side == "LONG" and price > sma200[i] and price > sma20[i])
        or (side == "SHORT" and price < sma200[i] and price < sma20[i]))
    look_idx = max(200, i - LOOKLEFT)
    nothing_left = False
    if i - look_idx >= 3:
        if side == "LONG":
            nothing_left = price > float(np.max(highs[look_idx:i]))
        else:
            nothing_left = price < float(np.min(lows[look_idx:i]))
    lo = float(np.min(lows[max(0,i-ZONE_LOOKBACK+1):i+1]))
    hi = float(np.max(highs[max(0,i-ZONE_LOOKBACK+1):i+1]))
    rng_pct = (hi-lo)/lo*100 if lo>0 else 999
    p20 = abs(price-sma20[i])/sma20[i]*100
    compressed = rng_pct < COMP_PCT and p20 < COMP_PCT*0.5
    comp_signal = compressed and ((side=="LONG" and price>sma20[i]) or (side=="SHORT" and price<sma20[i]))
    baleia = surge and flat200 and nothing_left
    if baleia:
        return ('BALEIA_'+('L' if side=='LONG' else 'S'), side, price, times[i])
    if surge and not flat200 and trending20:
        return ('SURGE_'+('L' if side=='LONG' else 'S'), side, price, times[i])
    if comp_signal:
        return ('COMP_'+('L' if side=='LONG' else 'S'), side, price, times[i])
    return None

def main():
    global position
    log.info(f"Baleia 200 Trailing Bot - {SYMBOL} {TIMEFRAME}")
    has_tg = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    log.info(f"Telegram: {'SIM' if has_tg else 'NAO configurado'}")
    if has_tg:
        send_telegram(f"\U0001F7E2 Bot rodando - {SYMBOL} {TIMEFRAME}")

    last_signal = {}
    last_heartbeat = 0

    while True:
        try:
            now = datetime.now(timezone.utc)
            klines = fetch_klines(1000)
            opens = np.array([float(k[1]) for k in klines])
            highs = np.array([float(k[2]) for k in klines])
            lows = np.array([float(k[3]) for k in klines])
            closes = np.array([float(k[4]) for k in klines])
            times = [datetime.fromtimestamp(k[0]/1000, tz=timezone.utc) for k in klines]

            now_ts = int(now.timestamp())
            if has_tg and now_ts - last_heartbeat >= 3600:
                last_heartbeat = now_ts
                status = f"Em {position['side']} @ {position['entry']:.0f}" if position else "Aguardando..."
                send_telegram(f"\U0001F7E2 BTC {closes[-1]:,.0f} | {status}")

            if len(closes) < 500:
                time.sleep(60)
                continue

            i = len(closes) - 1
            sma20 = np.full(len(closes), np.nan)
            sma200 = np.full(len(closes), np.nan)
            for j in range(max(19,199), len(closes)):
                sma20[j] = np.mean(closes[j-19:j+1])
                sma200[j] = np.mean(closes[j-199:j+1])

            # === GERENCIAMENTO DE POSICAO ===
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
                        exit_price = r['close']; motivo = 'REVERSAL_SMA200'
                    if exit_price is None:
                        position['peak'] = max(position['peak'], r['high'])
                        mfe = (position['peak']-position['entry'])/position['entry']
                        if mfe >= MIN_TRAIL_MFE:
                            new_stop = position['entry'] + TRAIL_PCT * (position['peak'] - position['entry'])
                            if new_stop > position['stop']:
                                old_stop = position['stop']
                                position['stop'] = new_stop
                                log.info(f"Trail atualizado: stop={position['stop']:.2f}")
                                if has_tg and (new_stop - old_stop) > 0:
                                    send_telegram(f"\U0001F817 <b>Stop atualizado</b> ({position['sinal']})\n💰 50% do lucro travado\n${old_stop:,.0f} > ${new_stop:,.0f}")
                else:
                    if r['low'] <= position['target']:
                        exit_price = position['target']; motivo = 'TARGET'
                    if exit_price is None and r['high'] >= position['stop']:
                        exit_price = position['stop']
                        motivo = 'TRAIL_WIN' if position['stop']<position['entry'] else 'STOP'
                    if exit_price is None and r['close'] > r['sma200']:
                        exit_price = r['close']; motivo = 'REVERSAL_SMA200'
                    if exit_price is None:
                        position['valley'] = min(position['valley'], r['low'])
                        maf = (position['entry']-position['valley'])/position['entry']
                        if maf >= MIN_TRAIL_MFE:
                            new_stop = position['entry'] - TRAIL_PCT * (position['entry'] - position['valley'])
                            if new_stop < position['stop']:
                                old_stop = position['stop']
                                position['stop'] = new_stop
                                log.info(f"Trail atualizado: stop={position['stop']:.2f}")
                                if has_tg and (new_stop - old_stop) < 0:
                                    send_telegram(f"\U0001F818 <b>Stop atualizado</b> ({position['sinal']})\n\U0001F4B0 50% do lucro travado\n${old_stop:,.0f} > ${new_stop:,.0f}")

                if exit_price is not None:
                    pnl_pct = (exit_price-position['entry'])/position['entry']*100 if position['side']=='LONG' else (position['entry']-exit_price)/position['entry']*100
                    hold = now - position['entry_ts']
                    hold_str = f"{int(hold.total_seconds()//3600)}h{int((hold.total_seconds()%3600)//60)}m"
                    msg = build_exit_msg(position['side'], position['entry'], exit_price, pnl_pct, motivo, hold_str)
                    log.info(f"\nSAIDA: {motivo} | PnL: {pnl_pct:+.2f}%")
                    log_signal({
                        "datetime": now.strftime("%d/%m %H:%M"), "tipo": "SAIDA",
                        "side": position['side'], "sinal": position['sinal'],
                        "entry": position['entry'], "exit": exit_price,
                        "pnl_pct": round(pnl_pct, 2), "motivo": motivo,
                    })
                    if has_tg:
                        send_telegram(msg)
                    position = None

            # === VERIFICACAO DE SINAIS (so se sem posicao) ===
            if position is None:
                sinal_data = check_signals(opens, highs, lows, closes, times, sma20, sma200, i)
                if sinal_data is not None:
                    sinal_tipo, side, price, sig_time = sinal_data
                    ls = last_signal.get(side)
                    if ls and (times[i] - ls).total_seconds() < 3600:
                        time.sleep(5)
                    else:
                        prev_idx = i - 1
                        entry = (opens[prev_idx] + closes[prev_idx]) / 2
                        stop_price = get_swing_stop_idx(highs, lows, prev_idx, side)
                        stop_dist = abs(entry - stop_price) / entry
                        if stop_dist < MIN_STOP_PCT or stop_dist > MAX_STOP_PCT:
                            log.info(f"Sinal {sinal_tipo} ignorado: stop {stop_dist*100:.2f}% fora do range")
                        else:
                            target = entry + TARGET_R * (entry - stop_price) if side == 'LONG' else entry - TARGET_R * (stop_price - entry)
                            position = {
                                'side': side, 'entry': entry, 'stop': stop_price,
                                'target': target, 'sinal': sinal_tipo,
                                'peak': entry if side=='LONG' else None,
                                'valley': entry if side=='SHORT' else None,
                                'entry_ts': times[i],
                            }
                            ts = times[i].strftime("%d/%m %H:%M")
                            msg = build_entry_msg(side, entry, stop_price, target, ts, sinal_tipo, sma20[i], sma200[i])
                            log.info(f"\nENTRADA: {sinal_tipo} @ {entry:.2f} stop={stop_price:.2f}")
                            log_signal({
                                "datetime": ts, "tipo": "ENTRADA",
                                "side": side, "sinal": sinal_tipo,
                                "entry": round(entry,2), "stop": round(stop_price,2),
                                "target": round(target,2), "sma20": round(sma20[i],2),
                                "sma200": round(sma200[i],2),
                            })
                            if has_tg:
                                send_telegram(msg)
                            last_signal[side] = times[i]

            next_candle = (now + timedelta(minutes=5)).replace(second=5, microsecond=0)
            next_candle = next_candle.replace(minute=(next_candle.minute // 5) * 5)
            if position is not None:
                next_candle = min(next_candle, now + timedelta(seconds=30))
            sleep_sec = (next_candle - now).total_seconds()
            if sleep_sec > 0:
                time.sleep(sleep_sec)

        except requests.exceptions.RequestException as e:
            log.error(f"Request: {e}")
            time.sleep(60)
        except Exception as e:
            log.error(f"Erro: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    class HealthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Baleia 200 Trailing Bot - OK")
        def log_message(self, *a): pass
    port = int(os.getenv("PORT", 10000))
    t = threading.Thread(target=lambda: http.server.HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever(), daemon=True)
    t.start()
    main()
