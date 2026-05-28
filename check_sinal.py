import requests, numpy as np, time, os
from datetime import datetime, timezone

SYMBOL='BTCUSDT'; TIMEFRAME='5m'
COMP_PCT=0.6; ZONE_PCT=0.3; POWER_MIN=0.6
LOOKLEFT=30; ZONE_LOOKBACK=10
SLOPE200_LEN=5; SLOPE20_LEN=3; SLOPE_THRESH=0.05
SWING_LOOKBACK=12; TARGET_R=5.0

HEADERS={'User-Agent':'Mozilla/5.0','Accept':'application/json'}

klines = None
for host in ['api.binance.us','api.binance.com','fapi.binance.com']:
    try:
        path='/fapi/v1/klines' if 'fapi' in host else '/api/v3/klines'
        r=requests.get(f'https://{host}{path}', params={'symbol':SYMBOL,'interval':TIMEFRAME,'limit':1000}, headers=HEADERS, timeout=15)
        if r.status_code==200:
            klines=r.json()
            break
    except:
        pass

if klines is None:
    bybit_interval={'1m':1,'3m':3,'5m':5,'15m':15,'30m':30,'1h':60,'2h':120,'4h':240,'6h':360,'12h':720,'1d':'D','1w':'W','1M':'M'}[TIMEFRAME]
    for _ in range(3):
        try:
            r=requests.get("https://api.bybit.com/v5/market/kline", params={"category":"spot","symbol":SYMBOL,"interval":bybit_interval,"limit":1000}, headers=HEADERS, timeout=10)
            if r.status_code==200:
                data=r.json()
                if data.get("retCode")==0:
                    klines=data["result"]["list"]
                    klines.reverse()
                    klines=[[int(k[0]),float(k[1]),float(k[2]),float(k[3]),float(k[4]),float(k[5]),float(k[6]),0,0,0,0] for k in klines]
                    break
        except:
            time.sleep(1)

if klines is None:
    print('Falha ao buscar dados')
    exit()

opens=np.array([float(k[1]) for k in klines])
highs=np.array([float(k[2]) for k in klines])
lows=np.array([float(k[3]) for k in klines])
closes=np.array([float(k[4]) for k in klines])
times=[datetime.fromtimestamp(k[0]/1000,tz=timezone.utc) for k in klines]

i=len(closes)-1
sma20=np.full(len(closes),np.nan)
sma200=np.full(len(closes),np.nan)
for j in range(max(19,199),len(closes)):
    sma20[j]=np.mean(closes[j-19:j+1])
    sma200[j]=np.mean(closes[j-199:j+1])

if np.isnan(sma20[i]) or np.isnan(sma200[i]):
    print('SMA nao disponivel')
    exit()

print(f'Candle atual: {times[i]}')
print(f'Open: {opens[i]:.0f} High: {highs[i]:.0f} Low: {lows[i]:.0f} Close: {closes[i]:.0f}')
print(f'SMA20: {sma20[i]:.0f} SMA200: {sma200[i]:.0f}')
print(f'Body/Range: {abs(closes[i]-opens[i])/(highs[i]-lows[i])*100 if highs[i]-lows[i]>0 else 0:.1f}%')

body=abs(closes[i]-opens[i])
tr=highs[i]-lows[i]
if tr==0 or body/tr < POWER_MIN:
    print(f'Candle fraco (body/tr={body/tr*100:.1f}%)')
    exit()

lado='LONG' if closes[i]>opens[i] else 'SHORT'
price=closes[i]
price_cur = closes[-1]

slope200 = (sma200[i]-sma200[i-SLOPE200_LEN])/sma200[i-SLOPE200_LEN]*100 if i>=SLOPE200_LEN and not np.isnan(sma200[i-SLOPE200_LEN]) else None
flat200 = slope200 is not None and abs(slope200)<SLOPE_THRESH
slope20 = (sma20[i]-sma20[i-SLOPE20_LEN])/sma20[i-SLOPE20_LEN]*100 if i>=SLOPE20_LEN and not np.isnan(sma20[i-SLOPE20_LEN]) else None
trending20 = slope20 is not None and abs(slope20)>SLOPE_THRESH

z_up=sma200[i]*(1+ZONE_PCT/100)
z_dn=sma200[i]*(1-ZONE_PCT/100)
near=(z_dn<=price<=z_up) or (z_dn<=lows[i]<=z_up) or (z_dn<=opens[i]<=z_up)
surge=near and ((lado=='LONG' and price>sma200[i] and price>sma20[i]) or (lado=='SHORT' and price<sma200[i] and price<sma20[i]))
look_idx=max(200,i-LOOKLEFT)
nothing=False
if i-look_idx>=3:
    nothing=price>float(np.max(highs[look_idx:i])) if lado=='LONG' else price<float(np.min(lows[look_idx:i]))
lo=float(np.min(lows[max(0,i-ZONE_LOOKBACK+1):i+1]))
hi=float(np.max(highs[max(0,i-ZONE_LOOKBACK+1):i+1]))
rng_pct=(hi-lo)/lo*100 if lo>0 else 999
p20=abs(price-sma20[i])/sma20[i]*100
compressed=rng_pct<COMP_PCT and p20<COMP_PCT*0.5
comp_signal=compressed and ((lado=='LONG' and price>sma20[i]) or (lado=='SHORT' and price<sma20[i]))
baleia=surge and flat200 and nothing

print(f'Lado: {lado} Preco: {price:.0f}')
print(f'Flat200: {flat200} (slope={slope200:.4f})' if slope200 is not None else 'Flat200: N/A')
print(f'Surge: {surge} NothingLeft: {nothing}')
print(f'Compressed: {compressed} (rng={rng_pct:.2f}% p20={p20:.2f}%)')
print(f'BALEIA: {baleia} SURGE: {surge and not flat200 and trending20} COMP: {comp_signal}')

if baleia or (surge and not flat200 and trending20) or comp_signal:
    sinal='BALEIA' if baleia else ('SURGE' if surge and not flat200 and trending20 else 'COMP')
    sinal_tipo=f'{sinal}_{lado[0]}'
    prev_idx=i-1
    entry=(opens[prev_idx]+closes[prev_idx])/2
    start_sw=max(0,prev_idx-SWING_LOOKBACK+1)
    if lado=='LONG':
        stop_price=float(np.min(lows[start_sw:prev_idx+1]))
    else:
        stop_price=float(np.max(highs[start_sw:prev_idx+1]))
    stop_dist=abs(entry-stop_price)/entry
    if lado=='LONG':
        target=entry+TARGET_R*(entry-stop_price)
    else:
        target=entry-TARGET_R*(stop_price-entry)

    token='8695489796:AAGqGSFn02hxUHAq7U09M-3Z5XVYylqdgAE'
    chat_id='1059819117'
    tick='🟢' if lado=='LONG' else '🔴'

    lines=[
        f'{tick} <b>BALEIA 200 TRAILING - SINAL {lado}</b> {tick}',
        f'<code>{sinal_tipo}</code> | {times[i].strftime("%d/%m %H:%M")}',
        f'<b>Entrada (midpoint):</b> ${entry:,.0f}',
        f'<b>Stop Swing:</b> ${stop_price:,.0f} ({stop_dist*100:.2f}%)',
        f'<b>Alvo (5R):</b> ${target:,.0f}',
        f'<b>Trailing:</b> 50% apos 1% MFE',
        f'<b>SMA20:</b> {sma20[i]:,.0f} | <b>SMA200:</b> {sma200[i]:,.0f}',
        f'<b>Preco atual:</b> ${closes[-1]:,.0f}',
        f'\n#baleia #trailing #{sinal.lower()} #btc',
    ]
    msg='\n'.join(lines)
    print(f'\nSINAL ENCONTRADO! Enviando Telegram...')
    r=requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
        json={'chat_id':chat_id,'text':msg,'parse_mode':'HTML'},timeout=10)
    print(f'Telegram: {r.status_code} - {r.text[:200]}')
else:
    print('Nenhum sinal no momento')
    token='8695489796:AAGqGSFn02hxUHAq7U09M-3Z5XVYylqdgAE'
    chat_id='1059819117'
    msg=(
        f'\U0001F4CA <b>Baleia 200 Trailing - Status</b>\n'
        f'{times[i].strftime("%d/%m %H:%M")} | BTC ${closes[-1]:,.0f}\n'
        f'SMA20: {sma20[i]:,.0f} SMA200: {sma200[i]:,.0f}\n'
        f'Nenhum sinal no momento'
    )
    r=requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
        json={'chat_id':chat_id,'text':msg,'parse_mode':'HTML'},timeout=10)
    print(f'Telegram status: {r.status_code}')
