"""Desktop settings, bundled resources and an owned local server process."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

VERSION = '0.8.1'


def resources() -> Path:
    return Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]


def code_root() -> Path:
    return resources()/'source' if getattr(sys, 'frozen', False) else resources()


def app_data() -> Path:
    path = Path(os.environ.get('LOCALAPPDATA', Path.home()/'AppData/Local'))/'ProjectQuant'
    path.mkdir(parents=True, exist_ok=True)
    return path


def validate_workspace(path) -> Path:
    folder = Path(path).expanduser().resolve()
    if not folder.is_dir() or not (folder/'data/raw').is_dir():
        raise ValueError('Choose the Project Quant folder containing data/raw and your reports/logs.')
    return folder


def save_workspace(path) -> Path:
    folder = validate_workspace(path)
    settings = app_data()/'settings.json'
    temporary = settings.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(version=1, workspace=str(folder)), indent=2)+'\n', encoding='utf-8')
    temporary.replace(settings)
    return folder


def default_workspace(explicit=None) -> Path:
    if explicit:
        return validate_workspace(explicit)
    settings = app_data()/'settings.json'
    if settings.is_file():
        try:
            return validate_workspace(json.loads(settings.read_text(encoding='utf-8'))['workspace'])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    candidates = [code_root(), Path.home()/'Projects/Project Quant', Path.home()/'Projects/ProjectQuant']
    for path in candidates:
        try:
            return save_workspace(path)
        except ValueError:
            continue
    folder = app_data()/'workspace'
    seed = resources()/'seed_workspace'
    if seed.is_dir():
        # Never overwrite user datasets on upgrades or subsequent starts.
        for source in seed.rglob('*'):
            if source.is_file():
                target = folder/source.relative_to(seed)
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    (folder/'data/raw').mkdir(parents=True, exist_ok=True)
    return save_workspace(folder)


class OwnedJob:
    """Kill only children assigned by this app when the app/job handle closes."""
    def __init__(self):
        self.handle = None
        if os.name != 'nt':
            return
        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', IO),
                        ('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),
                        ('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p,wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,wintypes.HANDLE]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api = api
        self.handle = api.CreateJobObjectW(None,None)
        info = Extended()
        # The display server remains owned by this job. Only explicitly launched
        # DEMO workers use CREATE_BREAKAWAY_FROM_JOB to survive closing the UI.
        info.BasicLimitInformation.LimitFlags = 0x2000 | 0x0800
        if not self.handle or not api.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
            self.close()
            raise OSError('Cannot create isolated desktop server job.')

    def assign(self, process):
        if self.handle and not self.api.AssignProcessToJobObject(self.handle,wintypes.HANDLE(int(process._handle))):
            raise OSError('Cannot isolate the local desktop server.')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class LocalServer:
    def __init__(self, workspace):
        self.workspace = validate_workspace(workspace)
        self.process = None
        self.job = None
        self.port = None
        self.url = None

    def start(self, timeout=45):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0))
            self.port = probe.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        args = ['--serve','--workspace',str(self.workspace),'--port',str(self.port)]
        frozen = bool(getattr(sys,'frozen',False))
        # The Windows venv redirector spawns another interpreter before job
        # assignment. Use the actual interpreter for source-mode child servers
        # and pass the existing package paths explicitly to avoid an orphan.
        interpreter = getattr(sys,'_base_executable',sys.executable)
        command = ([sys.executable,*args] if frozen else
                   [interpreter,'-B',str(code_root()/'desktop/main.py'),*args])
        environment = dict(os.environ,PROJECT_QUANT_ROOT=str(self.workspace),PROJECT_QUANT_CODE_ROOT=str(code_root()),
                           PROJECT_QUANT_DESKTOP='1',PYTHONDONTWRITEBYTECODE='1')
        if not frozen:
            environment['PYTHONPATH'] = os.pathsep.join(str(path) for path in sys.path if path)
        folder = app_data()/'logs'
        folder.mkdir(exist_ok=True)
        self.job = OwnedJob()
        try:
            with (folder/'server.log').open('ab') as log:
                self.process = subprocess.Popen(command,cwd=code_root(),stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                    env=environment,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            self.job.assign(self.process)
            opener = build_opener(ProxyHandler({}))
            deadline = time.monotonic()+timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError('The local server stopped. See %LOCALAPPDATA%/ProjectQuant/logs/server.log.')
                try:
                    with opener.open(self.url+'/_stcore/health',timeout=1) as response:
                        if response.status == 200 and response.read() == b'ok':
                            (app_data()/'desktop_session.json').write_text(json.dumps(dict(parent_pid=os.getpid(),
                                server_pid=self.process.pid,url=self.url,workspace=str(self.workspace),version=VERSION),indent=2),encoding='utf-8')
                            return self.url
                except OSError:
                    time.sleep(.15)
            raise TimeoutError('The local desktop server did not become ready within 45 seconds.')
        except Exception:
            self.close()
            raise

    def close(self):
        if self.job:
            self.job.close()  # Own child tree only, including the Python venv launcher.
            self.job = None
        if self.process:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
            self.process = None
