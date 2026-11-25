# Build with the isolated .venv-desktop environment on Windows x64.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH).parent
stage = root/'build/desktop-stage'
datas = []
for name in ('streamlit','plotly','webview','pythonnet','clr_loader'):
    datas += collect_data_files(name)
    datas += copy_metadata(name if name != 'webview' else 'pywebview')
datas += [(str(stage/'source'),'source'),(str(stage/'seed_workspace'),'seed_workspace'),
          (str(root/'desktop/assets/ProjectQuant.ico'),'desktop/assets'),
          (str(root/'docs/DESKTOP_APP.md'),'.')]
a = Analysis([str(root/'desktop/main.py')],pathex=[str(root)],datas=datas,binaries=[],
    # Streamlit inserts this import into the script AST at runtime, so static
    # dependency analysis cannot see it even when research_app is collected.
    hiddenimports=['dashboard.research_app','dashboard.history','desktop.runtime',
                   'streamlit.runtime.scriptrunner.magic_funcs'],
    excludes=['MetaTrader5','yfinance','pytest','IPython','notebook','matplotlib','scipy','sklearn',
              'PySide6','PyQt5','PyQt6','cefpython3','tkinter'],
    noarchive=False,optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz,a.scripts,[],exclude_binaries=True,name='ProjectQuant',debug=False,
          bootloader_ignore_signals=False,strip=False,upx=False,console=False,
          icon=str(root/'desktop/assets/ProjectQuant.ico'),
          version=str(root/'packaging/windows_version.txt'))
coll = COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='ProjectQuant')
