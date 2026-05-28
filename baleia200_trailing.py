"""
BALEIA 200 — Implementacao exata do PPTX baleia200_estrategia.pptx
Sinais: BALEIA | COMP | SURGE
Saida: Stop swing | Alvo 5R | Reversao SMA20
"""

import pandas as pd, numpy as np, warnings, time, os, sys
warnings.filterwarnings('ignore')

CAP = 200.0
COMM = 0.0

# Parametros do PPTX
ZONE_PCT = 0.003       # 0.3% zone around SMA200
POWER_MIN = 0.6        # body >= 60% range
LOOKLEFT = 30          # lookback for "nada a esquerda"
SWING_LOOKBACK = 12    # lookback for swing stop
TARGET_R = 5.0         # 5R take profit
SLOPE_THRESH = 0.0005  # 0.05% SMA200 slope threshold
COMP_PCT = 0.006       # 0.6% compression range
EXIT_TOL = 0.003       # 0.3% SMA20 reversal tolerance
SLOPE_LEN = 5          # periods for slope calculation

def build_signals(d, sma_shift=1):
    d = d.copy()
    d['body'] = (d['close']-d['open']).abs()
    d['range_h'] = d['high']-d['low']
    d['body_pct'] = d['body']/d['range_h'].clip(0.01)
    d['power_ok'] = d['body_pct'] >= POWER_MIN
    d['green'] = d['close'] > d['open']
    d['red'] = d['close'] < d['open']

    # SMA - shift controla se usa close atual (0) ou anterior (1)
    d['sma20'] = d['close'].shift(sma_shift).rolling(20).mean()
    d['sma200'] = d['close'].shift(sma_shift).rolling(200).mean()
    d['slope200'] = d['sma200'].diff(SLOPE_LEN) / d['sma200'].shift(SLOPE_LEN).clip(0.01)
    d['sma200_flat'] = d['slope200'].abs() < SLOPE_THRESH

    # BALEIA / SURGE: distancia da SMA200 (como o bot real: check close, low, ou open)
    d['z_up'] = d['sma200'] * (1 + ZONE_PCT)
    d['z_dn'] = d['sma200'] * (1 - ZONE_PCT)
    d['na_zona_close'] = (d['close'] >= d['z_dn']) & (d['close'] <= d['z_up'])
    d['na_zona_low'] = (d['low'] >= d['z_dn']) & (d['low'] <= d['z_up'])
    d['na_zona_open'] = (d['open'] >= d['z_dn']) & (d['open'] <= d['z_up'])
    d['na_zona'] = d['na_zona_close'] | d['na_zona_low'] | d['na_zona_open']

    # Nada a esquerda
    d['max30'] = d['high'].rolling(LOOKLEFT).max().shift(1)
    d['min30'] = d['low'].rolling(LOOKLEFT).min().shift(1)
    d['nada_esq_alta'] = d['high'] > d['max30']
    d['nada_esq_baixa'] = d['low'] < d['min30']

    # BALEIA: na zona + sma200 flat + nada esq + power + direcao
    d['sinal_baleia_long'] = (d['na_zona'] & d['sma200_flat'] & d['nada_esq_alta'] &
                              d['power_ok'] & d['green'] & (d['close']>d['sma20']) & (d['close']>d['sma200']))
    d['sinal_baleia_short'] = (d['na_zona'] & d['sma200_flat'] & d['nada_esq_baixa'] &
                               d['power_ok'] & d['red'] & (d['close']<d['sma20']) & (d['close']<d['sma200']))

    # SURGE: na zona + (sma200 flat OR sma20 trending) + power + direcao
    d['sma20_slope'] = d['sma20'].diff(3)/d['sma20'].shift(3).clip(0.01)
    d['sma20_trend'] = d['sma20_slope'].abs() > SLOPE_THRESH
    d['sinal_surge_long'] = (d['na_zona'] & (d['sma200_flat'] | d['sma20_trend']) &
                             d['power_ok'] & d['green'] & (d['close']>d['sma20']) & (d['close']>d['sma200']))
    d['sinal_surge_short'] = (d['na_zona'] & (d['sma200_flat'] | d['sma20_trend']) &
                              d['power_ok'] & d['red'] & (d['close']<d['sma20']) & (d['close']<d['sma200']))

    # COMP: range compression + near SMA20 + power + direcao
    d['range_max'] = d['high'].rolling(12).max()
    d['range_min'] = d['low'].rolling(12).min()
    d['range_pct'] = (d['range_max']-d['range_min'])/d['close'].clip(0.01)
    d['compressao'] = d['range_pct'] < COMP_PCT
    d['dist_sma20_pct'] = (d['close']-d['sma20']).abs() / d['sma20'].clip(0.01)
    d['perto_sma20'] = d['dist_sma20_pct'] < ZONE_PCT  # 0.3%
    d['sinal_comp_long'] = (d['compressao'] & d['perto_sma20'] & d['power_ok'] &
                            d['green'] & (d['close']>d['sma20']))
    d['sinal_comp_short'] = (d['compressao'] & d['perto_sma20'] & d['power_ok'] &
                             d['red'] & (d['close']<d['sma20']))

    # Sinal agregado (qualquer um dos 3)
    d['sinal_long'] = d['sinal_baleia_long'] | d['sinal_surge_long'] | d['sinal_comp_long']
    d['sinal_short'] = d['sinal_baleia_short'] | d['sinal_surge_short'] | d['sinal_comp_short']
    return d

def get_swing_stop(d, idx, tipo, lookback=SWING_LOOKBACK):
    """Swing stop baseado nos ultimos N candles antes da entrada"""
    start = max(0, idx-lookback)
    seg = d.iloc[start:idx]
    if len(seg)==0:
        return None
    if tipo=='LONG':
        return seg['low'].min()
    else:
        return seg['high'].max()

STOP_PCT_FIXO = 0.02  # 2% fixed stop
TRAIL_PCT = 0.50       # 50% trail after MFE
MIN_TRAIL_MFE = 0.01   # 1% minimum move to activate trail

def run(sub, entry_mode='next_open', risk_pct=0.0, target_r=5.0):
    """
    entry_mode: 'next_open' | 'midpoint' | 'close' | 'next_open_swing' | 'midpoint_swing' | '_trail'
    risk_pct: 0 = all-in, 0.02 = 2% de risco fixo por trade
    """
    swing_stop = '_swing' in entry_mode
    trailing = '_trail' in entry_mode
    base_mode = entry_mode.replace('_swing', '').replace('_trail', '')
    
    bal = CAP
    trades = []
    ip = False
    po = None

    for i in range(len(sub)):
        r = sub.iloc[i]
        if pd.isna(r.get('sma20', np.nan)) or pd.isna(r.get('sma200', np.nan)):
            continue

        if ip:
            ex = None
            rz = None

            if trailing:
                # Trailing stop + SMA200 reversal + target (ETH copy style + alvo)
                if po['tipo'] == 'LONG':
                    if r['high'] >= po['target']:
                        ex = po['target']; rz = 'alvo'
                    if ex is None and r['low'] <= po['stop']:
                        ex = po['stop']
                        rz = 'trail_win' if po['stop']>po['entry'] else 'stop'
                    if ex is None and r['close'] < r['sma200']:
                        ex = r['close']; rz = 'sma200_reversal'
                    if ex is None:
                        po['peak'] = max(po['peak'], r['high'])
                        mfe = (po['peak']-po['entry'])/po['entry']
                        if mfe >= MIN_TRAIL_MFE:
                            po['stop'] = max(po['stop'], po['entry']+TRAIL_PCT*(po['peak']-po['entry']))
                else:
                    if r['low'] <= po['target']:
                        ex = po['target']; rz = 'alvo'
                    if ex is None and r['high'] >= po['stop']:
                        ex = po['stop']
                        rz = 'trail_win' if po['stop']<po['entry'] else 'stop'
                    if ex is None and r['close'] > r['sma200']:
                        ex = r['close']; rz = 'sma200_reversal'
                    if ex is None:
                        po['valley'] = min(po['valley'], r['low'])
                        maf = (po['entry']-po['valley'])/po['entry']
                        if maf >= MIN_TRAIL_MFE:
                            po['stop'] = min(po['stop'], po['entry']-TRAIL_PCT*(po['entry']-po['valley']))
            else:
                # Original: Swing stop + target + SMA20 reversal
                if po['tipo'] == 'LONG':
                    if r['low'] <= po['stop']:
                        ex = po['stop']
                        rz = 'stop'
                    if ex is None and r['high'] >= po['target']:
                        ex = po['target']
                        rz = 'alvo'
                    if ex is None and r['close'] < r['sma20'] * (1 - EXIT_TOL):
                        pnl_r = (r['close'] - po['entry']) / (po['entry'] - po['stop']) if (po['entry']-po['stop'])!=0 else 0
                        if pnl_r > 0.2:
                            ex = r['close']
                            rz = 'reversao_win'
                        else:
                            ex = po['entry'] - 0.5 * (po['entry'] - po['stop'])
                            rz = 'reversao_stop'
                else:
                    if r['high'] >= po['stop']:
                        ex = po['stop']
                        rz = 'stop'
                    if ex is None and r['low'] <= po['target']:
                        ex = po['target']
                        rz = 'alvo'
                    if ex is None and r['close'] > r['sma20'] * (1 + EXIT_TOL):
                        pnl_r = (po['entry'] - r['close']) / (po['stop'] - po['entry']) if (po['stop']-po['entry'])!=0 else 0
                        if pnl_r > 0.2:
                            ex = r['close']
                            rz = 'reversao_win'
                        else:
                            ex = po['entry'] + 0.5 * (po['stop'] - po['entry'])
                            rz = 'reversao_stop'

            if ex is not None:
                mult = 1 if po['tipo']=='LONG' else -1
                pnl = mult * (ex - po['entry']) / po['entry'] * po['alloc'] - po['alloc'] * COMM
                bal += pnl
                trades.append({'pnl':pnl,'rz':rz,'alloc':po['alloc'],'entry':po['entry'],
                              'stop':po['stop'],'target':po.get('target',0),
                              'exit':ex,'eidx':po['eidx'],'xidx':i,'tipo':po['tipo'],
                              'sinal':po.get('sinal',''),
                              'entry_ts':sub.iloc[po['eidx']]['timestamp'],
                              'exit_ts':r['timestamp']})
                ip = False

        if not ip:
            prev = sub.iloc[i-1] if i>0 else None
            if prev is None:
                continue

            sinal_tipo = None
            if prev.get('sinal_baleia_long', False):
                sinal_tipo = 'BALEIA_L'
            elif prev.get('sinal_baleia_short', False):
                sinal_tipo = 'BALEIA_S'
            elif prev.get('sinal_surge_long', False):
                sinal_tipo = 'SURGE_L'
            elif prev.get('sinal_surge_short', False):
                sinal_tipo = 'SURGE_S'
            elif prev.get('sinal_comp_long', False):
                sinal_tipo = 'COMP_L'
            elif prev.get('sinal_comp_short', False):
                sinal_tipo = 'COMP_S'

            if sinal_tipo is not None:
                tipo = 'LONG' if sinal_tipo.endswith('_L') else 'SHORT'
                if base_mode == 'midpoint':
                    entry = (prev['open'] + prev['close']) / 2
                elif base_mode == 'close':
                    entry = prev['close']
                else:
                    entry = r['open']

                if swing_stop:
                    stop_idx = i-1 if base_mode in ['midpoint','close'] else i
                    stop_price = get_swing_stop(sub, stop_idx, tipo)
                    if stop_price is None or stop_price <= 0:
                        continue
                    stop_dist = abs(entry - stop_price) / entry
                    if stop_dist < 0.0005 or stop_dist > 0.05:
                        continue
                else:
                    # Fixed 2% stop
                    stop_price = entry * (1 - STOP_PCT_FIXO) if tipo=='LONG' else entry * (1 + STOP_PCT_FIXO)
                    stop_dist = STOP_PCT_FIXO

                if risk_pct > 0:
                    alloc = bal * risk_pct / stop_dist
                else:
                    alloc = bal

                if tipo == 'LONG':
                    target = entry + target_r * (entry - stop_price)
                else:
                    target = entry - target_r * (stop_price - entry)
                po = {'tipo':tipo,'entry':entry,'stop':stop_price,'target':target,
                      'alloc':alloc,'eidx':i,'sinal':sinal_tipo}
                if trailing:
                    po['peak'] = entry if tipo=='LONG' else None
                    po['valley'] = entry if tipo=='SHORT' else None
                ip = True

    return trades

def metrics(trades, cap=CAP):
    if not trades:
        return {}
    wins = [t for t in trades if t['pnl']>0]
    loses = [t for t in trades if t['pnl']<=0]
    wr = len(wins)/len(trades)*100
    r_vals = []
    for t in trades:
        if t['tipo']=='LONG':
            stop_dist = t['entry'] - t['stop']
        else:
            stop_dist = t['stop'] - t['entry']
        if stop_dist > 0:
            r = t['pnl'] / (stop_dist/t['entry']*t['alloc'])
        else:
            r = 0
        r_vals.append(r)
    r_vals = np.array(r_vals)
    r_total = r_vals.sum()
    r_avg = r_vals.mean()
    bal = cap + sum(t['pnl'] for t in trades)
    ret = (bal/cap-1)*100
    # DD
    b_hist = [cap]
    for t in trades:
        b_hist.append(b_hist[-1]+t['pnl'])
    b_hist = np.array(b_hist)
    peak = np.maximum.accumulate(b_hist)
    dd_max = (peak-b_hist).max()/peak[peak.argmax()]*100 if len(b_hist)>1 else 0
    # PF
    wins_pnl = sum(t['pnl'] for t in wins)
    loses_pnl = abs(sum(t['pnl'] for t in loses))
    pf = wins_pnl/loses_pnl if loses_pnl>0 else float('inf')
    sharpe = r_avg/r_vals.std()*np.sqrt(len(r_vals)) if np.std(r_vals)>0 else 0
    return {'trades':len(trades),'wins':len(wins),'losses':len(loses),'wr':wr,
            'r_total':r_total,'r_avg':r_avg,'r_std':np.std(r_vals),'bal':bal,'ret':ret,
            'dd_max':dd_max,'pf':pf,'sharpe':sharpe}

# =====================================================
# MAIN
# =====================================================
DATA = r'C:\Users\muril\Desktop\bot btc2\btc_5m_360d.pkl'
if not os.path.exists(DATA):
    print(f'ERRO: {DATA} nao encontrado')
    exit(1)

df = pd.read_pickle(DATA)
print(f'Dados: {len(df)} candles')
print(f'Periodo: {df["timestamp"].min()} ate {df["timestamp"].max()}')

data_fim = df['timestamp'].max()
data_60d_ini = pd.Timestamp('2026-03-22')
data_60d_fim = pd.Timestamp('2026-05-21')

STOP_PCT_FIXO = 0.02

configs = [
    ('midpoint_trail_r5', 'midpoint_trail', 1, 0.0, 5),
    ('midpoint_trail_r3', 'midpoint_trail', 1, 0.0, 3),
    ('next_open_trail_r5', 'next_open_trail', 1, 0.0, 5),
    ('next_open_trail_r3', 'next_open_trail', 1, 0.0, 3),
    ('midpoint_swing_r3', 'midpoint_swing', 1, 0.0, 3),
    ('next_open_swing_r3', 'next_open_swing', 1, 0.0, 3),
    ('midpoint_fixed_r3', 'midpoint', 1, 0.0, 3),
    ('next_open_fixed_r3', 'next_open', 1, 0.0, 3),
]

print('='*70)
print('PERIODO COMPLETO (360d)')
print('='*70)
for config_name, entry_mode, sma_shift, risk_pct, target_r in configs:
    cutoff = df['timestamp'].max() - pd.Timedelta(days=360)
    bt = df[df['timestamp']>=cutoff].copy().reset_index(drop=True)
    bt = build_signals(bt, sma_shift=sma_shift)
    bt = bt.dropna(subset=['sma20','sma200']).reset_index(drop=True)
    t0 = time.time()
    trades = run(bt, entry_mode, risk_pct, target_r)
    print(f'\nBacktest {config_name}: {time.time()-t0:.1f}s ({len(trades)} trades)')
    m = metrics(trades)
    if not m:
        print('  0 trades')
        continue
    max_lev = max(t['alloc']/CAP for t in trades)
    print(f'  WR: {m["wr"]:.1f}%  R: {m["r_total"]:.1f}R  Rmed: {m["r_avg"]:+.3f}R  Ret: {m["ret"]:+.2f}%  DD: {m["dd_max"]:.1f}%  MaxLev: {max_lev:.1f}x')
    r2_by_rz = {}
    for t in trades:
        sd = abs(t['entry']-t['stop'])
        r = t['pnl']/(sd/t['entry']*t['alloc']) if sd>0 else 0
        rz = t['rz']
        if rz not in r2_by_rz: r2_by_rz[rz] = []
        r2_by_rz[rz].append(r)
    for rz in ['alvo','reversao_win','reversao_stop','stop','trail_win','sma200_reversal']:
        rs = r2_by_rz.get(rz, [])
        if rs:
            print(f'    {rz}: {len(rs)}t Rmed:{np.mean(rs):+.2f}R')
    sigs = {}
    for t in trades:
        s = t['sinal']
        sd = abs(t['entry']-t['stop'])
        r = t['pnl']/(sd/t['entry']*t['alloc']) if sd>0 else 0
        if s not in sigs: sigs[s] = {'n':0,'w':0,'r':0}
        sigs[s]['n'] += 1
        if t['pnl']>0: sigs[s]['w'] += 1
        sigs[s]['r'] += r
    for s in sorted(sigs):
        v=sigs[s]
        wr_s=v['w']/v['n']*100
        print(f'    {s}: {v["n"]}t WR{wr_s:.0f}% R{v["r"]:+.1f}R')

print()
print('='*70)
print('PERIODO 60d (Dashboard: 22/mar a 21/mai/2026)')
print('='*70)
for config_name, entry_mode, sma_shift, risk_pct, target_r in configs:
    bt = df[(df['timestamp']>=data_60d_ini)&(df['timestamp']<=data_60d_fim)].copy().reset_index(drop=True)
    bt = build_signals(bt, sma_shift=sma_shift)
    bt = bt.dropna(subset=['sma20','sma200']).reset_index(drop=True)
    t0 = time.time()
    trades = run(bt, entry_mode, risk_pct, target_r)
    print(f'\nBacktest {config_name}: {time.time()-t0:.1f}s ({len(trades)} trades)')
    m = metrics(trades)
    if not m:
        print('  0 trades')
        continue
    max_lev = max(t['alloc']/CAP for t in trades)
    print(f'  WR: {m["wr"]:.1f}%  R: {m["r_total"]:.1f}R  Rmed: {m["r_avg"]:+.3f}R  Ret: {m["ret"]:+.2f}%  DD: {m["dd_max"]:.1f}%  MaxLev: {max_lev:.1f}x')
    r2_by_rz = {}
    for t in trades:
        sd = abs(t['entry']-t['stop'])
        r = t['pnl']/(sd/t['entry']*t['alloc']) if sd>0 else 0
        rz = t['rz']
        if rz not in r2_by_rz: r2_by_rz[rz] = []
        r2_by_rz[rz].append(r)
    for rz in ['alvo','reversao_win','reversao_stop','stop','trail_win','sma200_reversal']:
        rs = r2_by_rz.get(rz, [])
        if rs:
            print(f'    {rz}: {len(rs)}t Rmed:{np.mean(rs):+.2f}R')
    sigs = {}
    for t in trades:
        s = t['sinal']
        sd = abs(t['entry']-t['stop'])
        r = t['pnl']/(sd/t['entry']*t['alloc']) if sd>0 else 0
        if s not in sigs: sigs[s] = {'n':0,'w':0,'r':0}
        sigs[s]['n'] += 1
        if t['pnl']>0: sigs[s]['w'] += 1
        sigs[s]['r'] += r
    for s in sorted(sigs):
        v=sigs[s]
        wr_s=v['w']/v['n']*100
        print(f'    {s}: {v["n"]}t WR{wr_s:.0f}% R{v["r"]:+.1f}R')
    # vs dashboard 60d
    dash_bal = CAP*1.795
    print(f'  vs DASHBOARD (263t, 51.7% WR, +79.5%): trades {m["trades"]-263:+d}  WR {m["wr"]-51.7:+.1f}pp  Ret {m["ret"]-79.5:+.1f}pp  Bal ${m["bal"]:.0f} vs ${dash_bal:.0f}')
    print()
