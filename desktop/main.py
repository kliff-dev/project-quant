"""Entry point for the installed Project Quant Windows application."""
from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import sys
import threading
import traceback

if not getattr(sys,'frozen',False):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from desktop.runtime import VERSION, LocalServer, app_data, code_root, default_workspace, resources, save_workspace


def serve(workspace, port):
    os.environ.update(PROJECT_QUANT_ROOT=str(workspace),PROJECT_QUANT_CODE_ROOT=str(code_root()),PROJECT_QUANT_DESKTOP='1')
    from streamlit.web import bootstrap
    options={
        'global.developmentMode':False,
        'server.address':'127.0.0.1','server.port':port,'server.headless':True,
        'server.fileWatcherType':'none','browser.gatherUsageStats':False,
        'client.toolbarMode':'minimal','theme.base':'dark','theme.primaryColor':'#33c4ad'}
    # bootstrap.run installs watchers but assumes the CLI has already loaded
    # options. Without this call the first start falls back to port 8501.
    bootstrap.load_config_options(options)
    bootstrap.run(str(code_root()/'dashboard/research_app.py'),False,[],options)


def main(argv=None):
    if sys.stdout is None or sys.stderr is None:
        log_folder=app_data()/'logs'
        log_folder.mkdir(exist_ok=True)
        log=(log_folder/'application.log').open('a',encoding='utf-8',buffering=1)
        sys.stdout=sys.stdout or log
        sys.stderr=sys.stderr or log
    parser=argparse.ArgumentParser()
    parser.add_argument('--serve',action='store_true')
    parser.add_argument('--workspace')
    parser.add_argument('--port',type=int)
    parser.add_argument('--self-test',metavar='OUTPUT_JSON')
    args=parser.parse_args(argv)
    workspace=default_workspace(args.workspace)
    if args.serve:
        if not args.port or not 1024 <= args.port <= 65535:
            raise ValueError('A local server port is required.')
        serve(workspace,args.port)
        return 0
    if args.self_test:
        # Exercise installed imports, archives, chart serialization and pages,
        # without opening a GUI, sending orders or touching monitor state.
        from dashboard.research_model import ARCHIVES, load_bars, diagnostics, price_charts
        from dashboard.replay import source_fingerprints
        from desktop.verification import verify_pages
        result=dict(version=VERSION,frozen=bool(getattr(sys,'frozen',False)),workspace=str(workspace),checks={})
        for symbol,relative in ARCHIVES.items():
            frame=load_bars(workspace/relative,symbol)
            item=diagnostics(frame,symbol)
            result['checks'][symbol]=dict(rows=len(frame),charts=[len(chart.to_json()) for chart in price_charts(item)])
        result['logic_files']=len(source_fingerprints(code_root()))
        server=LocalServer(workspace)
        try:
            result['server_url']=server.start()
            result['server_ready']=True
            result['pages']=verify_pages(server.url)
        finally:
            server.close()
        result['server_closed']=server.process is None
        Path(args.self_test).write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        return 0
    import webview
    from webview import menu
    webview.settings['ALLOW_DOWNLOADS']=True
    server=LocalServer(workspace)
    window=webview.create_window(f'Project Quant — Monitor & Research {VERSION}',
        html='<body style="background:#0e1520;color:#e8eef5;font:18px Segoe UI;padding:60px"><h1>Project Quant</h1><p>Opening your local workspace…</p></body>',
        width=1400,height=920,min_size=(1000,680),background_color='#0e1520',text_select=True)
    lock=threading.Lock()

    def start_server():
        try:
            with lock:
                window.load_url(server.start())
        except Exception as exc:
            window.load_html(f'<body style="font:17px Segoe UI;padding:40px"><h1>Could not open workspace</h1><p>{html.escape(str(exc))}</p><p>Use File → Open workspace to choose your Project Quant folder.</p></body>')

    def choose_workspace():
        nonlocal server
        selected=window.create_file_dialog(webview.FileDialog.FOLDER,directory=str(workspace))
        if selected:
            try:
                folder=save_workspace(selected[0])
                with lock:
                    server.close()
                    server=LocalServer(folder)
                    window.load_url(server.start())
            except Exception as exc:
                window.create_confirmation_dialog('Workspace could not be opened',str(exc))

    menus=[menu.Menu('File',[menu.MenuAction('Open workspace…',choose_workspace),
                            menu.MenuAction('Reload',lambda: window.load_url(server.url) if server.url else None),
                            menu.MenuSeparator(),menu.MenuAction('Exit',window.destroy)])]
    try:
        webview.start(start_server,gui='edgechromium',debug=False,menu=menus,private_mode=False,
                      storage_path=str(app_data()/'webview'),icon=str(resources()/'desktop/assets/ProjectQuant.ico'))
    finally:
        server.close()
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception:
        (app_data()/'desktop_error.log').write_text(traceback.format_exc(),encoding='utf-8')
        if getattr(sys,'frozen',False) and '--serve' not in sys.argv and '--self-test' not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,'Project Quant could not start. See %LOCALAPPDATA%\\ProjectQuant\\desktop_error.log.','Project Quant',16)
        raise
