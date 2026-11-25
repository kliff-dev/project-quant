"""Reproducible local Windows build. Uses a reviewed, explicit data allowlist."""
from __future__ import annotations

import argparse
from pathlib import Path
import json
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))


def stage_files():
    stage=ROOT/'build/desktop-stage'
    # Recreate the payload from its allowlist. Otherwise removed source files
    # or a stray log from an earlier build can survive into the next installer.
    # Check the absolute target before recursive deletion, including junctions.
    expected=ROOT.resolve()/'build/desktop-stage'
    if stage.resolve()!=expected or not expected.is_relative_to(ROOT.resolve()):
        raise ValueError('Desktop staging must stay inside the workspace build folder.')
    if stage.exists():
        shutil.rmtree(stage)
    configuration=stage/'source/.streamlit/config.toml'
    configuration.parent.mkdir(parents=True,exist_ok=True)
    # Frozen packages do not live under a site-packages path. Streamlit must
    # use its bundled production frontend rather than infer development mode.
    from dashboard.ui import THEME_CONFIG
    configuration.write_text(THEME_CONFIG,encoding='utf-8')
    for folder in ('dashboard','analytics','desktop'):
        target=stage/'source'/folder
        target.mkdir(parents=True,exist_ok=True)
        for source in (ROOT/folder).glob('*.py'):
            shutil.copy2(source,target/source.name)
    for name in ['scripts/__init__.py','scripts/research_holding_period.py',
                 'config/activation_eurusd_policy.json','config/activation_gold_policy.json',
                 'config/activation_timing_spec.json','config/conditional_risk_spec.json','config/five_setup_spec.json']:
        target=stage/'source'/name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT/name,target)
    archives={'gold_intraday_2026-11-15':('XAUUSDm_M5.csv','csv_sha256'),
              'mt5_six_month_2026-11-14_025036':('EURUSDm_H1.csv','sha256')}
    for folder,(filename,key) in archives.items():
        source=ROOT/'data/raw'/folder
        target=stage/'seed_workspace/data/raw'/folder
        target.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source/filename,target/filename)
        metadata=json.loads((source/'manifest.json').read_text(encoding='utf-8'))
        (target/'manifest.json').write_text(json.dumps({key:metadata[key],
            'source':'Saved broker historical bid candles; offline installation seed.',
            'file':filename,'live':False},indent=2)+'\n',encoding='utf-8')
    # Allowlisted public price datasets for the seven-setup offline page.
    # Keep only the newest snapshot per exact provider/instrument/timeframe.
    from analytics.setup_lab import read_dataset
    latest={}
    for manifest in (ROOT/'data/research/setup_lab').glob('*.json'):
        _,info=read_dataset(manifest)
        meta=info['metadata']
        key=(meta['source'],meta['instrument'],meta['timeframe'])
        if key not in {('Exness MT5','EURUSDm','H1'),('Exness MT5','XAUUSDm','M5'),
                       ('Dukascopy','EURUSD','H1'),('Dukascopy','XAUUSD','M5')}:
            continue
        if key not in latest or meta.get('acquired_at_utc','')>latest[key][1]['metadata'].get('acquired_at_utc',''):
            latest[key]=(manifest,info)
    for manifest,info in latest.values():
        target=stage/'seed_workspace/data/research/setup_lab'
        target.mkdir(parents=True,exist_ok=True)
        shutil.copy2(manifest.parent/info['csv'],target/info['csv'])
        allowed=('source','instrument','timeframe','price_basis','instrument_type',
                 'acquired_at_utc','volume_quality','cost_quality')
        sanitized=dict(info,metadata={key:info['metadata'][key] for key in allowed if key in info['metadata']})
        (target/manifest.name).write_text(json.dumps(sanitized,indent=2)+'\n',encoding='utf-8')
    # BTC price history and aggregate research results only; never execution logs.
    btc=ROOT/'data/research/btc'
    if (btc/'manifest.json').is_file():
        target=stage/'seed_workspace/data/research/btc';target.mkdir(parents=True,exist_ok=True)
        shutil.copy2(btc/'BTCUSDm_H1.csv',target/'BTCUSDm_H1.csv')
        meta=json.loads((btc/'manifest.json').read_text(encoding='utf-8'))
        allowed=('source','symbol','instrument_type','timeframe','price_basis','account_currency','contract_size','point',
                 'volume_min','volume_max','volume_step','digits','contract','first_open_utc','last_close_utc','rows','csv_sha256','volume_quality','downloaded_at_utc')
        (target/'manifest.json').write_text(json.dumps({k:meta[k] for k in allowed if k in meta},indent=2)+'\n',encoding='utf-8')
    for path in (ROOT/'reports/market_analysis').glob('btc_*/run.json'):
        target=stage/'seed_workspace/reports/market_analysis'/path.parent.name;target.mkdir(parents=True,exist_ok=True)
        summary=json.loads(path.read_text(encoding='utf-8'));summary.get('source',{}).pop('manifest_path',None)
        (target/'run.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    # Credentials, account logs, actual trade history, monitor authority and
    # executable trading modules are deliberately absent from the payload.
    return stage


def icon():
    from PIL import Image,ImageDraw
    image=Image.new('RGBA',(256,256),(14,21,32,255))
    draw=ImageDraw.Draw(image)
    draw.rounded_rectangle((14,14,242,242),radius=44,outline=(50,196,173),width=10)
    for x,top,bottom in [(62,114,186),(105,89,161),(148,75,132),(191,41,111)]:
        draw.line((x,top-14,x,bottom+14),fill=(50,196,173),width=5)
        draw.rounded_rectangle((x-12,top,x+12,bottom),radius=4,fill=(50,196,173))
    target=ROOT/'desktop/assets/ProjectQuant.ico'
    target.parent.mkdir(parents=True,exist_ok=True)
    image.save(target,sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage-only',action='store_true')
    parser.add_argument('--iscc',type=Path,default=ROOT/'build-tools/InnoSetup/ISCC.exe')
    args=parser.parse_args(argv)
    icon();stage_files()
    if args.stage_only:
        return 0
    subprocess.run([sys.executable,'-B','-m','PyInstaller','--noconfirm',
                    '--distpath',str(ROOT/'dist'),'--workpath',str(ROOT/'build/pyinstaller'),
                    str(ROOT/'packaging/ProjectQuant.spec')],cwd=ROOT,check=True)
    # Exercise frozen script execution before publishing an installer. Server
    # health alone cannot detect imports injected by Streamlit at runtime.
    evidence=ROOT/'build/desktop-self-test.json'
    evidence.unlink(missing_ok=True)
    subprocess.run([str(ROOT/'dist/ProjectQuant/ProjectQuant.exe'),
                    '--workspace',str(ROOT),'--self-test',str(evidence)],
                   cwd=ROOT,check=True,timeout=90)
    checked=json.loads(evidence.read_text(encoding='utf-8'))
    if not (checked.get('frozen') and checked.get('server_closed')
            and len(checked.get('pages',{}))==8
            and len(checked.get('pages',{}).get('BTC research & DEMO',{}).get('chart_trace_counts',[]))>=2
            and all(page.get('script_finished') and page.get('exceptions')==0
                    for page in checked['pages'].values())):
        raise RuntimeError('Frozen application page checks did not pass; installer build stopped.')
    if not args.iscc.is_file():
        raise FileNotFoundError('Inno Setup compiler is missing; pass --iscc with its installed ISCC.exe path.')
    subprocess.run([str(args.iscc),'/Q',str(ROOT/'packaging/ProjectQuant.iss')],cwd=ROOT,check=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
