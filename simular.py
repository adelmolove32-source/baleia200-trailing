import pandas as pd, numpy as np, time, os, sys

exec(open(r'C:\Users\muril\Desktop\bot btc2\baleia200_pptx.py').read().split('# =====================================================')[0])

DATA = r'C:\Users\muril\Desktop\bot btc2\btc_5m_360d.pkl'
df = pd.read_pickle(DATA)
cutoff = df['timestamp'].max() - pd.Timedelta(days=360)
bt = df[df['timestamp']>=cutoff].copy().reset_index(drop=True)
bt = build_signals(bt, sma_shift=1)
bt = bt.dropna(subset=['sma20','sma200']).reset_index(drop=True)

t0 = time.time()
trades = run(bt, 'midpoint_trail', 0.0, 5)
print(f'Simulacao: {time.time()-t0:.1f}s, {len(trades)} trades\n')

eq = [200.0]
for t in trades:
    eq.append(eq[-1]+t['pnl'])
eq = np.array(eq)
peak = np.maximum.accumulate(eq)
dd = (peak-eq)/peak*100

print(f'  Capital inicial:   $200.00')
print(f'  Capital final:     ${eq[-1]:.2f}')
print(f'  Retorno total:     {((eq[-1]/200-1)*100):+.2f}%')
print(f'  Drawdown maximo:   {dd.max():.2f}%')
print(f'  Total trades:      {len(trades)}')
wins = [t for t in trades if t['pnl']>0]
losses = [t for t in trades if t['pnl']<=0]
print(f'  Vitorias:          {len(wins)} ({len(wins)/len(trades)*100:.1f}%)')
print(f'  Derrotas:          {len(losses)} ({len(losses)/len(trades)*100:.1f}%)')

pnls = np.array([t['pnl'] for t in trades])
print(f'  Maior win:         ${pnls.max():.2f}')
print(f'  Maior loss:        ${pnls.min():.2f}')
print(f'  PnL medio:         ${pnls.mean():.2f}')
print(f'  Mediana PnL:       ${np.median(pnls):.2f}')
print(f'  Desvio padrao PnL: ${pnls.std():.2f}')

print(f'\n  Top 10 maiores PnLs:')
for t in sorted(trades, key=lambda x: -x['pnl'])[:10]:
    print(f'    {t["sinal"]:>8} | {t["entry"]:>8.0f} -> {t["exit"]:>8.0f} | ${t["pnl"]:>+8.2f} | {t["rz"]}')

print(f'\n  Top 10 piores PnLs:')
for t in sorted(trades, key=lambda x: x['pnl'])[:10]:
    print(f'    {t["sinal"]:>8} | {t["entry"]:>8.0f} -> {t["exit"]:>8.0f} | ${t["pnl"]:>+8.2f} | {t["rz"]}')

print(f'  Resumo por saida (rz):')
rz_stats = {}
for t in trades:
    rz = t['rz']
    if rz not in rz_stats:
        rz_stats[rz] = {'n':0,'pnl':0}
    rz_stats[rz]['n'] += 1
    rz_stats[rz]['pnl'] += t['pnl']

print(f'  Resumo por saida:')
for rz in sorted(rz_stats):
    v = rz_stats[rz]
    print(f'    {rz:>15}: {v["n"]:>4}t PnL${v["pnl"]:>+10.2f}')

print(f'  Resumo por sinal:')
sigs = {}
for t in trades:
    s = t['sinal']
    if s not in sigs:
        sigs[s] = {'n':0,'w':0,'pnl':0}
    sigs[s]['n'] += 1
    sigs[s]['pnl'] += t['pnl']
    if t['pnl']>0:
        sigs[s]['w'] += 1
for s in sorted(sigs):
    v = sigs[s]
    wr = v['w']/v['n']*100
    print(f'    {s:>8}: {v["n"]:>4}t WR{wr:>5.1f}% PnL${v["pnl"]:>+9.2f}')

print(f'\n  Equity curve (primeiras 20 e ultimas 10 linhas):')
pts = [200.0]
for t in trades:
    pts.append(pts[-1]+t['pnl'])
for i in range(min(20, len(pts))):
    print(f'    trade {i:>4}: ${pts[i]:>8.2f}')
if len(pts) > 30:
    print(f'    ...')
    for i in range(len(pts)-10, len(pts)):
        print(f'    trade {i:>4}: ${pts[i]:>8.2f}')

print(f'\n  Equity salva em equity_curve.csv')
pd.Series(pts).to_csv(r'C:\Users\muril\Desktop\bot btc2\equity_curve.csv', index=False, header=False)
