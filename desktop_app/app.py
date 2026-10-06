import ctypes
import json
import math
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog
from tkinter import font as tkfont
import uuid

from core import Engine, parse_hf_url, file_url, file_page_url, resolve_file, list_repo, output_path, safe_piece, detect_model_type, validate_proxy, describe_error, save_json, human_size, remove_completed_identity

BASE = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
RESOURCES = Path(getattr(sys, '_MEIPASS', BASE))
MODE = 8
APP_VERSION = '1.0.1'
STATES = {'staged':'○ 等待确认', 'preparing':'… 读取信息', 'active':'● 下载中', 'waiting':'○ 排队中', 'paused':'Ⅱ 已暂停', 'error':'! 下载失败', 'complete':'✓ 已完成', 'checking':'◇ 校验中', 'removing':'… 正在移除'}
TOKEN_CREDENTIAL = 'HFDesktopDownloader.Token.v1'
QUEUE_SCHEMA_VERSION = 1
UI_COLOR = {
    'canvas':'#f5f7f8', 'surface':'#ffffff', 'text':'#27343b',
    'muted':'#586870', 'border':'#dce4e8', 'accent':'#246f9b',
    'focus':'#428fb7',
}
UI_FONT = ('Microsoft YaHei UI',10)

class _WinPoint(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]

class _WinRect(ctypes.Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long)]

class _MonitorInfo(ctypes.Structure):
    _fields_ = [('cbSize', ctypes.c_uint32), ('rcMonitor', _WinRect),
                ('rcWork', _WinRect), ('dwFlags', ctypes.c_uint32)]

def monitor_work_area(widget, x=None, y=None):
    """返回指定屏幕坐标所在显示器的工作区，坐标允许为负数。"""
    if x is None or y is None:
        owner=widget.winfo_toplevel()
        owner.update_idletasks()
        x=owner.winfo_rootx()+max(1,owner.winfo_width())//2
        y=owner.winfo_rooty()+max(1,owner.winfo_height())//2
    if os.name=='nt':
        try:
            user32=ctypes.WinDLL('user32',use_last_error=True)
            user32.MonitorFromPoint.argtypes=[_WinPoint,ctypes.c_uint32]
            user32.MonitorFromPoint.restype=ctypes.c_void_p
            user32.GetMonitorInfoW.argtypes=[ctypes.c_void_p,ctypes.POINTER(_MonitorInfo)]
            user32.GetMonitorInfoW.restype=ctypes.c_int
            monitor=user32.MonitorFromPoint(_WinPoint(round(x),round(y)),2)
            info=_MonitorInfo();info.cbSize=ctypes.sizeof(_MonitorInfo)
            if monitor and user32.GetMonitorInfoW(monitor,ctypes.byref(info)):
                work=info.rcWork
                return work.left,work.top,work.right,work.bottom
        except (AttributeError,OSError,ValueError):
            pass
    try:
        left=widget.winfo_vrootx();top=widget.winfo_vrooty()
        return left,top,left+widget.winfo_vrootwidth(),top+widget.winfo_vrootheight()
    except tk.TclError:
        return 0,0,widget.winfo_screenwidth(),widget.winfo_screenheight()

def fit_window_rect(x, y, width, height, work_area, padding=8):
    """缩放并限制矩形，使其完整落在同一显示器的工作区内。"""
    left,top,right,bottom=(int(value) for value in work_area)
    padding=max(0,int(padding))
    available_width=max(1,right-left-padding*2)
    available_height=max(1,bottom-top-padding*2)
    width=min(max(1,int(width)),available_width)
    height=min(max(1,int(height)),available_height)
    x=min(max(int(x),left+padding),right-padding-width)
    y=min(max(int(y),top+padding),bottom-padding-height)
    return x,y,width,height

def place_toplevel(window, x, y, width=None, height=None, work_area=None, padding=0):
    """按绝对虚拟桌面坐标放置窗口，并把原生标题栏和边框计入可见范围。"""
    if width is not None and height is not None:
        window.geometry(f'{int(width)}x{int(height)}')
    window.update_idletasks()
    if os.name=='nt':
        try:
            user32=ctypes.WinDLL('user32',use_last_error=True)
            user32.GetAncestor.argtypes=[ctypes.c_void_p,ctypes.c_uint]
            user32.GetAncestor.restype=ctypes.c_void_p
            user32.SetWindowPos.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_int,ctypes.c_int,
                                          ctypes.c_int,ctypes.c_int,ctypes.c_uint]
            user32.SetWindowPos.restype=ctypes.c_int
            user32.GetWindowRect.argtypes=[ctypes.c_void_p,ctypes.POINTER(_WinRect)]
            user32.GetWindowRect.restype=ctypes.c_int
            handle=user32.GetAncestor(int(window.winfo_id()),2) or int(window.winfo_id())
            native_rect=_WinRect()
            if work_area and user32.GetWindowRect(handle,ctypes.byref(native_rect)):
                left,top,right,bottom=(int(value) for value in work_area)
                padding=max(0,int(padding))
                max_outer_width=max(1,right-left-padding*2)
                max_outer_height=max(1,bottom-top-padding*2)
                outer_width=native_rect.right-native_rect.left
                outer_height=native_rect.bottom-native_rect.top
                if width is not None and height is not None and (outer_width>max_outer_width or outer_height>max_outer_height):
                    width=max(1,int(width)-max(0,outer_width-max_outer_width))
                    height=max(1,int(height)-max(0,outer_height-max_outer_height))
                    window.geometry(f'{width}x{height}')
                    window.update_idletasks()
                    user32.GetWindowRect(handle,ctypes.byref(native_rect))
                    outer_width=native_rect.right-native_rect.left
                    outer_height=native_rect.bottom-native_rect.top
                x=min(max(int(x),left+padding),right-padding-outer_width)
                y=min(max(int(y),top+padding),bottom-padding-outer_height)
            if user32.SetWindowPos(handle,None,int(x),int(y),0,0,0x0001|0x0004|0x0010):
                return
        except (AttributeError,OSError,ValueError,tk.TclError):
            pass
    # Tk 在 Windows 上会把负坐标解释成“距右/下边缘”；该回退主要用于其他平台。
    window.geometry(f'{int(x):+d}{int(y):+d}')

def normalize_comfy_favorite(value):
    if not isinstance(value,str):return None
    text=value.replace('\\','/').strip()
    if not text or text=='.' or text.startswith('/') or re.match(r'^[A-Za-z]:',text):return None
    if any(ord(char)<32 for char in text) or ':' in text:return None
    path=PurePosixPath(text)
    if path.is_absolute() or any(part in ('','.','..') for part in path.parts):return None
    return '/'.join(path.parts)

def comfy_repo_target(models, repo_info, file_info, chosen_rel, flatten=False):
    relative=chosen_rel.replace('\\','/') if isinstance(chosen_rel,str) else ''
    if relative!='.' and normalize_comfy_favorite(relative)!=relative:
        raise ValueError('请选择有效的 ComfyUI 模型目录。')
    destination=Path(models) if relative=='.' else Path(models)/relative
    if not destination.is_dir():raise ValueError('选择的 ComfyUI 模型目录不存在。')
    filename=file_info['filename']
    base=repo_info.get('folder','').strip('/')
    if base:
        if not filename.startswith(base+'/'):raise ValueError('文件不在当前仓库目录下。')
        filename=filename[len(base)+1:]
    parts=filename.split('/')
    if not parts or any(part in ('','.','..') or '\\' in part for part in parts):
        raise ValueError('仓库文件路径不安全。')
    if not flatten:destination/=safe_piece(repo_info['repo'].split('/')[-1])
    for part in (parts[-1:] if flatten else parts):destination/=safe_piece(part)
    return validate_target_path(destination)

def original_page_url(job):
    for candidate in (job.get('source_page_url'),job.get('source_url')):
        if not isinstance(candidate,str) or not candidate:continue
        try:return file_page_url(parse_hf_url(candidate))
        except (TypeError,ValueError):pass
    return file_page_url(job)

def proxy_from_windows_settings(enabled, server):
    if not enabled or not isinstance(server,str):return ''
    addresses={}
    for part in server.split(';'):
        part=part.strip()
        if not part:continue
        if '=' in part:
            scheme,address=part.split('=',1)
            addresses[scheme.strip().lower()]=address.strip()
        else:addresses.setdefault('default',part)
    for scheme in ('https','http','default'):
        try:
            if addresses.get(scheme):return validate_proxy(addresses[scheme])
        except ValueError:pass
    return ''

def detect_windows_proxy():
    if os.name!='nt':return ''
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
            enabled=winreg.QueryValueEx(key,'ProxyEnable')[0]
            server=winreg.QueryValueEx(key,'ProxyServer')[0]
        return proxy_from_windows_settings(enabled,server)
    except (OSError,TypeError,ValueError):return ''

def default_download_directory():
    """把用户下载内容放在程序目录之外，移动或更新程序时不受影响。"""
    return Path.home()/'Downloads'/'HFDownloader'

def normalize_settings(payload, warnings=None):
    warnings=warnings if warnings is not None else []
    if not isinstance(payload,dict):
        warnings.append('设置文件格式无效，已恢复默认设置。')
        return {}
    result={}
    invalid=False
    default_folder=payload.get('default_folder',True)
    if not isinstance(default_folder,bool):default_folder=True;invalid=True
    folder=payload.get('folder','')
    if isinstance(folder,str) and folder.strip():result['folder']=folder.strip()
    elif not default_folder:default_folder=True;invalid=True
    result['default_folder']=default_folder
    if 'proxy' in payload:
        try:
            if not isinstance(payload['proxy'],str):raise ValueError()
            result['proxy']=validate_proxy(payload['proxy'])
        except (TypeError,ValueError):
            invalid=True
    try:parallel=int(payload.get('parallel',3))
    except (TypeError,ValueError):parallel=3;invalid=True
    if parallel not in (1,2,3,4,5,6,8):parallel=3;invalid=True
    result['parallel']=parallel
    try:connections=int(payload.get('connections',MODE))
    except (TypeError,ValueError):connections=MODE;invalid=True
    if connections not in (8,16):connections=MODE;invalid=True
    result['connections']=connections
    comfy_root=payload.get('comfy_root','')
    result['comfy_root']=comfy_root if isinstance(comfy_root,str) else ''
    if not isinstance(comfy_root,str):invalid=True
    diffusion_dir=payload.get('diffusion_dir','diffusion_models')
    if diffusion_dir not in ('diffusion_models','unet'):diffusion_dir='diffusion_models';invalid=True
    result['diffusion_dir']=diffusion_dir
    favorites=payload.get('comfy_favorites',[])
    if not isinstance(favorites,list):favorites=[];invalid=True
    result['comfy_favorites']=favorites
    try:input_rows=int(payload.get('input_rows',3))
    except (TypeError,ValueError):input_rows=3;invalid=True
    result['input_rows']=max(3,min(18,input_rows))
    if result['input_rows']!=input_rows:invalid=True
    if invalid:warnings.append('设置中的部分内容无效，已恢复默认值。')
    return result

def extract_hf_urls(text):
    trailing='.,;:!\'"，。；：！？、）》】}〉'
    return list(dict.fromkeys(url.rstrip(trailing) for url in re.findall(r'https://[^\s<>\]\),;!\'"，。；：！？、）》】}〉]+',text)
                              if url.rstrip(trailing)))

class DaemonWorkerPool:
    """只运行少量后台 I/O 任务；关闭窗口时不让未结束的网络请求卡住进程。"""
    def __init__(self, max_workers=3):
        self.tasks=queue.Queue()
        self.stopping=threading.Event()
        self.threads=[]
        for index in range(max_workers):
            worker=threading.Thread(target=self._run,name=f'HFDownloader-worker-{index+1}',daemon=True)
            worker.start();self.threads.append(worker)

    def _run(self):
        while True:
            func=self.tasks.get()
            try:
                if func is None:return
                if not self.stopping.is_set():func()
            finally:self.tasks.task_done()

    def submit(self, func):
        if not self.stopping.is_set():self.tasks.put(func)

    def shutdown(self, wait=False, cancel_futures=True):
        if self.stopping.is_set():return
        self.stopping.set()
        if cancel_futures:
            while True:
                try:self.tasks.get_nowait();self.tasks.task_done()
                except queue.Empty:break
        for _ in self.threads:self.tasks.put(None)
        if wait:
            for worker in self.threads:worker.join()

def normalize_queue_payload(payload, warnings=None):
    warnings=warnings if warnings is not None else []
    if isinstance(payload,dict):
        version=payload.get('schema_version',1)
        if isinstance(version,int) and version>QUEUE_SCHEMA_VERSION:
            warnings.append('队列文件来自更新版本，已尽量兼容读取。')
        rows=payload.get('jobs',[])
    elif isinstance(payload,list):
        rows=payload
    else:
        warnings.append('队列文件格式无效，已忽略。')
        return []
    if not isinstance(rows,list):
        warnings.append('队列任务列表格式无效，已忽略。')
        return []
    result=[];seen_ids=set();seen_paths=set()
    for raw in rows:
        if not isinstance(raw,dict) or not all(isinstance(raw.get(key),str) and raw.get(key) for key in ('repo','filename','path')):
            warnings.append('已跳过一条不完整的队列记录。')
            continue
        job=dict(raw)
        raw_path=job['path']
        try:
            if any(ord(char)<32 for char in raw_path) or raw_path.startswith(('\\\\?\\','\\\\.\\')):
                raise ValueError()
            path=Path(os.path.expanduser(raw_path))
            if not path.is_absolute() or any(part in ('.','..') for part in path.parts):raise ValueError()
            normalized_path=os.path.normpath(str(path))
            if os.name=='nt' and len(normalized_path)>240:raise ValueError()
        except (OSError,TypeError,ValueError):
            warnings.append('已跳过一条路径无效的队列记录。')
            continue
        ident=job.get('id') if isinstance(job.get('id'),str) and job.get('id') else uuid.uuid4().hex
        if ident in seen_ids:
            ident=uuid.uuid4().hex
            warnings.append('发现重复的任务 ID，已自动修复。')
        seen_ids.add(ident);job['id']=ident
        path_key=os.path.normcase(normalized_path)
        if path_key in seen_paths:
            warnings.append('已跳过一条保存位置重复的队列记录。')
            continue
        seen_paths.add(path_key);job['path']=normalized_path
        job['kind']=job.get('kind','model') if job.get('kind') in ('model','dataset') else 'model'
        job['revision']=job.get('revision','main') if isinstance(job.get('revision'),str) else 'main'
        job['folder']=job.get('folder','') if isinstance(job.get('folder'),str) else ''
        try:connections=int(job.get('connections',MODE))
        except (TypeError,ValueError):connections=MODE
        job['connections']=connections if connections in (8,16) else MODE
        for key in ('downloaded','size','speed'):
            try:job[key]=max(0,int(job.get(key,0)))
            except (TypeError,ValueError):job[key]=0
        try:job['elapsed']=max(0,float(job.get('elapsed',0)))
        except (TypeError,ValueError):job['elapsed']=0
        job['status']=job.get('status','paused') if job.get('status') in STATES else 'paused'
        job['priority']=job.get('priority') is True
        job['replace_existing']=job.get('replace_existing') is True
        job['error']=job.get('error','') if isinstance(job.get('error'),str) else ''
        result.append(job)
    return result

def validate_target_path(path):
    target=Path(path).expanduser().resolve()
    if os.name=='nt' and len(str(target))>240:
        raise ValueError('保存路径过长（超过 240 个字符），请选择更短的目录或文件夹名。')
    return target

def ensure_download_capacity(path, total, committed=0):
    total=max(0,int(total or 0))
    if not total:return
    final=Path(path)
    partial=final.with_name(final.name+'.hfdownload')
    existing=partial.stat().st_size if partial.exists() else 0
    remaining=max(0,total-existing)
    if not remaining:return
    probe=final.parent
    while not probe.exists() and probe!=probe.parent:probe=probe.parent
    free=shutil.disk_usage(probe).free
    reserve=max(512*1024**2,min(2*1024**3,total//50))
    required=remaining+max(0,int(committed or 0))+reserve
    if free<required:
        raise ValueError(f'磁盘空间不足：当前文件还需 {human_size(remaining)}，连同其他队列任务和安全余量需要 {human_size(required)}，当前只有 {human_size(free)}。')

def human_duration(seconds):
    seconds=max(0, int(seconds))
    if seconds < 60:return f'{seconds}\u79d2'
    minutes, seconds=divmod(seconds,60)
    if minutes < 60:return f'{minutes}\u5206{seconds:02d}\u79d2'
    hours, minutes=divmod(minutes,60)
    if hours < 100:return f'{hours}\u5c0f\u65f6{minutes:02d}\u5206'
    return f'{hours // 24}\u5929{hours % 24}\u5c0f\u65f6'

def client_animations_enabled():
    if os.name!='nt':return False
    enabled=ctypes.c_int()
    try:
        return bool(ctypes.windll.user32.SystemParametersInfoW(0x1042,0,ctypes.byref(enabled),0) and enabled.value)
    except (AttributeError,OSError):
        return False

def proportional_right_widths(widths, index, requested, minimum=160):
    widths=[int(width) for width in widths]
    if not 0<=index<len(widths)-1:return widths
    suffix=widths[index:]
    if sum(suffix)<minimum*len(suffix):return widths
    chosen=max(minimum,min(int(requested),sum(suffix)-minimum*(len(suffix)-1)))
    right=widths[index+1:]
    remaining=sum(suffix)-chosen
    result=[minimum]*len(right)
    flexible=set(range(len(right)))
    while flexible:
        weight=sum(right[i] for i in flexible)
        limited={i for i in flexible if remaining*right[i]/weight<minimum}
        if not limited:break
        flexible-=limited
        remaining-=minimum*len(limited)
    if flexible:
        weight=sum(right[i] for i in flexible)
        shares={i:remaining*right[i]/weight for i in flexible}
        for i in flexible:result[i]=int(shares[i])
        spare=remaining-sum(result[i] for i in flexible)
        for i in sorted(flexible,key=lambda item:shares[item]-result[item],reverse=True)[:spare]:result[i]+=1
    return widths[:index]+[chosen]+result

def destination_conflicts(planned, queued_paths):
    seen=set()
    conflicts=[]
    for _,target in planned:
        key=str(target).casefold()
        if key in seen or key in queued_paths or target.exists():conflicts.append(target)
        seen.add(key)
    return conflicts

def resolve_destination_plan(planned, queued_paths, choice):
    if choice not in ('overwrite','keep','skip'):raise ValueError('未选择同名文件处理方式。')
    if choice=='overwrite' and any(str(target).casefold() in queued_paths for _,target in planned):
        raise ValueError('队列中已有相同保存位置的任务，不能覆盖正在排队或下载的文件。')
    last={str(target).casefold():index for index,(_,target) in enumerate(planned)}
    occupied=set(queued_paths)
    resolved=[]
    for index,(item,target) in enumerate(planned):
        if target.is_dir():raise ValueError('保存位置是文件夹，不能作为文件覆盖：'+str(target))
        key=str(target).casefold()
        if choice=='overwrite':
            if last[key]!=index:continue
            resolved.append((item,target,target.exists()))
        elif choice=='skip':
            if key in occupied or target.exists():continue
            occupied.add(key);resolved.append((item,target,False))
        else:
            candidate=target;number=2
            while str(candidate).casefold() in occupied or candidate.exists():
                candidate=target.with_name(f'{target.stem} ({number}){target.suffix}')
                number+=1
            occupied.add(str(candidate).casefold());resolved.append((item,candidate,False))
    return resolved

def bind_proportional_sashes(paned, minimum=160):
    drag={}
    def press(event):
        try:index=int(paned.identify(event.x,event.y))
        except (TypeError,ValueError):return
        panes=[paned.nametowidget(name) for name in paned.panes()]
        if not 0<=index<len(panes)-1:return
        widths=[pane.winfo_width() for pane in panes]
        offsets=[paned.sashpos(i)-sum(widths[:i+1]) for i in range(len(panes)-1)]
        drag.update(index=index,start_x=event.x,widths=widths,offsets=offsets)
        return 'break'
    def move(event):
        if not drag:return
        widths=proportional_right_widths(drag['widths'],drag['index'],
                                         drag['widths'][drag['index']]+event.x-drag['start_x'],minimum)
        for index in range(len(widths)-1):
            paned.sashpos(index,sum(widths[:index+1])+drag['offsets'][index])
        return 'break'
    def release(_event):
        if drag:drag.clear();return 'break'
    paned.bind('<ButtonPress-1>',press,add='+')
    paned.bind('<B1-Motion>',move,add='+')
    paned.bind('<ButtonRelease-1>',release,add='+')

class SlimScrollbar(tk.Canvas):
    def __init__(self, parent, command):
        super().__init__(parent, width=12, bg='#ffffff', bd=0, highlightthickness=0)
        self.command=command
        self.first=0.0
        self.last=1.0
        self.drag_y=None
        self.drag_first=0.0
        self.bind('<Configure>',lambda _:self.redraw())
        self.bind('<Button-1>',self.press)
        self.bind('<B1-Motion>',self.drag)
        self.bind('<ButtonRelease-1>',self.release)

    def set(self, first, last):
        self.first=max(0.0,min(1.0,float(first)))
        self.last=max(self.first,min(1.0,float(last)))
        self.redraw()

    def thumb(self):
        height=max(1,self.winfo_height()-8)
        length=max(24,height*(self.last-self.first))
        length=min(height,length)
        top=4+(height-length)*self.first/max(0.001,1-(self.last-self.first))
        return top,min(4+height,top+length)

    def redraw(self):
        self.delete('all')
        if self.last-self.first>=0.999:return
        height=self.winfo_height()
        if height<12:return
        self.create_line(6,4,6,height-4,fill='#eef0f2',width=6,capstyle='round')
        top,bottom=self.thumb()
        self.create_line(6,top+3,6,bottom-3,fill='#b6bac1',width=6,capstyle='round')

    def press(self, event):
        top,bottom=self.thumb()
        if top<=event.y<=bottom:
            self.drag_y=event.y
            self.drag_first=self.first
        else:
            self.command('scroll',-1 if event.y<top else 1,'pages')

    def drag(self, event):
        if self.drag_y is None:return
        height=max(1,self.winfo_height()-8)
        length=min(height,max(24,height*(self.last-self.first)))
        travel=max(1,height-length)
        fraction=(event.y-self.drag_y)/travel*(1-(self.last-self.first))
        self.command('moveto',max(0.0,min(1.0,self.drag_first+fraction)))

    def release(self, _event):
        self.drag_y=None

def rounded_tile(master, fill, border, radius=8, size=32):
    image=tk.PhotoImage(master=master,width=size,height=size)
    for y in range(size):
        edge=min(y,size-1-y)
        cut=0 if edge>=radius else math.ceil(radius-math.sqrt(max(0,radius*radius-(radius-edge-.5)**2)))
        image.put(border,to=(cut,y,size-cut,y+1))
        if 0<y<size-1 and size-cut*2>2:
            image.put(fill,to=(cut+1,y,size-cut-1,y+1))
    return image

def save_mode_switch(parent, mode, background='#ffffff'):
    switch=tk.Canvas(parent,width=330,height=36,bg=background,highlightthickness=0,
                     bd=0,cursor='hand2',takefocus=1)
    def draw(*_):
        if not switch.winfo_exists():return
        switch.delete('all')
        switch.create_oval(1,1,35,35,fill='#edf3f7',outline='#d3e0e7')
        switch.create_oval(295,1,329,35,fill='#edf3f7',outline='#d3e0e7')
        switch.create_rectangle(18,1,312,35,fill='#edf3f7',outline='#edf3f7')
        switch.create_line(18,1,312,1,fill='#d3e0e7')
        switch.create_line(18,35,312,35,fill='#d3e0e7')
        left=3 if mode.get()=='structure' else 165
        switch.create_oval(left,3,left+32,33,fill='#cfe8fa',outline='#8dc5ed')
        switch.create_oval(left+130,3,left+162,33,fill='#cfe8fa',outline='#8dc5ed')
        switch.create_rectangle(left+16,3,left+146,33,fill='#cfe8fa',outline='#cfe8fa')
        switch.create_text(84,18,text='保留原有文件夹结构',fill='#175a82' if mode.get()=='structure' else '#557083',
                           font=('Microsoft YaHei UI',9,'bold' if mode.get()=='structure' else 'normal'))
        switch.create_text(246,18,text='下载到同一目录',fill='#175a82' if mode.get()=='flat' else '#557083',
                           font=('Microsoft YaHei UI',9,'bold' if mode.get()=='flat' else 'normal'))
        if switch.focus_get() is switch:switch.create_line(10,34,320,34,fill=UI_COLOR['focus'],width=2)
    trace_id=mode.trace_add('write',draw)
    def cleanup(event):
        if event.widget is switch:
            try:mode.trace_remove('write',trace_id)
            except tk.TclError:pass
    switch.bind('<Destroy>',cleanup)
    def choose(event):
        switch.focus_set();mode.set('structure' if event.x<165 else 'flat')
    switch.bind('<Button-1>',choose)
    switch.bind('<FocusIn>',draw)
    switch.bind('<FocusOut>',draw)
    switch.bind('<Left>',lambda _:mode.set('structure') or 'break')
    switch.bind('<Right>',lambda _:mode.set('flat') or 'break')
    switch.bind('<space>',lambda _:mode.set('flat' if mode.get()=='structure' else 'structure') or 'break')
    draw()
    return switch

class RoundedSurface(tk.Canvas):
    def __init__(self,parent,padding=(12,6),background='#f5f7f8',stretch_y=False,fill='#ffffff',outline='#dfe6ea',shadow='#e6ebee',content_style='Card.TFrame'):
        super().__init__(parent,bg=background,bd=0,highlightthickness=0,height=1)
        self.stretch_y=stretch_y
        self.fill=fill;self.outline=outline;self.shadow=shadow
        self.content=ttk.Frame(self,style=content_style,padding=padding)
        self.window=self.create_window((4,4),window=self.content,anchor='nw')
        self.bind('<Configure>',self.redraw)
        self.content.bind('<Configure>',self.fit_height)
        self.after_idle(self.fit_height)

    def fit_height(self,_event=None):
        if self.stretch_y:return
        height=self.content.winfo_reqheight()+8
        if self.winfo_height()!=height:self.configure(height=height)

    def redraw(self,event=None):
        width=max(1,self.winfo_width())
        height=max(1,self.winfo_height())
        self.itemconfigure(self.window,width=max(1,width-8))
        if self.stretch_y:self.itemconfigure(self.window,height=max(1,height-8))
        self.delete('surface')
        if width<18 or height<18:return
        def shape(y0,y1,color,outline=''):
            r=8;x0=1;x1=width-2
            points=(x0+r,y0,x1-r,y0,x1,y0+r,x1,y1-r,x1-r,y1,x0+r,y1,x0,y1-r,x0,y0+r)
            self.create_polygon(points,smooth=True,splinesteps=16,fill=color,outline=outline,tags='surface')
        shape(3,height-1,self.shadow)
        shape(1,height-3,self.fill,self.outline)
        self.tag_lower('surface',self.window)

class RoundedTextField(tk.Canvas):
    def __init__(self,parent,rows):
        super().__init__(parent,bg='#ffffff',bd=0,highlightthickness=0,height=1)
        self.focused=False
        self.text=tk.Text(self,height=rows,wrap='word',bg='#ffffff',fg='#27343b',insertbackground='#246f9b',relief='flat',borderwidth=0,highlightthickness=0,padx=7,pady=3,font=('Microsoft YaHei UI',10))
        self.window=self.create_window((7,7),window=self.text,anchor='nw')
        self.bind('<Configure>',self.redraw)
        self.text.bind('<Configure>',self.fit_height,add='+')
        self.text.bind('<FocusIn>',self.focus_in,add='+')
        self.text.bind('<FocusOut>',self.focus_out,add='+')
        self.after_idle(self.fit_height)

    def fit_height(self,_event=None):
        height=self.text.winfo_reqheight()+14
        if self.winfo_height()!=height:self.configure(height=height)

    def focus_in(self,_event=None):
        self.focused=True;self.redraw()

    def focus_out(self,_event=None):
        self.focused=False;self.redraw()

    def redraw(self,_event=None):
        width=max(1,self.winfo_width());height=max(1,self.winfo_height())
        self.itemconfigure(self.window,width=max(1,width-14))
        self.delete('field')
        if width<18 or height<18:return
        r=8;x0=1;x1=width-2;y0=1;y1=height-2
        points=(x0+r,y0,x1-r,y0,x1,y0+r,x1,y1-r,x1-r,y1,x0+r,y1,x0,y1-r,x0,y0+r)
        self.create_polygon(points,smooth=True,splinesteps=16,fill='#ffffff',outline='#428fb7' if self.focused else '#dce4e8',tags='field')
        self.tag_lower('field',self.window)

class ModernSearch(tk.Canvas):
    def __init__(self,parent,variable):
        super().__init__(parent,height=38,bg='#ffffff',bd=0,highlightthickness=0,cursor='xterm')
        self.variable=variable
        self.focused=False
        self.entry=tk.Entry(self,textvariable=variable,bd=0,highlightthickness=0,relief='flat',bg='#ffffff',fg='#27343b',insertbackground='#246f9b',font=('Microsoft YaHei UI',10))
        self.window=self.create_window((38,19),window=self.entry,anchor='w')
        self.clear_button=tk.Label(self,text='×',bg='#ffffff',fg='#69869a',font=('Microsoft YaHei UI',15),cursor='hand2')
        self.clear_button.bind('<Button-1>',self.clear)
        self.clear_button.bind('<Enter>',lambda _:self.clear_button.configure(fg='#246f9b'))
        self.clear_button.bind('<Leave>',lambda _:self.clear_button.configure(fg='#69869a'))
        self.hint=tk.Label(self.entry,text='\u641c\u7d22\u672c\u680f\u6587\u4ef6\u5939',bg='#ffffff',fg='#5d6d76',font=('Microsoft YaHei UI',10),cursor='xterm')
        self.hint.bind('<Button-1>',lambda _:self.entry.focus_set())
        self.bind('<Configure>',self.redraw)
        self.bind('<Button-1>',lambda _:self.entry.focus_set())
        self.entry.bind('<FocusIn>',self.focus_in)
        self.entry.bind('<FocusOut>',self.focus_out)
        self.variable.trace_add('write',lambda *_:self.sync_hint())
        self.after_idle(self.sync_hint)

    def focus_in(self,_event=None):
        self.focused=True;self.sync_hint();self.redraw()

    def focus_out(self,_event=None):
        self.focused=False;self.sync_hint();self.redraw()

    def clear(self,_event=None):
        self.variable.set('')
        self.entry.focus_set()
        return 'break'

    def sync_hint(self):
        if self.variable.get() or self.focused:self.hint.place_forget()
        else:self.hint.place(x=0,rely=.5,anchor='w')
        if self.variable.get():self.clear_button.place(relx=1,x=-9,rely=.5,anchor='e')
        else:self.clear_button.place_forget()

    def redraw(self,_event=None):
        width=max(1,self.winfo_width());height=max(1,self.winfo_height())
        self.itemconfigure(self.window,width=max(1,width-76))
        self.delete('search')
        if width<30:return
        x0=1;x1=width-2;y0=1;y1=height-2;r=8
        points=(x0+r,y0,x1-r,y0,x1,y0+r,x1,y1-r,x1-r,y1,x0+r,y1,x0,y1-r,x0,y0+r)
        self.create_polygon(points,smooth=True,splinesteps=16,fill='#ffffff',outline='#428fb7' if self.focused else '#dce4e8',tags='search')
        self.create_oval(13,12,23,22,outline='#69869a',width=1.6,tags='search')
        self.create_line(22,21,27,26,fill='#69869a',width=1.6,capstyle='round',tags='search')
        self.tag_lower('search',self.window)

class ModernDropdown(tk.Canvas):
    def __init__(self,parent,variable,placeholder='\u9009\u62e9\u6536\u85cf\u76ee\u5f55'):
        super().__init__(parent,height=40,bg='#ffffff',bd=0,highlightthickness=0,cursor='hand2',takefocus=1)
        self.variable=variable
        self.placeholder=placeholder
        self.values=[]
        self.popup=None
        self.hovered=False
        self.active_index=0
        self.font=tkfont.Font(family='Microsoft YaHei UI',size=10)
        self.variable.trace_add('write',lambda *_:self.redraw())
        self.bind('<Configure>',self.redraw)
        self.bind('<Enter>',self.hover_in)
        self.bind('<Leave>',self.hover_out)
        self.bind('<Button-1>',self.toggle)
        self.bind('<Return>',self.toggle)
        self.bind('<space>',self.toggle)
        self.bind('<Down>',self.open_popup)
        self.bind('<FocusIn>',self.redraw)
        self.bind('<FocusOut>',self.redraw)

    def __setitem__(self,key,value):
        if key=='values':
            self.values=list(value)
            if self.popup:self.close_popup()
            self.redraw()
        else:super().__setitem__(key,value)

    def __getitem__(self,key):
        return tuple(self.values) if key=='values' else super().__getitem__(key)

    def get(self):
        return self.variable.get()

    def set(self,value):
        self.variable.set(value)

    def hover_in(self,_event=None):
        self.hovered=True;self.redraw()

    def hover_out(self,_event=None):
        self.hovered=False;self.redraw()

    def redraw(self,_event=None):
        width=max(1,self.winfo_width());height=max(1,self.winfo_height())
        self.delete('select')
        if width<28:return
        x0=1;x1=width-2;y0=1;y1=height-2;r=8
        points=(x0+r,y0,x1-r,y0,x1,y0+r,x1,y1-r,x1-r,y1,x0+r,y1,x0,y1-r,x0,y0+r)
        self.create_polygon(points,smooth=True,splinesteps=16,fill='#f4f6f8' if self.hovered or self.popup else UI_COLOR['surface'],outline=UI_COLOR['focus'] if self.popup or self.focus_get() is self else '#b8cfdd' if self.hovered else UI_COLOR['border'],width=2 if self.focus_get() is self else 1,tags='select')
        value=self.variable.get()
        if value:
            available=width-56
            if self.font.measure(value)>available:
                start=0
                while start<len(value) and self.font.measure('\u2026'+value[start:])>available:start+=1
                value='\u2026'+value[start:]
        else:value=self.placeholder
        self.create_text(14,height/2,text=value,anchor='w',fill='#27343b' if self.variable.get() else '#5d6d76',font=self.font,tags='select')
        x=width-21;y=height/2
        self.create_polygon(x-5,y-2,x+5,y-2,x,y+3,fill='#61727b',outline='',tags='select')

    def toggle(self,_event=None):
        if self.popup:self.close_popup()
        else:self.open_popup()
        return 'break'

    def open_popup(self,_event=None):
        if self.popup or not self.values:return 'break'
        width=max(180,self.winfo_width())
        visible=min(8,len(self.values))
        height=visible*38+18
        x=self.winfo_rootx()
        anchor_y=self.winfo_rooty()
        work_area=monitor_work_area(self,x+self.winfo_width()//2,anchor_y+self.winfo_height()//2)
        below=anchor_y+self.winfo_height()+4
        above=anchor_y-height-4
        y=below if below+height<=work_area[3]-8 or above<work_area[1]+8 else above
        x,y,width,height=fit_window_rect(x,y,width,height,work_area)
        popup=tk.Toplevel(self);popup.withdraw()
        popup.overrideredirect(True)
        popup.configure(bg='#f5f7f8')
        place_toplevel(popup,x,y,width,height,work_area,8)
        self.popup=popup
        surface=tk.Canvas(popup,bg='#f5f7f8',bd=0,highlightthickness=0)
        surface.pack(fill='both',expand=True)
        def draw_surface(event=None):
            w=max(1,surface.winfo_width());h=max(1,surface.winfo_height())
            surface.delete('chrome')
            for offset,fill,outline in ((4,'#e6ebee',''),(0,'#ffffff','#dce4e8')):
                x0=1+offset;x1=w-5+offset;y0=1+offset;y1=h-5+offset;r=8
                points=(x0+r,y0,x1-r,y0,x1,y0+r,x1,y1-r,x1-r,y1,x0+r,y1,x0,y1-r,x0,y0+r)
                surface.create_polygon(points,smooth=True,splinesteps=16,fill=fill,outline=outline,tags='chrome')
            surface.tag_lower('chrome',window)
        rows=tk.Canvas(surface,bg='#ffffff',bd=0,highlightthickness=0,takefocus=1)
        window=surface.create_window((7,7),window=rows,anchor='nw',width=width-18,height=height-18)
        surface.bind('<Configure>',draw_surface)
        rows.configure(scrollregion=(0,0,width-18,len(self.values)*38))
        self.active_index=self.values.index(self.variable.get()) if self.variable.get() in self.values else 0
        def draw_rows():
            rows.delete('option')
            w=max(1,rows.winfo_width());top=rows.canvasy(0);bottom=top+rows.winfo_height()
            for index in range(max(0,int(top//38)),min(len(self.values),int(bottom//38)+2)):
                y0=index*38+2;y1=y0+34
                if index==self.active_index:
                    left=4;right=w-5;r=7
                    points=(left+r,y0,right-r,y0,right,y0+r,right,y1-r,right-r,y1,left+r,y1,left,y1-r,left,y0+r)
                    rows.create_polygon(points,smooth=True,splinesteps=12,fill='#e7f1f7',outline='',tags='option')
                rows.create_text(14,index*38+19,text=self.values[index],anchor='w',fill='#164964' if index==self.active_index else '#27343b',font=self.font,tags='option')
        def motion(event):
            index=int(rows.canvasy(event.y)//38)
            if 0<=index<len(self.values) and index!=self.active_index:self.active_index=index;draw_rows()
        def choose(event=None):
            index=int(rows.canvasy(event.y)//38) if event and hasattr(event,'y') else self.active_index
            if 0<=index<len(self.values):
                selected=self.values[index]
                self.close_popup()
                self.variable.set(selected)
                self.event_generate('<<ComboboxSelected>>')
        def move(delta):
            self.active_index=max(0,min(len(self.values)-1,self.active_index+delta))
            rows.yview_moveto(max(0,(self.active_index-3)*38)/max(1,len(self.values)*38))
            draw_rows()
            return 'break'
        def wheel(event):
            rows.yview_scroll(-int(event.delta/120) if event.delta else 0,'units')
            draw_rows()
            return 'break'
        rows.bind('<Configure>',lambda _:draw_rows())
        rows.bind('<Motion>',motion)
        rows.bind('<ButtonRelease-1>',choose)
        rows.bind('<MouseWheel>',wheel)
        rows.bind('<Up>',lambda _:move(-1))
        rows.bind('<Down>',lambda _:move(1))
        rows.bind('<Return>',lambda _:choose())
        rows.bind('<Escape>',lambda _:self.close_popup())
        popup.bind('<Escape>',lambda _:self.close_popup())
        popup.bind('<ButtonPress-1>',lambda event:self.close_popup() if event.widget is not rows else None)
        popup.bind('<FocusOut>',lambda _:self.after(50,lambda:self.close_popup() if self.popup is popup else None))
        popup.update_idletasks()
        draw_surface();draw_rows();self.redraw()
        popup.deiconify();popup.lift();popup.grab_set();rows.focus_set()
        return 'break'

    def close_popup(self):
        if not self.popup:return
        popup=self.popup
        self.popup=None
        try:popup.grab_release()
        except tk.TclError:pass
        popup.destroy()
        self.redraw()

    def destroy(self):
        self.close_popup()
        super().destroy()

class EditContextMenu(tk.Toplevel):
    def __init__(self, owner, actions):
        super().__init__(owner)
        self.withdraw()
        self.owner=owner
        self.actions=actions
        self.rows=[]
        self.separators=[]
        self.active=None
        self._outside_binding=None
        self.width=204
        self.height=172
        self.overrideredirect(True)
        self.configure(bg='#f5f7f8')
        self.surface=tk.Canvas(self,width=self.width,height=self.height,bg='#f5f7f8',bd=0,highlightthickness=0,takefocus=1)
        self.surface.pack()
        y=10
        for label,shortcut,command,enabled in actions:
            if label is None:
                self.separators.append(y+4)
                y+=8
            else:
                self.rows.append((y,y+36,label,shortcut,command,enabled))
                y+=36
        self.height=y+10
        self.surface.configure(height=self.height)
        self.surface.bind('<Motion>',self.hover)
        self.surface.bind('<Leave>',lambda _:self.set_active(None))
        self.surface.bind('<ButtonRelease-1>',self.click)
        self.surface.bind('<Down>',lambda _:self.move(1))
        self.surface.bind('<Up>',lambda _:self.move(-1))
        self.surface.bind('<Return>',lambda _:self.choose_active())
        self.surface.bind('<Escape>',lambda _:self.close())
        self.surface.bind('<Control-x>',lambda _:self.choose_label('剪切'))
        self.surface.bind('<Control-c>',lambda _:self.choose_label('复制'))
        self.surface.bind('<Control-v>',lambda _:self.choose_label('粘贴'))
        self.surface.bind('<Control-a>',lambda _:self.choose_label('全选'))
        self.bind('<FocusOut>',lambda _:self.after(60,self.close_if_unfocused))
        self.draw()

    def show(self, x, y):
        work_area=monitor_work_area(self.owner,x,y)
        x,y,width,height=fit_window_rect(x,y,self.width,self.height,work_area)
        place_toplevel(self,x,y,width,height,work_area,8)
        self.deiconify();self.lift()
        self.surface.focus_set()
        owner_window=self.owner.winfo_toplevel()
        self._outside_binding=(owner_window,owner_window.bind('<ButtonPress-1>',lambda _:self.close(),add='+'))

    def draw(self):
        canvas=self.surface
        canvas.delete('all')
        width=self.width;height=self.height
        for offset,fill,outline in ((4,'#dfe5e9',''),(0,'#ffffff','#dce4e8')):
            x0=1+offset;x1=width-5+offset;y0=1+offset;y1=height-5+offset;r=9
            points=(x0+r,y0,x1-r,y0,x1,y0+r,x1,y1-r,x1-r,y1,x0+r,y1,x0,y1-r,x0,y0+r)
            canvas.create_polygon(points,smooth=True,splinesteps=16,fill=fill,outline=outline)
        for index,(top,bottom,label,shortcut,_command,enabled) in enumerate(self.rows):
            if index==self.active and enabled:
                x0=8;x1=width-12;y0=top+2;y1=bottom-2;r=7
                points=(x0+r,y0,x1-r,y0,x1,y0+r,x1,y1-r,x1-r,y1,x0+r,y1,x0,y1-r,x0,y0+r)
                canvas.create_polygon(points,smooth=True,splinesteps=12,fill='#e7f1f7',outline='')
            color='#27343b' if enabled else '#9ba8ae'
            canvas.create_text(20,(top+bottom)/2,text=label,anchor='w',fill=color,font=('Microsoft YaHei UI',10))
            canvas.create_text(width-22,(top+bottom)/2,text=shortcut,anchor='e',fill='#697983' if enabled else '#b3bdc2',font=('Microsoft YaHei UI',10))
        for separator in self.separators:
            canvas.create_line(18,separator,width-22,separator,fill='#e6ebee',width=1)

    def set_active(self, index):
        if self.active!=index:self.active=index;self.draw()

    def hover(self, event):
        index=next((i for i,(top,bottom,*rest) in enumerate(self.rows) if top<=event.y<bottom and rest[-1]),None)
        self.set_active(index)

    def click(self, event):
        if not (0<=event.x<self.width and 0<=event.y<self.height):return self.close()
        self.hover(event)
        self.choose_active()
        return 'break'

    def move(self, direction):
        enabled=[i for i,row in enumerate(self.rows) if row[-1]]
        if enabled:
            position=enabled.index(self.active) if self.active in enabled else (-1 if direction>0 else 0)
            self.set_active(enabled[(position+direction)%len(enabled)])
        return 'break'

    def choose_active(self):
        if self.active is not None:
            command=self.rows[self.active][4]
            self.close()
            self.owner.focus_set()
            command()
        return 'break'

    def choose_label(self, label):
        self.set_active(next((i for i,row in enumerate(self.rows) if row[2]==label and row[-1]),None))
        return self.choose_active()

    def close_if_unfocused(self):
        if self.winfo_exists() and self.focus_get() not in (self,self.surface):self.close()

    def close(self):
        if self._outside_binding:
            owner_window,binding_id=self._outside_binding
            self._outside_binding=None
            try:owner_window.unbind('<ButtonPress-1>',binding_id)
            except tk.TclError:pass
        if self.winfo_exists():
            try:self.grab_release()
            except tk.TclError:pass
            self.destroy()
        return 'break'

class FolderList(tk.Canvas):
    def __init__(self,parent):
        super().__init__(parent,bg=UI_COLOR['surface'],bd=0,highlightthickness=0,cursor='hand2',yscrollincrement=32,takefocus=1)
        self.items=[]
        self.selected=None
        self.hovered=None
        self._text_font=tkfont.Font(family='Microsoft YaHei UI',size=10)
        self._tip_after=None
        self._tip_window=None
        self.row_height=32
        self.bind('<Configure>',lambda _:self.redraw())
        self.bind('<Button-1>',self.pick)
        self.bind('<Motion>',self.hover)
        self.bind('<Leave>',self.leave)
        self.bind('<MouseWheel>',self.wheel)
        self.bind('<Up>',lambda _:self.step(-1))
        self.bind('<Down>',lambda _:self.step(1))
        self.bind('<FocusIn>',lambda _:self.redraw())
        self.bind('<FocusOut>',lambda _:self.redraw())

    def insert(self,index,value):
        if index=='end':self.items.append(value)
        else:self.items.insert(int(index),value)
        self.redraw()

    def delete(self,first,last=None):
        self.hide_name_tip()
        if first==0 and last=='end':self.items.clear();self.selected=None;self.hovered=None
        else:
            end=first if last is None else len(self.items)-1 if last=='end' else int(last)
            del self.items[int(first):end+1]
            self.selected=None;self.hovered=None
        super().yview_moveto(0)
        self.redraw()

    def size(self):
        return len(self.items)

    def get(self,index):
        return self.items[int(index)]

    def curselection(self):
        return (self.selected,) if self.selected is not None else ()

    def selection_set(self,index):
        index=int(index)
        if 0<=index<len(self.items):self.selected=index;self.redraw()

    def selection_clear(self,first=0,last='end'):
        self.selected=None;self.redraw()

    def see(self,index):
        index=int(index);top=self.canvasy(0);height=self.winfo_height();bottom=top+height
        y=index*self.row_height
        if y<top or y+self.row_height>bottom:
            visible_rows=max(1,height//self.row_height)
            first_row=max(0,index-visible_rows//2)
            super().yview_moveto(0)
            if first_row:
                super().yview_scroll(first_row,'units')
            self.redraw()

    def yview(self,*args):
        result=super().yview(*args)
        if args:self.redraw()
        return result

    def yview_moveto(self,fraction):
        super().yview_moveto(fraction);self.redraw()

    def pick(self,event):
        index=int(self.canvasy(event.y)//self.row_height)
        if 0<=index<len(self.items):
            self.selected=index
            self.focus_set()
            self.redraw()

    def hover(self,event):
        index=int(self.canvasy(event.y)//self.row_height)
        index=index if 0<=index<len(self.items) else None
        if index!=self.hovered:
            self.hide_name_tip()
            self.hovered=index
            if index is not None and self._text_font.measure(self.items[index])>max(1,self.winfo_width()-64):
                self._tip_after=self.after(550,lambda index=index:self.show_name_tip(index))
            self.redraw()

    def leave(self,_event=None):
        self.hide_name_tip()
        if self.hovered is not None:self.hovered=None;self.redraw()

    def hide_name_tip(self):
        if self._tip_after is not None:self.after_cancel(self._tip_after);self._tip_after=None
        if self._tip_window is not None:self._tip_window.destroy();self._tip_window=None

    def show_name_tip(self,index):
        self._tip_after=None
        if not self.winfo_exists() or self.hovered!=index or not 0<=index<len(self.items):return
        self._tip_window=tk.Toplevel(self);self._tip_window.withdraw()
        self._tip_window.overrideredirect(True)
        self._tip_window.attributes('-topmost',True)
        tk.Label(self._tip_window,text=self.items[index],bg='#27343b',fg='#ffffff',padx=9,pady=5,
                 wraplength=420,justify='left',font=UI_FONT).pack()
        self._tip_window.update_idletasks()
        pointer_x=self.winfo_pointerx();pointer_y=self.winfo_pointery()
        width=self._tip_window.winfo_reqwidth();height=self._tip_window.winfo_reqheight()
        area=monitor_work_area(self,pointer_x,pointer_y)
        x,y,width,height=fit_window_rect(pointer_x+12,pointer_y+14,width,height,area)
        place_toplevel(self._tip_window,x,y,width,height,area,8)
        self._tip_window.deiconify();self._tip_window.lift()

    def step(self,delta):
        if not self.items:return 'break'
        current=self.selected if self.selected is not None else (-1 if delta>0 else len(self.items))
        self.selected=max(0,min(len(self.items)-1,current+delta))
        self.see(self.selected);self.redraw()
        return 'break'

    def wheel(self,event):
        self.yview_scroll(-int(event.delta/120) if event.delta else 0,'units')
        self.redraw()
        return 'break'

    def redraw(self):
        width=max(1,self.winfo_width())
        height=max(1,self.winfo_height())
        self.configure(scrollregion=(0,0,width,max(height,len(self.items)*self.row_height)))
        super().delete('row')
        first=max(0,int(self.canvasy(0)//self.row_height))
        last=min(len(self.items),int((self.canvasy(0)+height)//self.row_height)+2)
        for index in range(first,last):
            top=index*self.row_height
            if index==self.selected or index==self.hovered:
                x0=4;x1=width-6;y0=top+2;y1=top+self.row_height-2;r=7
                points=(x0+r,y0,x1-r,y0,x1,y0+r,x1,y1-r,x1-r,y1,x0+r,y1,x0,y1-r,x0,y0+r)
                self.create_polygon(points,smooth=True,splinesteps=12,fill='#b5d7ef' if index==self.selected else '#f4f6f7',outline='#4f95c6' if index==self.selected else '',width=1,tags='row')
            color='#163f5b' if index==self.selected else '#344550'
            middle=top+self.row_height/2
            folder='#5aa9e6' if index==self.selected else '#83bce8'
            self.create_polygon(9,middle-6,15,middle-6,17,middle-4,25,middle-4,25,middle+6,9,middle+6,fill=folder,outline='',tags='row')
            self.create_line(10,middle-3,24,middle-3,fill='#cde5f7',width=1,tags='row')
            name=self.items[index]
            available=max(1,width-64)
            if self._text_font.measure(name)>available:
                low,high=0,len(name)
                while low<high:
                    mid=(low+high+1)//2
                    if self._text_font.measure(name[:mid]+'…')<=available:low=mid
                    else:high=mid-1
                name=name[:low]+'…'
            self.create_text(34,middle,text=name,anchor='w',fill=color,font=self._text_font,tags='row')
            self.create_text(width-17,top+self.row_height/2,text='\u203a',anchor='center',fill='#236b9f' if index==self.selected else '#73828a',font=('Segoe UI Symbol',11),tags='row')
        super().delete('focus')
        if self.focus_get() is self:self.create_rectangle(1,1,width-2,height-2,outline=UI_COLOR['focus'],width=2,tags='focus')

class Tooltip:
    def __init__(self,widget,text):
        self.widget=widget
        self.text=text
        self.window=None
        widget.bind('<Enter>',self.show,add='+')
        widget.bind('<Leave>',self.hide,add='+')
        widget.bind('<ButtonPress-1>',self.hide,add='+')

    def show(self,_event=None):
        if self.window:return
        content=self.text() if callable(self.text) else self.text
        if not content:return
        self.window=tk.Toplevel(self.widget);self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes('-topmost',True)
        anchor_x=self.widget.winfo_rootx();anchor_y=self.widget.winfo_rooty()
        area=monitor_work_area(self.widget,anchor_x+self.widget.winfo_width()//2,anchor_y+self.widget.winfo_height()//2)
        tk.Label(self.window,text=content,bg='#27343b',fg='#ffffff',padx=9,pady=5,
                 wraplength=min(720,max(280,area[2]-area[0]-80)),justify='left',
                 font=('Microsoft YaHei UI',10)).pack()
        self.window.update_idletasks()
        width=self.window.winfo_reqwidth();height=self.window.winfo_reqheight()
        below=anchor_y+self.widget.winfo_height()+5
        above=anchor_y-height-5
        y=below if below+height<=area[3]-8 or above<area[1]+8 else above
        x,y,width,height=fit_window_rect(anchor_x,y,width,height,area)
        place_toplevel(self.window,x,y,width,height,area,8)
        self.window.deiconify();self.window.lift()

    def hide(self,_event=None):
        if self.window:self.window.destroy();self.window=None

class Credential(ctypes.Structure):
    _fields_ = [('Flags', ctypes.c_uint32), ('Type', ctypes.c_uint32), ('TargetName', ctypes.c_wchar_p),
                ('Comment', ctypes.c_wchar_p), ('LastWritten', ctypes.c_byte * 8), ('CredentialBlobSize', ctypes.c_uint32),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_byte)), ('Persist', ctypes.c_uint32), ('AttributeCount', ctypes.c_uint32),
                ('Attributes', ctypes.c_void_p), ('TargetAlias', ctypes.c_wchar_p), ('UserName', ctypes.c_wchar_p)]

def load_saved_token():
    if os.name != 'nt': return ''
    credential=ctypes.POINTER(Credential)()
    try:
        api=ctypes.WinDLL('Advapi32.dll')
        ok=api.CredReadW(TOKEN_CREDENTIAL, 1, 0, ctypes.byref(credential))
        if not ok:return ''
        size=credential.contents.CredentialBlobSize
        return ctypes.string_at(credential.contents.CredentialBlob, size).decode('utf-16-le') if size else ''
    finally:
        if credential:
            ctypes.WinDLL('Advapi32.dll').CredFree(credential)

def save_token(token):
    if os.name != 'nt': raise RuntimeError('仅支持 Windows 凭据管理器保存 Token。')
    blob=token.encode('utf-16-le')
    memory=ctypes.create_string_buffer(blob)
    credential=Credential(0, 1, TOKEN_CREDENTIAL, None, (ctypes.c_byte * 8)(), len(blob), ctypes.cast(memory, ctypes.POINTER(ctypes.c_byte)), 2, 0, None, None, 'HF Downloader')
    if not ctypes.WinDLL('Advapi32.dll',use_last_error=True).CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())

def delete_saved_token():
    if os.name != 'nt':return
    api=ctypes.WinDLL('Advapi32.dll',use_last_error=True)
    if not api.CredDeleteW(TOKEN_CREDENTIAL,1,0):
        code=ctypes.get_last_error()
        if code!=1168:raise ctypes.WinError(code)

def data_directory():
    target=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'HF下载器'
    target.mkdir(parents=True,exist_ok=True)
    legacy=BASE/'data'
    marker=target/'migration-v1.done'
    if not marker.exists() and legacy.resolve()!=target.resolve() and legacy.is_dir():
        migration_ok=True
        for name in ('settings.json','settings.json.bak','queue.json','queue.json.bak'):
            source=legacy/name;destination=target/name
            try:
                if not source.is_file():continue
                json.loads(source.read_text(encoding='utf-8'))
                if destination.exists() and destination.stat().st_mtime>=source.stat().st_mtime:continue
                if destination.exists():shutil.copy2(destination,destination.with_name(destination.name+'.pre-migration'))
                shutil.copy2(source,destination)
            except (OSError,ValueError):migration_ok=False
        if migration_ok:
            try:marker.write_text('1',encoding='ascii')
            except OSError:pass
    return target

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        # 初始化完成前不显示窗口，避免用户在下载引擎刚启动、关闭回调尚未就绪时退出，
        # 从而留下孤立的 aria2c 进程。
        self.withdraw()
        self.closing = False
        self.stop = threading.Event()
        self.workers = None
        self.engine = None
        self.protocol('WM_DELETE_WINDOW', self.close_app)
        self.title(f'\u0048\u0046 \u6a21\u578b\u4e0b\u8f7d\u5668 v{APP_VERSION}')
        screen_width=self.winfo_screenwidth();screen_height=self.winfo_screenheight()
        window_width=max(760,min(1600,screen_width-80));window_height=max(560,min(1075,screen_height-80))
        self.geometry(f'{window_width}x{window_height}')
        self.minsize(min(1040,window_width),min(800,window_height))
        self.configure(bg='#f5f7f8')
        self.data = data_directory()
        self.events = queue.Queue()
        self.workers = DaemonWorkerPool(max_workers=3)
        self.jobs = {}
        self.last_poll = time.monotonic()
        self.load_warnings=[]
        self.cfg = normalize_settings(self.load('settings.json', {}),self.load_warnings)
        default_folder = default_download_directory()
        self.default_downloads = default_folder
        folder = str(default_folder) if self.cfg.get('default_folder', True) else self.cfg.get('folder', str(default_folder))
        self.folder = tk.StringVar(value=folder)
        self.proxy = tk.StringVar(value=self.cfg['proxy'] if 'proxy' in self.cfg else detect_windows_proxy())
        self.token = tk.StringVar(value=load_saved_token())
        self.parallel = tk.StringVar(value=str(self.cfg.get('parallel', 3)))
        saved_mode=str(self.cfg.get('connections', MODE))
        self.connections=tk.StringVar(value=saved_mode if saved_mode in ('8','16') else str(MODE))
        self.comfy_root=tk.StringVar(value=self.cfg.get('comfy_root',''))
        self.diffusion_dir=tk.StringVar(value=self.cfg.get('diffusion_dir','diffusion_models'))
        self.comfy_favorites=[]
        for value in self.cfg.get('comfy_favorites',[]):
            favorite=normalize_comfy_favorite(value)
            if favorite and favorite not in self.comfy_favorites:self.comfy_favorites.append(favorite)
        self.note = tk.StringVar(value='粘贴文件链接即可下载；仓库链接会先显示文件清单。')
        self.summary = tk.StringVar(value='0 个任务')
        self.speed = tk.StringVar(value='0 B/s')
        if self.load_warnings:self.note.set('\uff1b'.join(self.load_warnings))
        self.details = tk.StringVar(value='选中任务可查看完整文件名、保存位置和错误信息。')
        self.setup_style()
        self.build_ui()
        binary = RESOURCES / 'vendor' / 'aria2c.exe'
        try:
            self.engine = Engine(binary, self.data, int(self.parallel.get()))
        except Exception as exc:
            self.notice_dialog(self,'无法启动',describe_error(exc))
            self.destroy()
            raise SystemExit(1)
        for job in normalize_queue_payload(self.load('queue.json', []),self.load_warnings):
            job['gid'] = None
            job['model_type']=detect_model_type(job['filename'])
            if job['status'] == 'complete':
                remove_completed_identity(job['path'])
            else:
                job['status'] = 'paused'
            self.jobs[job['id']] = job
            self.tree.insert('', 'end', iid=job['id'])
            self.render(job)
        self.refresh_summary()
        self.selection_changed()
        if self.load_warnings:
            self.note.set('\uff1b'.join(self.load_warnings))
        elif self.jobs:
            self.note.set('已恢复任务记录。选中未完成任务，点击“继续 / 重试”即可续传。')
        threading.Thread(target=self.poller, daemon=True).start()
        self.after(100, self.drain)
        self.after(5000, self.autosave)
        self.bind('<Control-Return>', lambda e:self.add_links())
        self.deiconify()

    def load(self, name, default):
        path=self.data/name;backup=path.with_suffix(path.suffix+'.bak')
        try:return json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):
            try:
                value=json.loads(backup.read_text(encoding='utf-8'))
                self.load_warnings.append(name+' \u5df2\u4ece\u5907\u4efd\u6062\u590d\u3002')
                return value
            except (OSError,ValueError):
                if path.exists():self.load_warnings.append(name+' \u5df2\u635f\u574f\uff0c\u4e14\u6ca1\u6709\u53ef\u7528\u5907\u4efd\u3002')
                return default

    def setup_style(self):
        self.option_add('*Font', ('Microsoft YaHei UI', 10))
        self.option_add('*TCombobox*Listbox.Font', ('Microsoft YaHei UI', 10))
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI',10), background='#f5f7f8', foreground='#27343b')
        style.configure('Shell.TFrame', background='#f5f7f8')
        style.configure('Header.TFrame', background='#ffffff')
        style.configure('Header.TLabel', background='#ffffff', foreground='#27343b')
        style.configure('Brand.TLabel', background='#ffffff', foreground='#1f2f38', padding=0)
        style.configure('DarkHeader.TFrame', background='#102b3a')
        style.configure('DarkBrand.TLabel', background='#102b3a', foreground='#ffffff', padding=0)
        style.configure('DarkMeta.TLabel', background='#102b3a', foreground='#9fc9dc', font=('Microsoft YaHei UI',9))
        style.configure('HeaderMuted.TLabel', background='#ffffff', foreground='#586870', font=('Microsoft YaHei UI',10))
        style.configure('Header.TRadiobutton', background='#ffffff', foreground='#27343b', padding=(2,3))
        style.map('Header.TRadiobutton', background=[('active','#ffffff')], foreground=[('active','#164964')])
        style.configure('Controls.TLabel', background='#ffffff', foreground='#27343b')
        style.configure('ControlsMuted.TLabel', background='#ffffff', foreground='#586870', font=('Microsoft YaHei UI',10))
        style.configure('Search.TLabel', background='#ffffff', foreground='#586870')
        style.configure('Preview.TFrame', background='#ffffff')
        style.configure('Preview.TLabel', background='#ffffff', foreground='#27343b', font=('Microsoft YaHei UI',11,'bold'))
        style.configure('PreviewMuted.TLabel', background='#ffffff', foreground='#586870', font=('Microsoft YaHei UI',10))
        style.configure('Card.TFrame', background='#ffffff', relief='flat')
        style.configure('Card.TLabel', background='#ffffff', foreground='#172933')
        style.configure('Card.TRadiobutton', background='#ffffff', foreground='#172933', padding=(2,4))
        style.map('Card.TRadiobutton', background=[('active','#ffffff')])
        style.configure('Section.TLabel', background='#ffffff', foreground='#10252f', font=('Microsoft YaHei UI',12,'bold'))
        style.configure('SectionTitle.TLabel', background='#ffffff', foreground='#10252f', font=('Microsoft YaHei UI',13,'bold'))
        style.configure('QueueTitle.TLabel', background='#f5f7f8', foreground='#10252f', font=('Microsoft YaHei UI',13,'bold'))
        style.configure('QueueMeta.TLabel', background='#e7f1f7', foreground='#205b7d', font=('Microsoft YaHei UI',10,'bold'), padding=(10,5))
        style.configure('QueueDetail.TLabel',background=UI_COLOR['canvas'],foreground=UI_COLOR['muted'],font=UI_FONT)
        style.configure('SpeedLabel.TLabel', background='#f5f7f8', foreground='#586870', font=('Microsoft YaHei UI',10))
        style.configure('DialogTitle.TLabel', background='#ffffff', foreground='#10252f', font=('Microsoft YaHei UI',16,'bold'))
        style.configure('Index.TLabel', background='#e7f1f7', foreground='#205b7d', font=('Bahnschrift SemiBold',10,'bold'), padding=(8,3))
        style.configure('Muted.TLabel', foreground='#586870', background='#ffffff', font=('Microsoft YaHei UI',10))
        style.configure('Status.TLabel', background='#ffffff', foreground='#344550', padding=(8,4))
        style.configure('StatusContact.TLabel', background='#ffffff', foreground='#586870', font=('Microsoft YaHei UI',10), padding=(8,4))
        style.configure('Speed.TLabel', background='#f5f7f8', foreground='#175a82', font=('Bahnschrift SemiBold',16))
        self._style_images=[]
        def rounded_button(name,fill,border,hover,pressed,foreground,disabled='#eef2f0',bold=False):
            element='Round'+name.replace('.','')
            images=[rounded_tile(self,fill,border),rounded_tile(self,hover,border),rounded_tile(self,pressed,border),
                    rounded_tile(self,disabled,'#e5ebe8'),rounded_tile(self,fill,UI_COLOR['focus'])]
            self._style_images.extend(images)
            style.element_create(element,'image',images[0],('disabled',images[3]),('pressed',images[2]),
                                 ('focus',images[4]),('active',images[1]),border=(8,8,8,8),sticky='nsew')
            style.layout(name,[(element,{'sticky':'nsew','children':[('Button.padding',{'sticky':'nsew','children':[('Button.label',{'sticky':'nsew'})]})]})])
            style.configure(name,padding=(12,7),foreground=foreground,font=('Microsoft YaHei UI',10,'bold' if bold else 'normal'),borderwidth=0)
            style.map(name,foreground=[('disabled','#9baca6')])
        rounded_button('TButton','#f1f4f6','#dce4e8','#e8eef1','#dce5e9','#27343b')
        rounded_button('Accent.TButton','#246f9b','#246f9b','#1d628b','#185474','#ffffff',disabled='#c8dae5',bold=True)
        rounded_button('Secondary.TButton','#f4f6f7','#dce4e8','#e9eef1','#dce5e9','#27343b')
        rounded_button('ComfyAction.TButton','#e4f1ff','#b8d8f7','#d5e9fc','#c8e0f8','#275c88',bold=True)
        rounded_button('ViewSelected.TButton','#dceef9','#9fc9e6','#cce7f7','#b9ddf2','#175a82',bold=True)
        rounded_button('Back.TButton','#e8f3ff','#9fc9ee','#d9ecff','#c9e2fa','#1f5f93',bold=True)
        rounded_button('Danger.TButton','#fff1ee','#f4d7d0','#fce5df','#f7d8d0','#a34d42')
        rounded_button('DeleteConfirm.TButton','#b94e43','#b94e43','#a74036','#91372f','#ffffff',bold=True)
        rounded_button('HeaderIcon.TButton','#f4f6f7','#dce4e8','#e9eef1','#dce5e9','#27343b')
        rounded_button('Queue.TButton','#f1f4f6','#dce4e8','#e8eef1','#dce5e9','#27343b')
        rounded_button('QueueAccent.TButton','#246f9b','#246f9b','#1d628b','#185474','#ffffff',disabled='#c8dae5',bold=True)
        rounded_button('QueueDanger.TButton','#fff1ee','#f4d7d0','#fce5df','#f7d8d0','#a34d42')
        for name in ('Queue.TButton','QueueAccent.TButton','QueueDanger.TButton'):
            style.configure(name,padding=(6,5))
        entry_images=[rounded_tile(self,'#ffffff','#dce4e8'),rounded_tile(self,'#ffffff','#428fb7')]
        self._style_images.extend(entry_images)
        style.element_create('RoundedEntry.field','image',entry_images[0],('focus',entry_images[1]),border=(8,8,8,8),sticky='nsew')
        style.layout('Rounded.TEntry',[('RoundedEntry.field',{'sticky':'nsew','children':[('Entry.padding',{'sticky':'nsew','children':[('Entry.textarea',{'sticky':'nsew'})]})]})])
        style.configure('Rounded.TEntry',fieldbackground='#ffffff',foreground='#27343b',padding=(9,6),borderwidth=0)
        style.configure('TEntry', fieldbackground='#ffffff', foreground='#27343b', padding=6, bordercolor='#dce4e8', lightcolor='#dce4e8', darkcolor='#dce4e8')
        style.map('TEntry', bordercolor=[('focus','#428fb7')], lightcolor=[('focus','#428fb7')], darkcolor=[('focus','#428fb7')])
        style.configure('TScrollbar', background='#c7d4dc', troughcolor='#f2f5f7', bordercolor='#f2f5f7', arrowcolor='#617683', arrowsize=12)
        style.map('TScrollbar', background=[('active','#9fb7c6'),('pressed','#809eaf')])
        style.configure('TCombobox', fieldbackground='#ffffff', background='#f1f4f6', padding=6, arrowsize=15, bordercolor='#dce4e8')
        style.map('TCombobox',fieldbackground=[('readonly','#ffffff')],selectbackground=[('readonly','#ffffff')],selectforeground=[('readonly','#27343b')])
        style.configure('Treeview', background='#ffffff', fieldbackground='#ffffff', foreground='#263940', rowheight=35, borderwidth=0, font=('Microsoft YaHei UI',10))
        style.configure('Treeview.Heading', background='#eef2f4', foreground='#344550', padding=(8,9), font=('Microsoft YaHei UI',10,'bold'), relief='flat')
        style.map('Treeview', background=[('selected','#dceef9')], foreground=[('selected','#175a82')])
        style.configure('Folder.Treeview', background='#ffffff', fieldbackground='#ffffff', foreground='#344550', rowheight=31, borderwidth=0, font=('Microsoft YaHei UI',10))
        style.map('Folder.Treeview', background=[('selected','#dceef9')], foreground=[('selected','#175a82')])
        style.configure('Files.Treeview', background='#ffffff', fieldbackground='#ffffff', foreground='#263940', rowheight=34, borderwidth=0, font=('Microsoft YaHei UI',10))
        style.configure('Files.Treeview.Heading', background='#eef2f4', foreground='#344550', padding=(8,8), font=('Microsoft YaHei UI',10,'bold'), relief='flat')
        style.map('Files.Treeview', background=[('selected','#dceef9')], foreground=[('selected','#175a82')])
        style.configure('Column.Treeview', background='#ffffff', fieldbackground='#ffffff', foreground='#344155', rowheight=35, borderwidth=0, relief='flat', font=('Microsoft YaHei UI',10))
        style.map('Column.Treeview', background=[('selected','#dceef9')], foreground=[('selected','#175a82')])

    def build_ui(self):
        outer = ttk.Frame(self, padding=(20,16), style='Shell.TFrame')
        outer.pack(fill='both', expand=True)
        header_surface=RoundedSurface(outer,padding=(18,10),fill='#102b3a',outline='#102b3a',shadow='#d8e1e6',content_style='DarkHeader.TFrame')
        header_surface.pack(fill='x',pady=(0,8))
        header=header_surface.content
        header.columnconfigure(1,weight=1)
        brand=ttk.Frame(header,style='DarkHeader.TFrame')
        brand.grid(row=0,column=0,sticky='w')
        mark=tk.Frame(brand,bg='#76bde3',width=4,height=27)
        mark.pack(side='left',padx=(0,10));mark.pack_propagate(False)
        ttk.Label(brand,text='HF \u6a21\u578b\u4e0b\u8f7d\u5668',style='DarkBrand.TLabel',font=('Microsoft YaHei UI',21,'bold')).pack(side='left')
        ttk.Label(brand,text='v'+APP_VERSION,style='DarkMeta.TLabel').pack(side='left',padx=(8,0),pady=(8,0))
        header_actions=ttk.Frame(header,style='DarkHeader.TFrame')
        header_actions.grid(row=0,column=2,sticky='e')
        settings_button=ttk.Button(header_actions,text='设置',style='HeaderIcon.TButton',width=4,command=self.comfy_settings)
        settings_button.pack(side='right',padx=(8,0))
        token_button=ttk.Button(header_actions,text='Token / 帮助',style='HeaderIcon.TButton',width=10,command=self.advanced)
        token_button.pack(side='right')
        Tooltip(settings_button,'\u8bbe\u7f6e')
        Tooltip(token_button,'HF Token / \u5e2e\u52a9')
        quick=tk.Frame(header,bg='#ffffff',highlightbackground='#dce4e8',highlightthickness=1,padx=6,pady=4)
        quick.grid(row=0,column=1,sticky='w',padx=(8,8))
        ttk.Label(quick,text='\u4ee3\u7406',style='Controls.TLabel').pack(side='left',padx=(0,5))
        proxy_entry=ttk.Entry(quick,textvariable=self.proxy,width=20,style='Rounded.TEntry')
        proxy_entry.pack(side='left')
        Tooltip(proxy_entry,'代理地址留空时直连')
        ttk.Label(quick,text='\u540c\u65f6',style='Controls.TLabel').pack(side='left',padx=(8,4))
        picker=ttk.Combobox(quick,textvariable=self.parallel,values=[1,2,3,4,5,6,8],width=2,state='readonly')
        picker.pack(side='left')
        picker.bind('<<ComboboxSelected>>',self.change_parallel)
        ttk.Label(quick,text='\u4e2a\u6587\u4ef6  \u00b7  \u7ebf\u8def',style='Controls.TLabel').pack(side='left',padx=(4,3))
        ttk.Radiobutton(quick,text='8 \u8def',style='Header.TRadiobutton',variable=self.connections,value='8',command=self.change_connections).pack(side='left')
        ttk.Radiobutton(quick,text='16 \u8def',style='Header.TRadiobutton',variable=self.connections,value='16',command=self.change_connections).pack(side='left',padx=(3,0))
        def arrange_header(event):
            needed=brand.winfo_reqwidth()+quick.winfo_reqwidth()+header_actions.winfo_reqwidth()+40
            compact=event.width<needed
            if compact==getattr(self,'_header_compact',None):return
            self._header_compact=compact
            if compact:quick.grid_configure(row=1,column=0,columnspan=3,sticky='ew',padx=0,pady=(8,0))
            else:quick.grid_configure(row=0,column=1,columnspan=1,sticky='w',padx=(8,8),pady=0)
        header.bind('<Configure>',arrange_header,add='+')
        card_surface=RoundedSurface(outer,padding=(16,7))
        card_surface.pack(fill='x')
        card=card_surface.content
        heading=ttk.Frame(card,style='Card.TFrame');heading.pack(fill='x',pady=(0,5))
        ttk.Label(heading,text='\u6dfb\u52a0\u4e0b\u8f7d',style='SectionTitle.TLabel').pack(side='left')
        ttk.Label(heading,text='\u6587\u4ef6\u94fe\u63a5\u76f4\u63a5\u4e0b\u8f7d \u00b7 \u4ed3\u5e93\u6216\u5b50\u76ee\u5f55\u94fe\u63a5\u5148\u9009\u6587\u4ef6',style='Muted.TLabel').pack(side='right')
        try:self.input_rows=max(3,min(18,int(self.cfg.get('input_rows',3))))
        except (TypeError,ValueError):self.input_rows=3
        input_field=RoundedTextField(card,self.input_rows)
        input_field.pack(fill='x')
        self.input=input_field.text
        input_menu=[None]
        def select_input():
            self.input.tag_add('sel','1.0','end-1c')
            self.input.mark_set('insert','1.0')
            self.input.see('insert')
        def show_input_menu(event):
            if input_menu[0] and input_menu[0].winfo_exists():input_menu[0].close()
            self.input.focus_set()
            selected=bool(self.input.tag_ranges('sel'))
            try:has_clipboard=bool(self.input.clipboard_get())
            except tk.TclError:has_clipboard=False
            actions=[('剪切','Ctrl+X',lambda:self.input.event_generate('<<Cut>>'),selected),
                     ('复制','Ctrl+C',lambda:self.input.event_generate('<<Copy>>'),selected),
                     ('粘贴','Ctrl+V',lambda:self.input.event_generate('<<Paste>>'),has_clipboard),
                     (None,None,None,None),
                     ('全选','Ctrl+A',select_input,bool(self.input.get('1.0','end-1c')))]
            input_menu[0]=EditContextMenu(self.input,actions)
            input_menu[0].show(event.x_root,event.y_root)
            return 'break'
        self.input.bind('<Button-3>',show_input_menu)
        grip=tk.Label(self.input,text='\u25e2',bg='#ffffff',fg='#667680',cursor='sb_v_double_arrow',font=('Microsoft YaHei UI',12))
        grip.place(relx=1,rely=1,anchor='se')
        drag={}
        def set_input_rows(rows,max_rows):
            rows=max(3,min(max_rows,rows))
            if rows!=self.input_rows:self.input_rows=rows;self.input.configure(height=rows)
        def begin_input_resize(event):
            drag['y']=event.y_root;drag['rows']=self.input_rows
            drag['line_height']=max(1,tkfont.Font(font=self.input.cget('font')).metrics('linespace'))
            queue_height=table_surface.winfo_height() if table_surface.winfo_exists() else 0
            drag['max_rows']=min(18,self.input_rows+max(0,queue_height-190)//drag['line_height']) if queue_height>1 else 18
        def resize_input(event):
            if 'y' not in drag:return
            rows=drag['rows']+round((event.y_root-drag['y'])/drag['line_height'])
            set_input_rows(rows,drag['max_rows'])
        def finish_input_resize(event):
            if 'y' in drag:resize_input(event);drag.clear();self.persist()
        grip.bind('<ButtonPress-1>',begin_input_resize)
        grip.bind('<B1-Motion>',resize_input)
        grip.bind('<ButtonRelease-1>',finish_input_resize)
        inputbar = ttk.Frame(card, style='Card.TFrame')
        inputbar.pack(fill='x', pady=(4,0))
        ttk.Button(inputbar, text='粘贴剪贴板',style='Secondary.TButton',command=self.paste).pack(side='left')
        ttk.Button(inputbar, text='清空输入',style='Secondary.TButton',command=lambda:self.input.delete('1.0','end')).pack(side='left', padx=8)
        path_group=ttk.Frame(inputbar,style='Card.TFrame')
        ttk.Label(path_group,text='默认保存位置',style='Card.TLabel').pack(side='left',padx=(0,6))
        folder_entry=ttk.Entry(path_group,textvariable=self.folder,style='Rounded.TEntry')
        folder_entry.pack(side='left',fill='x',expand=True)
        Tooltip(folder_entry,lambda:self.folder.get().strip())
        def show_folder_tail(*_):
            if folder_entry.winfo_exists() and folder_entry.focus_get() is not folder_entry:
                folder_entry.after_idle(lambda:folder_entry.winfo_exists() and folder_entry.xview_moveto(1))
        self.folder.trace_add('write',show_folder_tail)
        folder_entry.bind('<FocusOut>',show_folder_tail,add='+')
        folder_entry.after_idle(show_folder_tail)
        copy_folder=ttk.Button(path_group,text='复制',style='Queue.TButton',width=4,
                               command=lambda:self.copy_text(self.folder.get().strip(),'已复制默认保存位置。'))
        copy_folder.pack(side='left',padx=(4,0))
        Tooltip(copy_folder,'复制完整保存路径')
        ttk.Button(path_group,text='选择文件夹',style='Secondary.TButton',command=self.choose_folder).pack(side='left',padx=(4,0))
        self.add_button = ttk.Button(inputbar, text='添加并下载', style='Accent.TButton', command=self.add_links)
        comfy_button=ttk.Button(inputbar,text='添加并下载到 ComfyUI',style='ComfyAction.TButton',command=self.add_comfy_links)
        comfy_button.pack(side='right')
        self.add_button.pack(side='right',padx=(0,8))
        path_group.pack(side='left',fill='x',expand=True,padx=(4,12))
        toolbar = ttk.Frame(outer)
        toolbar.pack(fill='x',pady=(8,5))
        ttk.Label(toolbar,text='\u4e0b\u8f7d\u961f\u5217',style='QueueTitle.TLabel').pack(side='left')
        ttk.Label(toolbar,textvariable=self.summary,style='QueueMeta.TLabel').pack(side='left',padx=12)
        ttk.Label(toolbar,textvariable=self.speed,style='Speed.TLabel').pack(side='right')
        ttk.Label(toolbar,text='总速度',style='SpeedLabel.TLabel').pack(side='right',padx=(0,8))
        table_surface=RoundedSurface(outer,padding=(8,8),stretch_y=True)
        table_surface.pack(fill='both',expand=True)
        table=table_surface.content
        cols = ('name','status','progress','size','speed','eta','avg','mode')
        self.tree = ttk.Treeview(table,columns=cols,show='headings',selectmode='extended',height=3)
        for col,title,width in [('name','文件',255),('status','状态',100),('progress','进度',70),('size','已下载 / 总大小',140),('speed','速度',80),('eta','\u9884\u8ba1\u5269\u4f59',94),('avg','平均速度',84),('mode','连接上限',74)]:
            self.tree.heading(col,text=title)
            self.tree.column(col,width=width,minwidth=220 if col=='name' else 65,stretch=col=='name',anchor='w' if col=='name' else 'center')
        v = SlimScrollbar(table,command=self.tree.yview)
        h = ttk.Scrollbar(table,orient='horizontal',command=self.tree.xview)
        def update_horizontal(first,last):
            h.set(first,last)
            if float(first)<=0 and float(last)>=1:
                if h.winfo_manager():h.grid_remove()
            elif not h.winfo_manager():h.grid()
        self.tree.configure(yscrollcommand=v.set,xscrollcommand=update_horizontal)
        self.tree.grid(row=0,column=0,sticky='nsew'); v.grid(row=0,column=1,sticky='ns'); h.grid(row=1,column=0,sticky='ew')
        table.rowconfigure(0,weight=1);table.columnconfigure(0,weight=1)
        self.empty_queue=tk.Canvas(table,width=390,height=144,bg='#ffffff',bd=0,highlightthickness=0)
        self.empty_queue.create_rectangle(4,4,386,140,outline='#cbd6dc',dash=(5,4),width=1,fill='#ffffff')
        self.empty_queue.create_text(195,48,text='\u21e9',fill='#246f9b',font=('Segoe UI Symbol',23))
        self.empty_queue.create_text(195,78,text='\u6682\u65e0\u4e0b\u8f7d\u4efb\u52a1',fill='#27343b',font=('Microsoft YaHei UI',12,'bold'))
        self.empty_queue.create_text(195,105,text='\u7c98\u8d34 Hugging Face \u94fe\u63a5\u540e\uff0c\u4efb\u52a1\u4f1a\u663e\u793a\u5728\u8fd9\u91cc\u3002',fill='#586870',font=('Microsoft YaHei UI',10))
        self.tree.bind('<<TreeviewSelect>>',self.selection_changed)
        self.tree.bind('<Control-a>',lambda event:(self.tree.selection_set(self.tree.get_children()),'break')[1])
        self.tree.bind('<Control-A>',lambda event:(self.tree.selection_set(self.tree.get_children()),'break')[1])
        self._resize_column=None
        self._resize_widths=None
        self.tree.bind('<ButtonPress-1>',self.begin_column_resize,add='+')
        self.tree.bind('<ButtonRelease-1>',self.finish_column_resize,add='+')
        self._task_drag=None
        self.tree.bind('<ButtonPress-1>',self.begin_task_drag,add='+')
        self.tree.bind('<B1-Motion>',self.drag_select_tasks)
        self.tree.bind('<ButtonRelease-1>',self.finish_task_drag,add='+')
        self.tree.bind('<Delete>',lambda _:self.remove_and_delete_selected() or 'break')
        self.tree.bind('<Double-1>',self.double_click_task)
        self.tree.bind('<Button-3>',self.show_task_menu)
        self.task_menu=None
        self.tree.tag_configure('staged',foreground='#205b7d',background='#edf5f9')
        self.tree.tag_configure('preparing',foreground='#2563a6',background='#f2f7fc')
        self.tree.tag_configure('waiting',foreground='#66757b')
        self.tree.tag_configure('active',foreground='#25343c',background='#edf5f9')
        self.tree.tag_configure('paused',foreground='#403b33',background='#fff8e8')
        self.tree.tag_configure('checking',foreground='#2563a6',background='#eef6ff')
        self.tree.tag_configure('complete',foreground='#344550',background='#f5f7f8')
        self.tree.tag_configure('error',foreground='#3f3432',background='#fff1f0')
        detail_row=ttk.Frame(outer,style='Shell.TFrame')
        detail_row.pack(fill='x',pady=(6,0));detail_row.pack_propagate(False)
        details_label=ttk.Label(detail_row,textvariable=self.details,style='QueueDetail.TLabel',
                                anchor='w',width=1)
        details_label.pack(side='left',fill='x',expand=True)
        Tooltip(details_label,lambda:self.details.get())
        copy_detail=ttk.Button(detail_row,text='复制详情',style='Queue.TButton',
                               command=lambda:self.copy_text(self.details.get(),'已复制任务详情。'))
        copy_detail.pack(side='right',padx=(8,0))
        detail_row.configure(height=copy_detail.winfo_reqheight())
        actions = ttk.Frame(outer)
        actions.pack(fill='x',pady=(7,4))
        operations=ttk.Frame(actions,style='Shell.TFrame');operations.grid(row=0,column=0,sticky='w')
        cleanup=ttk.Frame(actions,style='Shell.TFrame');cleanup.grid(row=0,column=1,sticky='w',padx=(14,0))
        self.task_action_buttons=[]
        for text,func,style_name in [('\u6682\u505c\u9009\u4e2d',self.pause_selected,'Queue.TButton'),('\u7ee7\u7eed / \u91cd\u8bd5',self.resume_selected,'QueueAccent.TButton'),('\u6253\u5f00\u6587\u4ef6\u5939',self.open_folder,'Queue.TButton'),('\u79fb\u9664\u8bb0\u5f55',self.remove_selected,'Queue.TButton'),('\u79fb\u9664\u5e76\u5220\u9664\u6587\u4ef6',self.remove_and_delete_selected,'QueueDanger.TButton')]:
            group=operations if len(self.task_action_buttons)<3 else cleanup
            button=ttk.Button(group,text=text,style=style_name,command=func)
            button.pack(side='left',padx=(0,4))
            self.task_action_buttons.append(button)
        selection_actions=ttk.Frame(actions)
        selection_actions.grid(row=0,column=2,sticky='e')
        ttk.Button(selection_actions,text='\u5168\u9009',style='Queue.TButton',command=self.select_all_tasks).pack(side='left')
        ttk.Button(selection_actions,text='\u53cd\u9009',style='Queue.TButton',command=self.invert_task_selection).pack(side='left',padx=(6,0))
        ttk.Button(selection_actions,text='\u53d6\u6d88\u6240\u6709',style='Queue.TButton',command=self.clear_task_selection).pack(side='left',padx=(6,0))
        actions.columnconfigure(2,weight=1)
        def arrange_actions(event):
            required=operations.winfo_reqwidth()+cleanup.winfo_reqwidth()+selection_actions.winfo_reqwidth()+16
            compact=event.width<required
            if compact==getattr(self,'_actions_compact',None):return
            self._actions_compact=compact
            if compact:
                cleanup.grid_configure(row=1,column=0,padx=0,pady=(6,0))
                selection_actions.grid_configure(row=1,column=2,pady=(6,0))
            else:
                cleanup.grid_configure(row=0,column=1,padx=(14,0),pady=0)
                selection_actions.grid_configure(row=0,column=2,pady=0)
        actions.bind('<Configure>',arrange_actions,add='+')
        status_surface=RoundedSurface(outer,padding=(4,0))
        status_surface.pack(fill='x')
        statusbar=status_surface.content
        ttk.Label(statusbar,text='bug\u53cd\u9988\u6216\u5176\u4ed6\u95ee\u9898\u8054\u7cfb\u5fae\u4fe1\uff1ayujian5455',style='StatusContact.TLabel').pack(side='right',fill='y')
        ttk.Label(statusbar,textvariable=self.note,style='Status.TLabel',anchor='w').pack(side='left',fill='x',expand=True)

    def submit(self, func, success, failure=None):
        def work():
            try:
                result = func()
                self.events.put(lambda:success(result))
            except Exception as exc:
                msg = describe_error(exc)
                self.events.put(lambda msg=msg:failure(msg) if failure else self.note.set(msg))
        self.workers.submit(work)

    def drain(self):
        if self.closing:
            return
        for _ in range(200):
            try: fn=self.events.get_nowait()
            except queue.Empty: break
            try: fn()
            except Exception as exc: self.note.set('操作未完成：'+describe_error(exc))
        self.after(100,self.drain)

    def config_snapshot(self):
        folder = self.folder.get().strip()
        if not folder:
            raise ValueError('请选择保存文件夹。')
        proxy = validate_proxy(self.proxy.get())
        return folder, proxy, self.token.get().strip()

    def paste(self):
        try:
            value=self.clipboard_get()
            if self.input.get('1.0','end').strip(): self.input.insert('end','\n')
            self.input.insert('end',value)
        except tk.TclError:
            self.note.set('剪贴板中没有可粘贴的文字。')

    def choose_folder(self):
        folder=filedialog.askdirectory(parent=self,title='选择模型保存目录')
        if folder:self.folder.set(folder)

    def copy_text(self, value, message='已复制到剪贴板。'):
        if not value:return
        self.clipboard_clear();self.clipboard_append(value)
        self.note.set(message)

    def center_dialog(self, win, width, height):
        win.withdraw()
        self.update_idletasks()
        owner=win.master if isinstance(win.master,tk.Misc) and win.master.winfo_exists() else self
        owner.update_idletasks()
        center_x=owner.winfo_rootx()+max(1,owner.winfo_width())//2
        center_y=owner.winfo_rooty()+max(1,owner.winfo_height())//2
        area=monitor_work_area(owner,center_x,center_y)
        x=center_x-int(width)//2;y=center_y-int(height)//2
        x,y,width,height=fit_window_rect(x,y,width,height,area,padding=20)
        win.transient(owner)
        place_toplevel(win,x,y,width,height,area,20)
        win.grab_set()
        if client_animations_enabled():
            try:
                win.attributes('-alpha',.4)
            except tk.TclError:
                return
            def fade(step=1):
                if not win.winfo_exists():return
                try:win.attributes('-alpha',min(1.,.4+step*.1))
                except tk.TclError:return
                if step<6:win.after(30,fade,step+1)
            win.after(30,fade)

    def ask_folder_name(self, parent, prompt='在当前列新建文件夹：'):
        dialog=tk.Toplevel(parent);dialog.title('新建文件夹');self.center_dialog(dialog,500,230)
        dialog.configure(bg='#f5f7f8');dialog.transient(parent);dialog.resizable(False,False)
        surface=RoundedSurface(dialog,padding=(22,18),stretch_y=True);surface.pack(fill='both',expand=True,padx=12,pady=12)
        box=surface.content
        ttk.Label(box,text='新建文件夹',style='DialogTitle.TLabel').pack(anchor='w')
        ttk.Label(box,text=prompt,style='Muted.TLabel').pack(anchor='w',pady=(8,8))
        value=tk.StringVar();entry=ttk.Entry(box,textvariable=value,style='Rounded.TEntry',font=('Microsoft YaHei UI',10))
        entry.pack(fill='x')
        result=[]
        def accept():
            name=value.get().strip()
            if name:result.append(name);dialog.destroy()
        buttons=ttk.Frame(box,style='Card.TFrame');buttons.pack(side='bottom',fill='x',pady=(18,0))
        ttk.Button(buttons,text='\u53d6\u6d88',style='Secondary.TButton',command=dialog.destroy).pack(side='right')
        ttk.Button(buttons,text='\u521b\u5efa',style='Accent.TButton',command=accept).pack(side='right',padx=(0,8))
        dialog.bind('<Return>',lambda _:accept());dialog.bind('<Escape>',lambda _:dialog.destroy())
        dialog.protocol('WM_DELETE_WINDOW',dialog.destroy)
        dialog.deiconify();dialog.lift();dialog.grab_set();entry.focus_set();parent.wait_window(dialog)
        return result[0] if result else None

    def decision_dialog(self, parent, title, message, options, detail=''):
        previous_grab=parent.grab_current()
        dialog=tk.Toplevel(parent);dialog.withdraw();dialog.title(title)
        width=620;height=280 if detail else 250
        self.center_dialog(dialog,width,height)
        dialog.update_idletasks()
        width=dialog.winfo_width();height=dialog.winfo_height()
        if parent.winfo_exists():
            dialog.transient(parent)
        dialog.configure(bg='#f5f7f8');dialog.resizable(False,False)
        surface=RoundedSurface(dialog,padding=(24,20),stretch_y=True)
        surface.pack(fill='both',expand=True,padx=12,pady=12)
        box=surface.content
        ttk.Label(box,text=title,style='DialogTitle.TLabel').pack(anchor='w')
        wrap=max(260,width-80)
        ttk.Label(box,text=message,style='Card.TLabel',wraplength=wrap,justify='left').pack(anchor='w',pady=(12,6))
        if detail:ttk.Label(box,text=detail,style='Muted.TLabel',wraplength=wrap,justify='left').pack(anchor='w')
        buttons=ttk.Frame(box,style='Card.TFrame');buttons.pack(side='bottom',fill='x',pady=(16,0))
        result=[]
        def choose(value):
            result.append(value);dialog.destroy()
        for value,label,style_name,enabled in reversed(options):
            button=ttk.Button(buttons,text=label,style=style_name,command=lambda value=value:choose(value))
            button.pack(side='right',padx=(8,0))
            if not enabled:button.state(['disabled'])
        dialog.protocol('WM_DELETE_WINDOW',dialog.destroy)
        dialog.bind('<Escape>',lambda _:dialog.destroy())
        dialog.deiconify();dialog.lift();dialog.grab_set();parent.wait_window(dialog)
        if previous_grab is not None and previous_grab.winfo_exists():previous_grab.grab_set()
        return result[0] if result else None

    def notice_dialog(self, parent, title, message):
        self.decision_dialog(parent,title,message,[('ok','知道了','Accent.TButton',True)])

    def choose_destination_plan(self, parent, planned):
        queued={str(Path(job['path'])).casefold() for job in self.jobs.values()}
        conflicts=destination_conflicts(planned,queued)
        choice='skip'
        if conflicts:
            queued_conflict=any(str(target).casefold() in queued for target in conflicts)
            examples='、'.join(target.name for target in conflicts[:2])
            if len(conflicts)>2:examples+=f' 等 {len(conflicts)} 个文件'
            choice=self.decision_dialog(parent,'处理同名文件',
                f'这些文件会保存到相同位置，或目标文件已经存在：{examples}',
                [('overwrite','覆盖同名文件','Danger.TButton',not queued_conflict),
                 ('keep','分别保存','Accent.TButton',True),
                 ('skip','跳过冲突文件','Secondary.TButton',True)],
                '覆盖会在新文件下载完成并核对大小后替换旧文件；同批同名文件保留列表中最后一个。'
                if not queued_conflict else '队列中已有相同位置的任务，不能覆盖；可分别保存或跳过冲突文件。')
            if choice is None:return None
        return resolve_destination_plan(planned,queued,choice)

    def add_comfy_links(self):
        self.add_links(comfy=True)

    def finish_selection_session(self, session):
        if session.get('finished'):return
        session['finished']=True
        ids=[ident for ident in session['ids'] if ident in self.jobs and self.jobs[ident]['status']=='staged']
        if not ids:return
        choice='start'
        if session['cancelled']:
            choice=self.decision_dialog(self,'处理已添加任务',
                f'文件选择尚未完成，队列中已有 {len(ids)} 个本次添加的任务。',
                [('start','现在开始','Accent.TButton',True),
                 ('keep','保留到队列','Secondary.TButton',True),
                 ('remove','移除记录','Danger.TButton',True)],
                '保留到队列后，可选中任务并点击“继续 / 重试”启动；移除记录不会删除本地文件。') or 'keep'
        if choice=='remove':
            self.remove_ids(ids,False)
            self.note.set(f'已移除本次添加的 {len(ids)} 条任务记录。')
            return
        if choice=='keep':
            for ident in ids:self.jobs[ident]['status']='paused';self.render(self.jobs[ident])
            self.persist();self.selection_changed()
            self.note.set(f'已保留 {len(ids)} 个任务，暂不下载。')
            return
        for ident in ids:
            job=self.jobs[ident]
            job['status']='preparing';self.render(job)
            self.submit(lambda current=dict(job):resolve_file(current,session['proxy'],session['token']),
                        lambda meta,ident=ident:self.prepared(ident,meta,session['proxy'],session['token']),
                        lambda msg,ident=ident:self.fail(ident,msg))
        self.persist();self.selection_changed()
        self.note.set(f'已确认 {len(ids)} 个任务，正在一起准备下载。')

    def comfy_direct_sequence(self, infos, proxy, token, index=0, on_done=None, session=None):
        if session is None:session={'ids':[],'cancelled':False,'proxy':proxy,'token':token}
        if index>=len(infos):
            self.persist();self.note.set(f'\u5df2\u5904\u7406 {len(infos)} \u4e2a ComfyUI \u6587\u4ef6\u94fe\u63a5\u3002')
            if on_done:self.after_idle(on_done)
            else:self.after_idle(lambda:self.finish_selection_session(session))
            return
        root=Path(self.comfy_root.get()).expanduser();models=root/'models'
        if not models.is_dir():
            self.notice_dialog(self,'ComfyUI','请先在设置中选择 ComfyUI 根目录。');self.comfy_settings()
            session['cancelled']=True
            if on_done:self.after_idle(on_done)
            else:self.after_idle(lambda:self.finish_selection_session(session))
            return
        info=infos[index]
        def confirmed(rel, dialog):
            destination=models if rel=='.' else models/rel
            target=destination/safe_piece(PurePosixPath(info['filename']).name)
            try:session['ids'].append(self.enqueue(info,str(root),proxy,token,target,staged=True));self.persist()
            except Exception as exc:
                self.notice_dialog(dialog,'无法添加',str(exc))
                self.after_idle(lambda:self.comfy_direct_sequence(infos,proxy,token,index+1,on_done,session))
                return
            self.after_idle(lambda:self.comfy_direct_sequence(infos,proxy,token,index+1,on_done,session))
        def cancelled():
            session['cancelled']=True
            if on_done:self.after_idle(on_done)
            else:self.after_idle(lambda:self.finish_selection_session(session))
        self.comfy_column_dialog(root,models,[info],proxy,token,self.comfy_preferences(info),
                                 on_confirm=confirmed,button_text=f'\u786e\u8ba4\u5e76\u7ee7\u7eed\uff08{index+1}/{len(infos)}\uff09',on_cancel=cancelled)

    def repo_sequence(self, items, folder, proxy, token, comfy=False, index=0, session=None):
        if session is None:session={'ids':[],'cancelled':False,'proxy':proxy,'token':token}
        if index>=len(items):
            self.finish_selection_session(session);return
        info=items[index]
        def next_repo():
            self.after_idle(lambda:self.repo_sequence(items,folder,proxy,token,comfy,index+1,session))
        def failed(message):
            self.note.set(message)
            session['cancelled']=True
            next_repo()
        self.note.set(f'\u6b63\u5728\u8bfb\u53d6\u4ed3\u5e93\u6587\u4ef6\u6e05\u5355\uff08{index+1}/{len(items)}\uff09\u2026')
        self.submit(lambda:list_repo(info,proxy,token),
                    lambda files:self.repo_dialog(info,files,folder,proxy,token,comfy,on_done=next_repo,session=session),failed)

    def add_links(self, comfy=False):
        try: folder,proxy,token=self.config_snapshot()
        except Exception as exc:
            self.note.set(str(exc));return
        text=self.input.get('1.0','end').strip()
        urls=extract_hf_urls(text)
        if not urls:
            self.note.set('请先粘贴 Hugging Face 链接。');return
        accepted=0;errors=[];direct=[];repos=[]
        for url in dict.fromkeys(urls):
            try:
                info=parse_hf_url(url)
                if info['filename']:
                    if comfy:direct.append(info)
                    else:self.enqueue(info,folder,proxy,token,
                                      output_path(folder,info,flatten=True,repository_folder=False))
                else:repos.append(info)
                accepted+=1
            except Exception as exc:errors.append(describe_error(exc))
        session={'ids':[],'cancelled':False,'proxy':proxy,'token':token}
        start_repos=lambda:self.repo_sequence(repos,folder,proxy,token,comfy,session=session)
        if comfy and direct:self.comfy_direct_sequence(direct,proxy,token,on_done=start_repos,session=session)
        else:start_repos()
        if accepted:
            self.note.set(f'已接收 {accepted} 个链接，'+('请确认文件和保存位置后开始下载。' if comfy or repos else '正在准备下载。'))
        if errors:self.note.set('；'.join(errors[:3]))
        self.persist()

    def enqueue(self, info, folder, proxy, token, target_override=None, staged=False, known_size=0, replace_existing=False):
        target=str(validate_target_path(target_override or output_path(folder,info,flatten=True,repository_folder=False)))
        if any(os.path.normcase(j['path'])==os.path.normcase(target) for j in self.jobs.values()):
            raise ValueError('队列已有这个保存位置的任务：'+info['filename'])
        if Path(target).exists() and (not replace_existing or not Path(target).is_file()):
            raise ValueError('文件已存在，不会覆盖：'+target)
        ident=uuid.uuid4().hex
        job=dict(info,id=ident,path=target,model_type=detect_model_type(info['filename']),connections=int(self.connections.get()),status='staged' if staged else 'preparing',priority=False,gid=None,downloaded=0,size=max(0,int(known_size or 0)),speed=0,elapsed=0,error='',replace_existing=bool(replace_existing),source_url=file_url(info),source_page_url=file_page_url(info))
        self.jobs[ident]=job
        self.tree.insert('','end',iid=ident)
        self.empty_queue.place_forget()
        self.render(job)
        if not staged:self.submit(lambda:resolve_file(info,proxy,token),lambda meta:self.prepared(ident,meta,proxy,token),lambda msg:self.fail(ident,msg))
        self.refresh_summary()
        return ident

    def comfy_settings(self):
        win=tk.Toplevel(self);win.title('ComfyUI \u8bbe\u7f6e');self.center_dialog(win,650,330)
        win.configure(bg='#f5f7f8')
        surface=RoundedSurface(win,padding=(20,16),stretch_y=True);surface.pack(fill='both',expand=True,padx=12,pady=12)
        box=surface.content
        ttk.Label(box,text='ComfyUI \u6839\u76ee\u5f55',style='Section.TLabel').pack(anchor='w')
        row=ttk.Frame(box,style='Card.TFrame');row.pack(fill='x',pady=8)
        ttk.Entry(row,textvariable=self.comfy_root).pack(side='left',fill='x',expand=True)
        ttk.Button(row,text='\u9009\u62e9',command=lambda: self.comfy_root.set(filedialog.askdirectory(parent=win,title='\u9009\u62e9 ComfyUI \u6839\u76ee\u5f55') or self.comfy_root.get())).pack(side='left',padx=(8,0))
        ttk.Label(box,text='\u6269\u6563\u6a21\u578b\u76ee\u5f55',style='Section.TLabel').pack(anchor='w',pady=(8,3))
        ttk.Radiobutton(box,text='models\\diffusion_models',style='Card.TRadiobutton',variable=self.diffusion_dir,value='diffusion_models').pack(anchor='w')
        ttk.Radiobutton(box,text='models\\unet',style='Card.TRadiobutton',variable=self.diffusion_dir,value='unet').pack(anchor='w')
        def save():
            root=Path(self.comfy_root.get()).expanduser()
            if not (root/'models').is_dir(): self.notice_dialog(win,'ComfyUI 设置','请选择包含 models 文件夹的 ComfyUI 根目录。');return
            if not (root/'main.py').exists() and self.decision_dialog(win,'ComfyUI 设置','未发现 main.py，仍保存这个目录？',
                [('yes','仍然保存','Accent.TButton',True),('no','返回检查','Secondary.TButton',True)])!='yes':return
            self.comfy_root.set(str(root));self.persist();win.destroy()
        footer=ttk.Frame(box,style='Card.TFrame');footer.pack(side='bottom',fill='x',pady=(12,0))
        ttk.Button(footer,text='\u4fdd\u5b58',style='Accent.TButton',command=save).pack(side='right')
        win.update_idletasks();win.deiconify();win.lift()

    def comfy_preferences(self, info):
        model_kind=detect_model_type(info['filename'])
        names={
            'UNet / \u6269\u6563\u6a21\u578b':(self.diffusion_dir.get(),'diffusion_models','unet'),
            '\u4e3b\u6a21\u578b':('checkpoints','checkpoint'),
            'LoRA':('loras','lora'),
            'VAE':('vae',),
            'CLIP / \u6587\u672c\u7f16\u7801\u5668':('text_encoders','text_encoder','clip'),
            'ControlNet':('controlnet','controlnets'),
            '\u653e\u5927\u6a21\u578b':('upscale_models','upscalers'),
            '\u5d4c\u5165':('embeddings','embedding'),
        }.get(model_kind,(self.diffusion_dir.get(),'diffusion_models','unet'))
        return tuple(name.casefold() for name in names)

    def comfy_batch_preferences(self, infos):
        if not infos:return ()
        if len(infos)>1:
            kinds={detect_model_type(info['filename']) for info in infos}
            if len(kinds)!=1 or '其他' in kinds:return ()
        return self.comfy_preferences(infos[0])

    def comfy_tree_dialog(self, infos, proxy, token):
        root=Path(self.comfy_root.get()).expanduser(); models=root/'models'
        if not models.is_dir():
            self.notice_dialog(self,'ComfyUI','请先在设置中选择 ComfyUI 根目录。');self.comfy_settings();return
        preferred_names=self.comfy_batch_preferences(infos)
        return self.comfy_column_dialog(root,models,infos,proxy,token,preferred_names)

    def comfy_column_dialog(self, root, models, infos, proxy, token, preferred_names, on_confirm=None, initial_rel=None, button_text='\u786e\u8ba4\u5e76\u4e0b\u8f7d', secondary_text=None, on_secondary=None, on_back=None, on_cancel=None, save_mode=None):
        win=tk.Toplevel(self);win.withdraw();win.title('\u786e\u8ba4 ComfyUI \u4e0b\u8f7d\u4f4d\u7f6e');self.center_dialog(win,1300,800)
        win.configure(bg='#f5f7f8')
        dialog_surface=RoundedSurface(win,padding=(14,12),stretch_y=True)
        dialog_surface.pack(fill='both',expand=True,padx=12,pady=12)
        box=dialog_surface.content
        roots=[p for p in models.iterdir() if p.is_dir()]
        initial_path=models if initial_rel=='.' else models/(initial_rel or '')
        best=initial_rel if initial_rel and initial_path.is_dir() else next((p.name for name in preferred_names for p in roots if p.name.casefold()==name),'.')
        def path_label(rel):return 'models' if rel=='.' else 'models\\'+rel.replace('/','\\')
        current=best
        favorite_row=ttk.Frame(box,style='Card.TFrame');favorite_row.pack(fill='x',pady=(0,8))
        favorite_row.columnconfigure(2,weight=1)
        back_slot=ttk.Frame(favorite_row,style='Card.TFrame',width=88,height=38)
        back_slot.grid(row=0,column=0,sticky='w',padx=(0,10));back_slot.grid_propagate(False)
        if on_back is not None:
            def go_back():
                win.destroy();on_back()
            ttk.Button(back_slot,text='\u2190  \u8fd4\u56de',style='Back.TButton',command=go_back).pack(fill='both',expand=True)
        ttk.Label(favorite_row,text='\u2605  \u6536\u85cf\u76ee\u5f55',style='Section.TLabel').grid(row=0,column=1,sticky='w',padx=(0,10))
        favorite=tk.StringVar();favorite_box=ModernDropdown(favorite_row,favorite)
        favorite_box.grid(row=0,column=2,sticky='ew',padx=(18,8))
        footer=ttk.Frame(box,style='Card.TFrame')
        footer.pack(side='bottom',fill='x',pady=(8,0))
        viewport=tk.Canvas(box,highlightthickness=0,bg='#ffffff')
        viewport.pack(fill='both',expand=True)
        horizontal=ttk.Scrollbar(box,orient='horizontal',command=viewport.xview)
        viewport.configure(xscrollcommand=horizontal.set)
        columns=ttk.Frame(viewport,style='Card.TFrame')
        column_window=viewport.create_window((0,0),window=columns,anchor='nw')
        def sync_horizontal(event=None):
            viewport.update_idletasks()
            visible_width=max(1,viewport.winfo_width())
            required_width=max(1,columns.winfo_reqwidth())
            content_width=max(visible_width,required_width)
            content_height=max(1,viewport.winfo_height(),columns.winfo_reqheight())
            viewport.itemconfigure(column_window,width=content_width,height=content_height)
            viewport.configure(scrollregion=(0,0,content_width,content_height))
            if required_width<=visible_width:
                viewport.xview_moveto(0)
                if horizontal.winfo_manager():horizontal.pack_forget()
            elif not horizontal.winfo_manager():
                horizontal.pack(fill='x',after=viewport)
        viewport.bind('<Configure>',sync_horizontal)
        columns.bind('<Configure>',lambda _:win.after_idle(sync_horizontal))
        def target():return models if current=='.' else models/current
        def entries(rel):
            base=models if rel=='.' else models/rel
            items=[p for p in base.iterdir() if p.is_dir()]
            return sorted(items,key=lambda p:p.name.casefold())
        def display_name(item,rel):
            return item.name
        panes=[];handles=[];pane_parents=[];column_lists={};column_filters={};column_filter_vars={};redrawing=False
        active_parent=['.' if best=='.' else best.rpartition('/')[0] or '.']
        folder_popup=[None]
        def paint_active_layer():
            for pane,parent in zip(panes,pane_parents):
                active=parent==active_parent[0]
                pane.configure(highlightbackground='#4f95c6' if active else '#dce4e8',
                               highlightcolor='#4f95c6' if active else '#dce4e8',
                               highlightthickness=2 if active else 1)
        def reveal_active_layer():
            if not win.winfo_exists() or active_parent[0] not in pane_parents:return
            pane=panes[pane_parents.index(active_parent[0])]
            viewport.update_idletasks()
            visible_width=max(1,viewport.winfo_width())
            content_width=max(visible_width,columns.winfo_reqwidth())
            left=viewport.canvasx(0)
            pane_left=pane.winfo_x()
            pane_right=pane_left+pane.winfo_width()
            if pane_right>left+visible_width:
                viewport.xview_moveto(max(0,(pane_right-visible_width)/content_width))
            elif pane_left<left:
                viewport.xview_moveto(max(0,pane_left/content_width))
        def set_active_layer(parent):
            active_parent[0]=parent
            paint_active_layer()
            win.after_idle(reveal_active_layer)
        resize_drag={}
        def resize_press(event, index):
            widths=[int(pane.cget('width')) for pane in panes]
            if index>=len(widths)-1:return
            resize_drag.update(index=index,start_x=event.x_root,widths=widths)
            return 'break'
        def resize_move(event):
            if not resize_drag:return
            widths=proportional_right_widths(resize_drag['widths'],resize_drag['index'],
                resize_drag['widths'][resize_drag['index']]+event.x_root-resize_drag['start_x'],160)
            for pane,width in zip(panes,widths):pane.configure(width=width)
            win.after_idle(sync_horizontal)
            return 'break'
        def resize_release(_event):
            resize_drag.clear()
            return 'break'
        def draw_chain(rel, preserve_view=False):
            nonlocal current, panes, handles, pane_parents, column_lists, column_filter_vars, redrawing
            old_views={key:widget.yview()[0] for key,widget in column_lists.items()} if preserve_view else {}
            redrawing=True;current=rel
            pending_selections=[];chain=[] if rel=='.' else rel.split('/');parents=['.'];parent='.'
            for part in chain:
                parent=parent+'/'+part if parent!='.' else part;parents.append(parent)
            common=0
            while common<min(len(pane_parents),len(parents)) and pane_parents[common]==parents[common]:common+=1
            for handle in handles[max(0,common-1):]:handle.destroy()
            handles=handles[:max(0,common-1)]
            for pane in panes[common:]:pane.destroy()
            panes=panes[:common];pane_parents=pane_parents[:common]
            column_lists={parent:column_lists[parent] for parent in pane_parents if parent in column_lists}
            column_filter_vars={parent:column_filter_vars[parent] for parent in pane_parents if parent in column_filter_vars}
            for column_index,parent in enumerate(parents):
                if column_index<common:
                    lb=column_lists[parent];lb.selection_clear()
                    query=column_filters.get(parent,'').strip().casefold()
                    shown=[p for p in entries(parent) if query in p.name.casefold()]
                    names=[p.name for p in shown]
                    if lb.items!=names:
                        lb.delete(0,'end')
                        for name in names:lb.insert('end',name)
                    if column_index<len(chain):
                        index=next((i for i,p in enumerate(shown) if p.name==chain[column_index]),None)
                        if index is not None:lb.selection_set(index);pending_selections.append((lb,index,parent))
                    if parent in old_views:lb.yview_moveto(old_views[parent])
                    continue
                if panes:
                    handle=tk.Frame(columns,width=12,bg=UI_COLOR['canvas'],cursor='sb_h_double_arrow')
                    handle.pack(side='left',fill='y');handle.pack_propagate(False)
                    grip=tk.Frame(handle,width=3,bg='#aebfc8',cursor='sb_h_double_arrow')
                    grip.place(relx=.5,rely=.5,anchor='center',relheight=.15)
                    for surface in (handle,grip):
                        surface.bind('<ButtonPress-1>',lambda event,index=len(panes)-1:resize_press(event,index))
                        surface.bind('<B1-Motion>',resize_move)
                        surface.bind('<ButtonRelease-1>',resize_release)
                    handles.append(handle)
                pane=tk.Frame(columns,width=244,bg='#ffffff',highlightbackground='#dce4e8',highlightcolor='#dce4e8',highlightthickness=1,takefocus=0)
                pane.pack(side='left',fill='y');pane.pack_propagate(False);panes.append(pane)
                pane_parents.append(parent)
                filter_text=tk.StringVar(value=column_filters.get(parent,''))
                column_filter_vars[parent]=filter_text
                searchbar=ModernSearch(pane,filter_text)
                searchbar.pack(fill='x',padx=7,pady=(8,8))
                searchbar.entry.bind('<FocusIn>',lambda _event,base=parent:set_active_layer(base),add='+')
                list_frame=ttk.Frame(pane,style='Card.TFrame');list_frame.pack(fill='both',expand=True,padx=(7,0),pady=(0,7))
                lb=FolderList(list_frame)
                scroll=SlimScrollbar(list_frame,command=lb.yview)
                lb.configure(yscrollcommand=scroll.set);lb.pack(side='left',fill='both',expand=True);scroll.pack(side='right',fill='y')
                lb.bind('<ButtonPress-1>',lambda _event,base=parent:set_active_layer(base),add='+')
                column_lists[parent]=lb
                query=filter_text.get().strip().casefold()
                for item in entries(parent):
                    if query in item.name.casefold():lb.insert('end',display_name(item,parent))
                def choose(event, widget=lb, base=parent):
                    if redrawing:return
                    set_active_layer(base)
                    selected=widget.curselection()
                    if selected:
                        query=column_filters.get(base,'').strip().casefold()
                        shown=[p for p in entries(base) if query in p.name.casefold()]
                        if selected[0]>=len(shown):return
                        chosen=str(shown[selected[0]].relative_to(models)).replace('\\','/')
                        draw_chain(chosen,preserve_view=True)
                lb.bind('<ButtonRelease-1>',choose);lb.bind('<Return>',choose)
                def folder_menu(event, widget=lb, base=parent):
                    index=int(widget.canvasy(event.y)//widget.row_height)
                    query=column_filters.get(base,'').strip().casefold()
                    shown=[p for p in entries(base) if query in p.name.casefold()]
                    if not 0<=index<len(shown):return 'break'
                    rel=str(shown[index].relative_to(models)).replace('\\','/')
                    widget.selection_set(index);widget.redraw()
                    set_active_layer(base)
                    saved=rel in self.comfy_favorites
                    def toggle_favorite():
                        if saved:self.comfy_favorites.remove(rel)
                        elif rel not in self.comfy_favorites:self.comfy_favorites.append(rel)
                        self.persist();refresh_favorites()
                        favorite.set('' if saved else path_label(rel))
                    if folder_popup[0] and folder_popup[0].winfo_exists():folder_popup[0].close()
                    folder_popup[0]=EditContextMenu(widget,[('取消收藏此文件夹' if saved else '收藏此文件夹','',toggle_favorite,True)])
                    folder_popup[0].show(event.x_root,event.y_root)
                    return 'break'
                lb.bind('<Button-3>',folder_menu)
                def on_filter(*_, base=parent, widget=lb, query_var=filter_text):
                    column_filters[base]=query_var.get()
                    selected=current.split('/')[len(base.split('/')) if base!='.' else 0] if current!='.' and (base=='.' or current.startswith(base+'/')) else ''
                    shown=[p for p in entries(base) if query_var.get().strip().casefold() in p.name.casefold()]
                    widget.delete(0,'end')
                    for item in shown:widget.insert('end',display_name(item,base))
                    index=next((i for i,p in enumerate(shown) if p.name==selected),None)
                    if index is not None:widget.selection_set(index)
                filter_text.trace_add('write',on_filter)
                if column_index < len(chain):
                    shown=[p for p in entries(parent) if filter_text.get().strip().casefold() in p.name.casefold()]
                    index=next((i for i,p in enumerate(shown) if p.name==chain[column_index]),None)
                    if index is not None:
                        lb.selection_set(index);pending_selections.append((lb,index,parent))
                    if parent in old_views:lb.yview_moveto(old_views[parent])
                elif parent in old_views:lb.yview_moveto(old_views[parent])
            redrawing=False
            if active_parent[0] not in pane_parents:active_parent[0]=parents[-2] if len(parents)>1 else '.'
            paint_active_layer()
            target_suffix.set(str(target()))
            def finish_layout():
                sync_horizontal()
                for widget,index,parent_path in pending_selections:
                    if not widget.winfo_exists():continue
                    widget.selection_set(index)
                    if parent_path in old_views:widget.yview_moveto(old_views[parent_path])
                    else:widget.see(index)
                    widget.redraw()
                reveal_active_layer()
            win.after_idle(finish_layout)
        def refresh_favorites():favorite_box['values']=[path_label(p) for p in self.comfy_favorites if (models/p).is_dir()]
        def pick_favorite(*_):
            picked=next((p for p in self.comfy_favorites if (models/p).is_dir() and path_label(p)==favorite.get()),None)
            if picked is not None:
                active_parent[0]=picked.rpartition('/')[0] or '.'
                for filter_var in column_filter_vars.values():
                    if filter_var.get():filter_var.set('')
                column_filters.clear();draw_chain(picked)
        favorite_box.bind('<<ComboboxSelected>>',pick_favorite)
        def add_favorite():
            if current!='.' and current not in self.comfy_favorites:self.comfy_favorites.append(current);self.persist()
            refresh_favorites();favorite.set(path_label(current))
        ttk.Button(favorite_row,text='\u6536\u85cf\u5f53\u524d\u76ee\u5f55',style='Secondary.TButton',command=add_favorite).grid(row=0,column=3,sticky='e')
        def remove_favorite():
            if current in self.comfy_favorites:self.comfy_favorites.remove(current);self.persist()
            refresh_favorites();favorite.set('')
        ttk.Button(favorite_row,text='\u53d6\u6d88\u6536\u85cf',style='Secondary.TButton',command=remove_favorite).grid(row=0,column=4,sticky='e',padx=(6,0))
        refresh_favorites()
        target_row=tk.Frame(footer,bg='#edf6fa',highlightbackground='#d4e7ef',highlightthickness=1,padx=12,pady=8)
        target_row.pack(fill='x',pady=(0,10))
        target_title='\u76ee\u6807\u4fdd\u5b58\u4f4d\u7f6e' if save_mode is None else f'\u76ee\u6807\u4fdd\u5b58\u4f4d\u7f6e  \u00b7  {len(infos)} \u4e2a\u6587\u4ef6'
        if save_mode is not None:
            save_mode_switch(target_row,save_mode,background='#edf6fa').pack(side='right')
            tk.Label(target_row,text='下载保存方式',bg='#edf6fa',fg='#27343b',
                     font=('Microsoft YaHei UI',10)).pack(side='right',padx=(0,10))
        tk.Label(target_row,text=target_title,bg='#edf6fa',fg='#5b7380',font=('Microsoft YaHei UI',10)).pack(side='left',padx=(0,12))
        target_suffix=tk.StringVar()
        copy_target=ttk.Button(target_row,text='复制路径',style='Queue.TButton',
                               command=lambda:self.copy_text(target_suffix.get(),'已复制目标保存路径。'))
        copy_target.pack(side='right',padx=(10,0))
        target_path=tk.Entry(target_row,textvariable=target_suffix,bg='#edf6fa',fg='#1d485f',
                             readonlybackground='#edf6fa',font=('Microsoft YaHei UI',10,'bold'),
                             relief='flat',bd=0,highlightthickness=0,state='readonly')
        target_path.pack(side='left',fill='x',expand=True)
        Tooltip(target_path,lambda:target_suffix.get())
        target_suffix.trace_add('write',lambda *_:target_path.xview_moveto(0))
        draw_chain(best)
        buttons=ttk.Frame(footer,style='Card.TFrame');buttons.pack(fill='x')
        def new_folder():
            parent_rel=active_parent[0]
            parent_dir=models if parent_rel=='.' else models/parent_rel
            name=self.ask_folder_name(win,f'在 {path_label(parent_rel)} 中新建文件夹：')
            if not name:return
            clean=safe_piece(name)
            if name in ('.','..') or '/' in name or '\\' in name or clean!=name:
                self.notice_dialog(win,'无法新建','文件夹名不合法，不能包含 Windows 禁用字符、保留名、末尾空格或句点。')
                return
            try:
                child=validate_target_path(parent_dir/name)
                if child.exists():
                    self.notice_dialog(win,'无法新建','同名文件夹已存在。');return
                child.mkdir()
            except (OSError,ValueError) as exc:
                self.notice_dialog(win,'无法新建',describe_error(exc));return
            rel=str(child.relative_to(models)).replace('\\','/')
            filter_var=column_filter_vars.get(parent_rel)
            if filter_var is not None and filter_var.get():filter_var.set('')
            draw_chain(rel,preserve_view=True)
        def add():
            if on_confirm is not None:
                chosen=str(target().relative_to(models)).replace('\\','/') or '.'
                if on_confirm(chosen,win) is False:return
                win.destroy();self.persist();return
            skipped=[]
            for info in infos:
                out=target()/safe_piece(info['filename'].split('/')[-1])
                if out.exists():skipped.append(out.name)
                else:self.enqueue(info,str(root),proxy,token,out)
            win.destroy();self.persist();self.note.set('\u5df2\u6dfb\u52a0 ComfyUI \u4efb\u52a1\u3002' if not skipped else '\u5df2\u6dfb\u52a0\uff0c\u5df2\u8df3\u8fc7\u540c\u540d\u6587\u4ef6\u3002')
        ttk.Button(buttons,text='新建文件夹',style='Secondary.TButton',command=new_folder).pack(side='left')
        ttk.Button(buttons,text=button_text,style='Accent.TButton',command=add).pack(side='right')
        if secondary_text and on_secondary is not None:
            def secondary():
                chosen=str(target().relative_to(models)).replace('\\','/') or '.'
                if on_secondary(chosen,win) is False:return
                win.destroy();self.persist()
            ttk.Button(buttons,text=secondary_text,style='Secondary.TButton',command=secondary).pack(side='right',padx=(0,8))
        if on_cancel is not None:
            def cancel():
                win.destroy();on_cancel()
            win.protocol('WM_DELETE_WINDOW',cancel)
        elif on_back is not None:
            win.protocol('WM_DELETE_WINDOW',go_back)
        win.update_idletasks();win.deiconify();win.lift()

    def comfy_dialog(self, infos, proxy, token):
        return self.comfy_tree_dialog(infos,proxy,token)

    def check_job_capacity(self, ident, total=None):
        job=self.jobs[ident]
        volume=Path(job['path']).anchor.casefold()
        committed=sum(max(0,int(other.get('size',0))-int(other.get('downloaded',0)))
                      for key,other in self.jobs.items() if key!=ident and Path(other['path']).anchor.casefold()==volume
                      and other.get('status') not in ('complete','error','removing'))
        ensure_download_capacity(job['path'],job.get('size',0) if total is None else total,committed)

    def prepared(self, ident, meta, proxy, token):
        if ident not in self.jobs:return
        job=self.jobs[ident]
        if job.get('removing'):
            self.finish_remove(ident, job.get('delete_files', False));return
        try:
            self.check_job_capacity(ident,meta.get('size',0))
        except Exception as exc:
            self.fail(ident,describe_error(exc));return
        job.update(meta)
        self.start_job(ident,proxy,token)

    def detach_engine_job(self, gid):
        try:self.engine.call('forceRemove',gid)
        except Exception as stop_error:
            try:status=self.engine.call('tellStatus',gid).get('status')
            except Exception:
                raise RuntimeError('无法确认旧下载任务已停止，为避免同一文件被继续写入，操作已取消。') from stop_error
            if status not in ('complete','error','removed'):
                raise RuntimeError('旧下载任务仍在运行，为避免文件冲突，操作已取消。') from stop_error
        try:self.engine.call('removeDownloadResult',gid)
        except Exception:pass

    def start_job(self, ident, proxy, token):
        job=self.jobs[ident]
        if job.get('removing'):return
        if job.get('starting'):return
        job['starting']=True
        job['status']='preparing';self.render(job)
        snapshot=dict(job)
        def start():
            if snapshot.get('gid'):self.detach_engine_job(snapshot['gid'])
            return self.engine.add(snapshot,proxy,token)
        def ready(gid):
            if ident not in self.jobs:
                self.submit(lambda:self.engine.call('forceRemove',gid),lambda _:None);return
            if self.jobs[ident].get('removing'):
                self.jobs[ident].update(gid=gid,starting=False)
                self.stop_and_finish_remove(ident);return
            job.update(gid=gid,status='waiting',starting=False,error='')
            self.note.set('已加入下载队列，正在等待开始：'+job['filename'])
            self.persist();self.render(job)
            if job.get('priority'):self.prioritize_gid(ident)
        self.submit(start,ready,lambda msg:self.fail(ident,msg))

    def fail(self, ident, msg):
        if ident in self.jobs and not self.jobs[ident].get('removing'):
            self.jobs[ident].update(status='error',error=msg,speed=0,starting=False)
            self.render(self.jobs[ident]);self.persist();self.selection_changed()
        self.note.set(msg)

    def repo_dialog(self, info, files, folder, proxy, token, comfy=False, committed_paths=None, initial_directory='', initial_selected_paths=None, initial_view='tree', initial_sort='文件名 A→Z', on_done=None, initial_save_mode='structure', session=None):
        if session is None:session={'ids':[],'cancelled':False,'proxy':proxy,'token':token}
        win=tk.Toplevel(self);win.withdraw()
        win.title('选择仓库文件 · '+info['repo'])
        self.center_dialog(win,1120,720)
        win.minsize(min(980,win.winfo_width()),min(620,win.winfo_height()))
        win.configure(bg='#f5f7f8')
        finished=False
        def finish_dialog(confirmed=False):
            nonlocal finished
            if finished:return
            finished=True
            if not confirmed:session['cancelled']=True
            if win.winfo_exists():win.destroy()
            if on_done:self.after_idle(on_done)
            else:self.after_idle(lambda:self.finish_selection_session(session))
        win.protocol('WM_DELETE_WINDOW',finish_dialog)
        win.bind('<Escape>',lambda _:finish_dialog())
        surface=RoundedSurface(win,padding=(24,20),stretch_y=True)
        surface.pack(fill='both',expand=True,padx=12,pady=12)
        pane=surface.content

        base=info.get('folder','').strip('/')
        searchbar=ttk.Frame(pane,style='Card.TFrame');searchbar.pack(fill='x',pady=(0,10))
        searchbar.columnconfigure(1,weight=1)
        ttk.Label(searchbar,text='搜索文件',style='Card.TLabel').grid(row=0,column=0,sticky='w',padx=(0,10))
        search=tk.StringVar()
        search_entry=ttk.Entry(searchbar,textvariable=search)
        search_entry.grid(row=0,column=1,sticky='ew')
        ttk.Button(searchbar,text='清除',command=lambda:search.set('')).grid(row=0,column=2,sticky='w',padx=(8,0))
        view_mode=tk.StringVar(value=initial_view if initial_view in ('tree','all') else 'tree')
        view_controls=ttk.Frame(searchbar,style='Card.TFrame')
        view_controls.grid(row=0,column=3,sticky='e',padx=(16,0))
        view_top=ttk.Frame(view_controls,style='Card.TFrame')
        view_top.pack(anchor='w')
        view_switch=tk.Canvas(view_top,width=174,height=36,bg='#ffffff',highlightthickness=0,
                              bd=0,cursor='hand2',takefocus=1)
        view_switch.pack(side='left')
        sort_mode=tk.StringVar(value=initial_sort)
        sort_slot=ttk.Frame(view_top,style='Card.TFrame',width=170,height=36)
        sort_slot.pack(side='left',padx=(8,0))
        sort_slot.pack_propagate(False)
        sort_box=ModernDropdown(sort_slot,sort_mode,'排序方式')
        sort_box['values']=('文件名 A→Z','文件名 Z→A','路径 A→Z','路径 Z→A',
                            '文件大小 小→大','文件大小 大→小','下载 已勾选优先','下载 未勾选优先')
        sort_box.configure(width=170)
        structure_note=ttk.Label(view_controls,text='仅用于筛选查看；下载保存方式在右下角选择。' if not comfy else
                                 '仅用于筛选查看；ComfyUI 保存位置在下一步确认。',style='Muted.TLabel')
        structure_note.pack(anchor='w',pady=(3,0))
        def arrange_searchbar(event):
            compact=event.width<930
            if compact==getattr(win,'_searchbar_compact',None):return
            win._searchbar_compact=compact
            if compact:view_controls.grid_configure(row=1,column=0,columnspan=4,sticky='w',padx=0,pady=(8,0))
            else:view_controls.grid_configure(row=0,column=3,columnspan=1,sticky='e',padx=(16,0),pady=0)
        searchbar.bind('<Configure>',arrange_searchbar,add='+')
        relative_paths=[]
        file_folders=[]
        children_dirs={'':set()}
        direct_files={}
        for index,item in enumerate(files):
            path=item['path'].strip('/')
            relative=path[len(base)+1:] if base and path.startswith(base+'/') else path
            relative_paths.append(relative)
            parent=str(PurePosixPath(relative).parent)
            parent='' if parent=='.' else parent
            file_folders.append(parent)
            direct_files.setdefault(parent,[]).append(index)
            ancestor=''
            for part in parent.split('/') if parent else []:
                current=part if not ancestor else ancestor+'/'+part
                children_dirs.setdefault(ancestor,set()).add(current)
                children_dirs.setdefault(current,set())
                ancestor=current

        folder_icon=tk.PhotoImage(master=win,width=16,height=14)
        folder_icon.put('#5aa9e6',to=(1,3,15,13))
        folder_icon.put('#83c5f1',to=(1,2,8,5))
        folder_icon.put('#b9dcf5',to=(2,5,14,12))
        file_icon=tk.PhotoImage(master=win,width=14,height=16)
        file_icon.put('#d9e0e7',to=(2,1,11,15))
        file_icon.put('#f8fafc',to=(3,2,10,14))
        file_icon.put('#9eb0bf',to=(4,6,9,7))
        file_icon.put('#9eb0bf',to=(4,9,9,10))
        win._repo_icons=(folder_icon,file_icon)

        browser_shell=ttk.Frame(pane,style='Card.TFrame')
        browser_shell.pack(fill='both',expand=True)
        columns_frame=ttk.Panedwindow(browser_shell,orient='horizontal')
        columns_frame.place(relx=0,rely=0,relwidth=1,relheight=1)

        panel_trees=[]
        for column in range(3):
            panel=ttk.Frame(columns_frame,style='Card.TFrame',width=330)
            columns_frame.add(panel,weight=1)
            tree_box=ttk.Frame(panel,style='Card.TFrame')
            tree_box.pack(fill='both',expand=True,padx=(8,6))
            tree=ttk.Treeview(tree_box,columns=('mark',),show='tree',selectmode='extended',style='Column.Treeview')
            tree.column('#0',width=235 if column<2 else 360,minwidth=180,stretch=True,anchor='w')
            tree.column('mark',width=58,minwidth=58,stretch=False,anchor='center')
            tree.tag_configure('unavailable',foreground='#a7b0b6')
            scroll=SlimScrollbar(tree_box,command=tree.yview)
            tree.configure(yscrollcommand=scroll.set)
            tree.pack(side='left',fill='both',expand=True,pady=(8,4))
            scroll.pack(side='right',fill='y')
            panel_trees.append(tree)
        bind_proportional_sashes(columns_frame,minimum=180)

        search_frame=ttk.Frame(browser_shell,style='Card.TFrame')
        search_tree=ttk.Treeview(search_frame,columns=('location','mark'),show='tree',selectmode='extended',style='Column.Treeview')
        search_tree.column('#0',width=500,minwidth=260,stretch=True,anchor='w')
        search_tree.column('location',width=360,minwidth=180,stretch=True,anchor='w')
        search_tree.column('mark',width=42,minwidth=42,stretch=False,anchor='center')
        search_tree.tag_configure('unavailable',foreground='#a7b0b6')
        search_scroll=SlimScrollbar(search_frame,command=search_tree.yview)
        search_tree.configure(yscrollcommand=search_scroll.set)
        search_tree.pack(side='left',fill='both',expand=True,pady=(8,4))
        search_scroll.pack(side='right',fill='y')

        all_frame=ttk.Frame(browser_shell,style='Card.TFrame')
        all_tree=ttk.Treeview(all_frame,columns=('path','name','size','mark'),show='headings',selectmode='extended',style='Column.Treeview')
        all_tree.heading('path',text='仓库内路径')
        all_tree.heading('name',text='文件名')
        all_tree.heading('size',text='文件大小')
        all_tree.heading('mark',text='下载')
        all_tree.column('path',width=330,minwidth=140,stretch=True,anchor='w')
        all_tree.column('name',width=390,minwidth=180,stretch=True,anchor='w')
        all_tree.column('size',width=135,minwidth=100,stretch=False,anchor='e')
        all_tree.column('mark',width=80,minwidth=70,stretch=False,anchor='center')
        all_tree.tag_configure('unavailable',foreground='#a7b0b6')
        all_scroll=SlimScrollbar(all_frame,command=all_tree.yview)
        all_tree.configure(yscrollcommand=all_scroll.set)
        all_tree.pack(side='left',fill='both',expand=True,pady=(8,4))
        all_scroll.pack(side='right',fill='y')

        bold_font=tkfont.Font(root=win,family='Microsoft YaHei UI',size=10,weight='bold')
        win._repo_bold_font=bold_font
        for tree in (*panel_trees,search_tree,all_tree):tree.tag_configure('chosen',font=bold_font)

        committed_paths=set(committed_paths or ())
        committed_indices={index for index,item in enumerate(files) if item['path'] in committed_paths}
        available_indices=set(range(len(files)))-committed_indices
        selected_indices=(set(available_indices) if initial_selected_paths is None else
                          {index for index,item in enumerate(files) if index in available_indices and item['path'] in initial_selected_paths})
        initial_directory=initial_directory if initial_directory in children_dirs else ''
        while initial_directory:
            prefix=initial_directory+'/'
            if any(index in available_indices and (parent==initial_directory or parent.startswith(prefix))
                   for index,parent in enumerate(file_folders)):break
            initial_directory=initial_directory.rpartition('/')[0]
        ancestors=[''];ancestor=''
        for part in initial_directory.split('/') if initial_directory else []:
            ancestor=part if not ancestor else ancestor+'/'+part;ancestors.append(ancestor)
        panel_paths=ancestors[-3:]
        visible_indices=[]
        current_directory=initial_directory
        count_before=tk.StringVar()
        count_selected=tk.StringVar()
        count_after=tk.StringVar()
        selection_anchors={}
        drag_state={}
        folder_clicks={}
        active_selection_tree=[None]

        def subtree_files(directory):
            prefix=directory+'/' if directory else ''
            return [index for index,parent in enumerate(file_folders)
                    if not directory or parent==directory or parent.startswith(prefix)]
        def folder_available(directory):
            return any(index in available_indices for index in subtree_files(directory))
        def file_tags(index):
            return ('unavailable',) if index in committed_indices else (('chosen',) if index in selected_indices else ())
        def folder_tags(directory):
            if not folder_available(directory):return ('unavailable',)
            return ('chosen',) if any(index in selected_indices for index in subtree_files(directory)) else ()
        visible_indices=subtree_files(current_directory)

        def update_count():
            total=sum(int(files[index].get('size') or 0) for index in selected_indices)
            count_before.set(f'共 {len(files)} 个文件  ·  已暂存 {len(committed_indices)} 个  ·  '
                             f'剩余 {len(available_indices)} 个  ·  本次选中 ')
            count_selected.set(str(len(selected_indices)))
            count_after.set(f' 个  ·  本次大小 {human_size(total)}')
            if selected_indices:add_button.state(['!disabled'])
            else:add_button.state(['disabled'])
            if continue_button:
                if selected_indices and len(selected_indices)<len(available_indices):
                    if not continue_button.winfo_manager():continue_button.pack(side='right',padx=(0,8))
                else:continue_button.pack_forget()

        def refresh_marks():
            for tree in panel_trees:
                for item_id in tree.get_children():
                    if item_id.startswith('f:'):
                        index=int(item_id[2:])
                        tree.item(item_id,values=('已添加' if index in committed_indices else ('✓' if index in selected_indices else ''),),tags=file_tags(index))
                    elif item_id.startswith('d:'):
                        directory=item_id[2:];contained=subtree_files(directory)
                        chosen=sum(index in selected_indices for index in contained)
                        tree.item(item_id,values=(f'{chosen}/{len(contained)}  ›',),
                                  tags=folder_tags(directory))
            for item_id in search_tree.get_children():
                if item_id.startswith('f:'):
                    index=int(item_id[2:])
                    search_tree.item(item_id,values=(file_folders[index] or '根目录','已添加' if index in committed_indices else ('✓' if index in selected_indices else '')),tags=file_tags(index))
            if view_mode.get()=='all' and sort_mode.get().startswith('下载 '):
                render_all_files()
            else:
                for item_id in all_tree.get_children():
                    index=int(item_id[2:])
                    all_tree.item(item_id,values=(file_folders[index] or '根目录',PurePosixPath(relative_paths[index]).name,
                                                  human_size(int(files[index].get('size') or 0)),
                                                  '已添加' if index in committed_indices else ('✓' if index in selected_indices else '')),
                                  tags=file_tags(index))
                update_count()

        def render_panel(column,directory,next_directory=None):
            tree=panel_trees[column]
            tree.delete(*tree.get_children())
            for child in sorted(children_dirs.get(directory,set()),key=lambda value:value.rsplit('/',1)[-1].casefold()):
                name=child.rsplit('/',1)[-1]
                contained=subtree_files(child);chosen=sum(index in selected_indices for index in contained)
                tree.insert('','end',iid='d:'+child,text=name,image=folder_icon,
                            values=(f'{chosen}/{len(contained)}  ›',),
                            tags=folder_tags(child))
            for index in sorted(direct_files.get(directory,[]),key=lambda value:PurePosixPath(relative_paths[value]).name.casefold()):
                name=PurePosixPath(relative_paths[index]).name
                unavailable=index in committed_indices
                tree.insert('','end',iid='f:'+str(index),text=name,image=file_icon,values=('已添加' if unavailable else ('✓' if index in selected_indices else ''),),tags=file_tags(index))
            if next_directory:
                item_id='d:'+next_directory
                if tree.exists(item_id) and folder_available(next_directory):
                    tree.selection_set(item_id);tree.focus(item_id);tree.see(item_id)

        def render_columns():
            for column in range(3):
                if column<len(panel_paths):
                    next_directory=panel_paths[column+1] if column+1<len(panel_paths) else None
                    render_panel(column,panel_paths[column],next_directory)
                else:
                    panel_trees[column].delete(*panel_trees[column].get_children())
            update_count()

        def navigate(column,directory):
            nonlocal panel_paths,current_directory,visible_indices
            current_directory=directory
            if column<2:
                panel_paths=panel_paths[:column+1]+[directory]
            else:
                panel_paths=(panel_paths+[directory])[-3:]
            visible_indices=subtree_files(directory)
            render_columns()
            panel_trees[column if column<2 else 1].focus_set()

        def set_file_selection(tree,item_id,event):
            index=int(item_id[2:]);ctrl=bool(event.state&0x0004);shift=bool(event.state&0x0001)
            if index in committed_indices:
                tree.selection_remove(item_id);return 'break'
            children=list(tree.get_children())
            if shift and tree in selection_anchors and selection_anchors[tree] in children:
                start=children.index(selection_anchors[tree]);end=children.index(item_id)
                ids=[item for item in children[min(start,end):max(start,end)+1]
                     if (item.startswith('d:') and folder_available(item[2:])) or
                        (item.startswith('f:') and int(item[2:]) in available_indices)]
                tree.selection_set(ids)
            elif ctrl:
                if item_id in tree.selection():tree.selection_remove(item_id)
                else:tree.selection_add(item_id)
                selection_anchors[tree]=item_id
            else:
                tree.selection_set(item_id);selection_anchors[tree]=item_id
                if index in selected_indices:selected_indices.remove(index)
                else:selected_indices.add(index)
            tree.focus(item_id);refresh_marks();return 'break'

        def toggle_highlighted(tree,item_id):
            ids=tree.selection() or (item_id,)
            indices=set()
            for item in ids:
                if item.startswith('d:'):indices.update(subtree_files(item[2:]))
                elif item.startswith('f:'):indices.add(int(item[2:]))
            indices.intersection_update(available_indices)
            if not indices:return
            if all(index in selected_indices for index in indices):selected_indices.difference_update(indices)
            else:selected_indices.update(indices)
            refresh_marks()

        def toggle_folder(directory):
            indices=[index for index in subtree_files(directory) if index in available_indices]
            if not indices:return
            if all(index in selected_indices for index in indices):selected_indices.difference_update(indices)
            else:selected_indices.update(indices)
            refresh_marks()

        def begin_drag(event,tree):
            if tree is all_tree and tree.identify_region(event.x,event.y) in ('heading','separator'):
                return 'break'
            active_selection_tree[0]=tree
            clicked=tree.identify_row(event.y)
            if clicked.startswith('d:') and not folder_available(clicked[2:]):return 'break'
            if not event.state&0x0005:
                for panel_tree in panel_trees:
                    if panel_tree is not tree and panel_tree.selection():panel_tree.selection_remove(*panel_tree.selection())
                for other_tree in (search_tree,all_tree):
                    if other_tree is not tree and other_tree.selection():other_tree.selection_remove(*other_tree.selection())
            tree.focus_set()
            drag_state[tree]={'dragged':False,'start_y':event.y,'base':set(tree.selection()) if event.state&0x0004 else set()}
            return 'break'

        def drag_select(event,tree):
            state=drag_state.get(tree)
            if not state:return 'break'
            if abs(event.y-state['start_y'])<4:return 'break'
            state['dragged']=True;low=min(state['start_y'],event.y);high=max(state['start_y'],event.y)
            swept=[]
            for item_id in tree.get_children():
                if item_id.startswith('f:') and int(item_id[2:]) not in available_indices:continue
                if item_id.startswith('d:') and not folder_available(item_id[2:]):continue
                box=tree.bbox(item_id)
                if box and box[1]+box[3]>=low and box[1]<=high:swept.append(item_id)
            chosen=state['base'].union(swept)
            tree.selection_set(tuple(chosen))
            if swept:tree.focus(swept[-1]);selection_anchors[tree]=swept[0]
            return 'break'

        def panel_click(event,column):
            tree=panel_trees[column]
            state=drag_state.pop(tree,None)
            if state and state['dragged']:
                folder_clicks.pop(tree,None)
                return 'break'
            item_id=tree.identify_row(event.y)
            if not item_id:
                if tree.selection():tree.selection_remove(*tree.selection())
                return 'break'
            if item_id.startswith('d:'):
                if not folder_available(item_id[2:]):return 'break'
                if event.state&0x0001:
                    folder_clicks.pop(tree,None)
                    children=list(tree.get_children())
                    anchor=selection_anchors.get(tree,tree.focus())
                    if anchor in children:
                        start=children.index(anchor);end=children.index(item_id)
                        tree.selection_set([item for item in children[min(start,end):max(start,end)+1]
                                            if (item.startswith('d:') and folder_available(item[2:])) or
                                               (item.startswith('f:') and int(item[2:]) in available_indices)])
                    else:tree.selection_set(item_id)
                    tree.focus(item_id)
                    return 'break'
                if event.state&0x0004:
                    folder_clicks.pop(tree,None)
                    if item_id in tree.selection():tree.selection_remove(item_id)
                    else:tree.selection_add(item_id)
                    selection_anchors[tree]=item_id;tree.focus(item_id)
                    return 'break'
                now=time.monotonic();previous=folder_clicks.get(tree)
                if previous and previous[0]==item_id and now-previous[1]<=0.5:
                    folder_clicks.pop(tree,None);toggle_folder(item_id[2:])
                else:
                    folder_clicks[tree]=(item_id,now);selection_anchors[tree]=item_id;navigate(column,item_id[2:])
                return 'break'
            elif item_id.startswith('f:'):
                return set_file_selection(tree,item_id,event)

        for column,tree in enumerate(panel_trees):
            tree.bind('<ButtonPress-1>',lambda event,tree=tree:begin_drag(event,tree),add='+')
            tree.bind('<B1-Motion>',lambda event,tree=tree:drag_select(event,tree))
            tree.bind('<ButtonRelease-1>',lambda event,column=column:panel_click(event,column))
            def activate_panel(event,column=column,tree=tree):
                item_id=tree.focus()
                if item_id.startswith('d:'):
                    if not folder_available(item_id[2:]):return 'break'
                    if event.keysym=='space':toggle_highlighted(tree,item_id)
                    else:navigate(column,item_id[2:])
                elif item_id.startswith('f:'):toggle_highlighted(tree,item_id)
                return 'break'
            tree.bind('<Return>',activate_panel)
            tree.bind('<space>',activate_panel)
            def keep_available_selection(event,tree=tree):
                blocked=[item for item in tree.selection() if
                         (item.startswith('d:') and not folder_available(item[2:])) or
                         (item.startswith('f:') and int(item[2:]) not in available_indices)]
                if blocked:tree.selection_remove(*blocked)
            tree.bind('<<TreeviewSelect>>',keep_available_selection)

        def clear_folder_highlight(event):
            if event.widget in (*panel_trees,search_tree,all_tree,select_marked_button,clear_marked_button):return
            for tree in (*panel_trees,search_tree,all_tree):
                if tree.selection():tree.selection_remove(*tree.selection())
        win.bind('<ButtonPress-1>',clear_folder_highlight,add='+')

        def render_all_files():
            nonlocal visible_indices
            query=search.get().casefold().strip()
            ordered=[index for index,path in enumerate(relative_paths) if query in path.casefold()]
            mode=sort_mode.get()
            if mode.startswith('文件大小'):
                ordered.sort(key=lambda index:(int(files[index].get('size') or 0),PurePosixPath(relative_paths[index]).name.casefold(),relative_paths[index].casefold()),reverse=mode.endswith('大→小'))
            elif mode.startswith('下载 '):
                checked_first=mode=='下载 已勾选优先'
                ordered.sort(key=lambda index:(2 if index in committed_indices else
                             (0 if (index in selected_indices)==checked_first else 1),
                             PurePosixPath(relative_paths[index]).name.casefold(),relative_paths[index].casefold()))
            elif mode.startswith('路径 '):
                ordered.sort(key=lambda index:(file_folders[index].casefold(),PurePosixPath(relative_paths[index]).name.casefold()),
                             reverse=mode.endswith('Z→A'))
            else:
                ordered.sort(key=lambda index:(PurePosixPath(relative_paths[index]).name.casefold(),relative_paths[index].casefold()),reverse=mode.endswith('Z→A'))
            wanted={'f:'+str(index) for index in ordered}
            stale=[item_id for item_id in all_tree.get_children() if item_id not in wanted]
            if stale:all_tree.delete(*stale)
            for position,index in enumerate(ordered):
                unavailable=index in committed_indices
                item_id='f:'+str(index)
                values=(file_folders[index] or '根目录',PurePosixPath(relative_paths[index]).name,
                        human_size(int(files[index].get('size') or 0)),
                        '已添加' if unavailable else ('✓' if index in selected_indices else ''))
                if all_tree.exists(item_id):
                    all_tree.move(item_id,'',position)
                    all_tree.item(item_id,values=values,tags=file_tags(index))
                else:all_tree.insert('',position,iid=item_id,values=values,tags=file_tags(index))
            visible_indices=ordered
            update_count()

        def refresh_search(*_):
            nonlocal visible_indices
            if view_mode.get()=='all':
                if not sort_box.winfo_manager():sort_box.pack(fill='both',expand=True)
                render_all_files()
                columns_frame.place_forget();search_frame.place_forget()
                all_frame.place(relx=0,rely=0,relwidth=1,relheight=1)
                return
            all_frame.place_forget();sort_box.pack_forget()
            query=search.get().casefold().strip()
            if not query:
                visible_indices=subtree_files(current_directory)
                if not columns_frame.winfo_manager():render_columns()
                else:update_count()
                search_frame.place_forget()
                columns_frame.place(relx=0,rely=0,relwidth=1,relheight=1)
                return
            visible_indices=[index for index,path in enumerate(relative_paths) if query in path.casefold()]
            search_tree.delete(*search_tree.get_children())
            for index in visible_indices:
                unavailable=index in committed_indices
                search_tree.insert('','end',iid='f:'+str(index),text=PurePosixPath(relative_paths[index]).name,
                                   image=file_icon,values=(file_folders[index] or '根目录','已添加' if unavailable else ('✓' if index in selected_indices else '')),
                                   tags=file_tags(index))
            update_count()
            columns_frame.place_forget()
            search_frame.place(relx=0,rely=0,relwidth=1,relheight=1)

        def search_click(event):
            state=drag_state.pop(search_tree,None)
            if state and state['dragged']:return 'break'
            item_id=search_tree.identify_row(event.y)
            if not item_id:
                if search_tree.selection():search_tree.selection_remove(*search_tree.selection())
                return 'break'
            if item_id.startswith('f:'):return set_file_selection(search_tree,item_id,event)

        search_tree.bind('<ButtonPress-1>',lambda event:begin_drag(event,search_tree),add='+')
        search_tree.bind('<B1-Motion>',lambda event:drag_select(event,search_tree))
        search_tree.bind('<ButtonRelease-1>',search_click)
        def activate_search(event):
            item_id=search_tree.focus()
            if item_id.startswith('f:'):toggle_highlighted(search_tree,item_id)
            return 'break'
        search_tree.bind('<Return>',activate_search)
        search_tree.bind('<space>',activate_search)
        def keep_available_files(event):
            tree=event.widget
            blocked=[item for item in tree.selection() if item.startswith('f:') and int(item[2:]) not in available_indices]
            if blocked:tree.selection_remove(*blocked)
        search_tree.bind('<<TreeviewSelect>>',keep_available_files)
        all_tree.bind('<ButtonPress-1>',lambda event:begin_drag(event,all_tree),add='+')
        all_tree.bind('<B1-Motion>',lambda event:drag_select(event,all_tree))
        def all_click(event):
            region=all_tree.identify_region(event.x,event.y)
            if region=='heading':
                column=all_tree.identify_column(event.x)
                kinds={'#1':'path','#2':'name','#3':'size','#4':'mark'}
                if column in kinds:toggle_sort(kinds[column])
                return 'break'
            if region=='separator':return 'break'
            state=drag_state.pop(all_tree,None)
            if state and state['dragged']:return 'break'
            item_id=all_tree.identify_row(event.y)
            if not item_id:
                if all_tree.selection():all_tree.selection_remove(*all_tree.selection())
                return 'break'
            return set_file_selection(all_tree,item_id,event)
        all_tree.bind('<ButtonRelease-1>',all_click)
        def activate_all(event):
            item_id=all_tree.focus()
            if item_id.startswith('f:'):toggle_highlighted(all_tree,item_id)
            return 'break'
        all_tree.bind('<Return>',activate_all)
        all_tree.bind('<space>',activate_all)
        all_tree.bind('<<TreeviewSelect>>',keep_available_files)
        def toggle_sort(kind):
            current=sort_mode.get()
            options={'name':('文件名 A→Z','文件名 Z→A'),
                     'path':('路径 A→Z','路径 Z→A'),
                     'size':('文件大小 小→大','文件大小 大→小'),
                     'mark':('下载 已勾选优先','下载 未勾选优先')}
            first,second=options[kind]
            sort_mode.set(second if current==first else first)
        all_tree.heading('name',command=lambda:toggle_sort('name'))
        all_tree.heading('path',command=lambda:toggle_sort('path'))
        all_tree.heading('size',command=lambda:toggle_sort('size'))
        all_tree.heading('mark',command=lambda:toggle_sort('mark'))
        def change_view(mode):
            view_mode.set(mode)
            refresh_search()
        switch_position=[0.0 if view_mode.get()=='tree' else 1.0]
        def draw_view_switch(position):
            switch_position[0]=position
            view_switch.delete('all')
            view_switch.create_oval(1,1,35,35,fill='#edf3f7',outline='#d3e0e7')
            view_switch.create_oval(139,1,173,35,fill='#edf3f7',outline='#d3e0e7')
            view_switch.create_rectangle(18,1,156,35,fill='#edf3f7',outline='#edf3f7')
            view_switch.create_line(18,1,156,1,fill='#d3e0e7')
            view_switch.create_line(18,35,156,35,fill='#d3e0e7')
            x=3+position*85
            view_switch.create_oval(x,3,x+32,33,fill='#cfe8fa',outline='#8dc5ed')
            view_switch.create_oval(x+53,3,x+85,33,fill='#cfe8fa',outline='#8dc5ed')
            view_switch.create_rectangle(x+16,3,x+69,33,fill='#cfe8fa',outline='#cfe8fa')
            view_switch.create_line(x+16,3,x+69,3,fill='#8dc5ed')
            view_switch.create_line(x+16,33,x+69,33,fill='#8dc5ed')
            view_switch.create_text(45,18,text='文件树',fill='#175a82' if position<0.5 else '#557083',
                                    font=('Microsoft YaHei UI',9,'bold' if position<0.5 else 'normal'))
            view_switch.create_text(129,18,text='所有文件',fill='#175a82' if position>=0.5 else '#557083',
                                     font=('Microsoft YaHei UI',9,'bold' if position>=0.5 else 'normal'))
            if view_switch.focus_get() is view_switch:
                view_switch.create_rectangle(1,1,172,34,outline=UI_COLOR['focus'],width=2)
        switch_animation_jobs=[]
        def slide_view(mode):
            if mode==view_mode.get():return
            for job in switch_animation_jobs:
                view_switch.after_cancel(job)
            switch_animation_jobs.clear()
            start=switch_position[0]
            change_view(mode)
            end=0.0 if mode=='tree' else 1.0
            for step in range(1,7):
                switch_animation_jobs.append(view_switch.after(
                    step*18,lambda step=step:draw_view_switch(start+(end-start)*step/6)))
        view_switch.bind('<Button-1>',lambda event:slide_view('tree' if event.x<87 else 'all'))
        view_switch.bind('<Left>',lambda event:slide_view('tree'))
        view_switch.bind('<Right>',lambda event:slide_view('all'))
        view_switch.bind('<space>',lambda event:slide_view('all' if view_mode.get()=='tree' else 'tree'))
        view_switch.bind('<FocusIn>',lambda event:draw_view_switch(switch_position[0]))
        view_switch.bind('<FocusOut>',lambda event:draw_view_switch(switch_position[0]))
        sort_mode.trace_add('write',lambda *_:render_all_files() if view_mode.get()=='all' else None)
        search.trace_add('write',refresh_search)

        def select_all():
            selected_indices.clear();selected_indices.update(available_indices);refresh_marks()
        def invert_selection():
            selected_indices.symmetric_difference_update(available_indices);refresh_marks()
        def clear_selection():
            selected_indices.clear()
            for tree in (*panel_trees,search_tree,all_tree):
                if tree.selection():tree.selection_remove(*tree.selection())
            refresh_marks()
        def highlighted_indices():
            result=set()
            active_trees=[all_tree] if view_mode.get()=='all' else ([search_tree] if search.get().strip() else panel_trees)
            for tree in active_trees:
                for item in tree.selection():
                    if item.startswith('f:'):result.add(int(item[2:]))
                    elif item.startswith('d:'):result.update(subtree_files(item[2:]))
            return result & available_indices
        def select_highlighted():
            selected_indices.update(highlighted_indices());refresh_marks()
            tree=active_selection_tree[0]
            if tree and tree.selection():tree.focus_set()
        def clear_highlighted():
            selected_indices.difference_update(highlighted_indices());refresh_marks()
            tree=active_selection_tree[0]
            if tree and tree.selection():tree.focus_set()
        save_mode=tk.StringVar(value=initial_save_mode if initial_save_mode in ('structure','flat') else 'structure')
        def reopen_with_committed(paths):
            nonlocal finished
            finished=True
            if win.winfo_exists():win.destroy()
            self.after_idle(lambda:self.repo_dialog(info,files,folder,proxy,token,comfy,committed_paths|set(paths),
                                                    current_directory,None,view_mode.get(),sort_mode.get(),on_done,save_mode.get(),session))
        def add(continue_adding=False):
            ids=sorted(selected_indices)
            if not ids:return
            if comfy:
                root=Path(self.comfy_root.get()).expanduser();models=root/'models'
                if not models.is_dir():
                    self.notice_dialog(win,'ComfyUI','请先在设置中选择 ComfyUI 根目录。');self.comfy_settings();return
                batch=[dict(info,filename=files[index]['path']) for index in ids]
                preferred_names=self.comfy_batch_preferences(batch)
                remaining=len(available_indices)-len(ids)
                win.withdraw()
                def reopen():win.deiconify();win.lift();win.grab_set()
                def confirm_comfy(rel,dialog,keep_choosing=False):
                    errors=[];planned=[]
                    sizes={files[index]['path']:files[index].get('size',0) for index in ids}
                    for selected in batch:
                        try:target=comfy_repo_target(models,info,selected,rel,save_mode.get()=='flat')
                        except (KeyError,TypeError,ValueError) as exc:
                            errors.append(str(exc));continue
                        planned.append((selected,target))
                    if errors:
                        self.notice_dialog(dialog,'无法添加所选文件','\n'.join(errors[:5]))
                        return False
                    try:resolved=self.choose_destination_plan(dialog,planned)
                    except ValueError as exc:
                        self.notice_dialog(dialog,'保存位置不可用',str(exc));return False
                    if resolved is None:return False
                    added_paths=set()
                    for selected,target,replace_existing in resolved:
                        try:
                            ident=self.enqueue(selected,str(root),proxy,token,target,staged=True,
                                               known_size=sizes[selected['filename']],replace_existing=replace_existing)
                            session['ids'].append(ident);added_paths.add(selected['filename'])
                        except Exception as exc:errors.append(str(exc))
                    if errors:
                        self.notice_dialog(dialog,'部分文件未能添加','\n'.join(errors[:5]))
                        if not added_paths:return False
                    self.persist()
                    if keep_choosing:reopen_with_committed(added_paths)
                    else:finish_dialog(True)
                    self.note.set(f'已暂存 {len(added_paths)} 个 ComfyUI 下载任务。'+
                                  (f' 跳过 {len(planned)-len(resolved)} 个冲突文件。' if len(planned)>len(resolved) else ''))
                try:
                    self.comfy_column_dialog(root,models,batch,proxy,token,preferred_names,
                                             on_confirm=confirm_comfy,button_text='确认下载',
                                             secondary_text='确认并选择剩余文件' if remaining else None,
                                             on_secondary=(lambda rel,dialog:confirm_comfy(rel,dialog,True)) if remaining else None,
                                             on_back=reopen,save_mode=save_mode)
                except Exception as exc:
                    reopen();self.notice_dialog(win,'无法打开目录选择窗口',describe_error(exc))
                return
            errors=[];planned=[];flatten=save_mode.get()=='flat'
            for index in ids:
                selected=dict(info,filename=files[index]['path'])
                try:
                    target=validate_target_path(output_path(folder,selected,flatten=flatten,repository_folder=False))
                    planned.append((index,selected,target))
                except Exception as exc:errors.append(str(exc))
            if errors:
                self.notice_dialog(win,'保存路径不可用','\n'.join(errors[:5]))
                return
            try:resolved=self.choose_destination_plan(win,[((index,selected),target) for index,selected,target in planned])
            except ValueError as exc:
                self.notice_dialog(win,'保存位置不可用',str(exc));return
            if resolved is None:return
            added=0;added_paths=set()
            for (index,selected),target,replace_existing in resolved:
                try:
                    ident=self.enqueue(selected,folder,proxy,token,target,staged=True,
                                       known_size=files[index].get('size',0),replace_existing=replace_existing)
                    session['ids'].append(ident);added+=1;added_paths.add(selected['filename'])
                except Exception as exc:errors.append(str(exc))
            self.persist()
            if continue_adding and added_paths:reopen_with_committed(added_paths)
            elif added_paths:finish_dialog(True)
            elif errors:self.notice_dialog(win,'无法添加文件','\n'.join(errors[:5]))
            self.note.set('；'.join(errors[:2]) if errors else f'已暂存 {added} 个文件，等待最终确认。')

        summary_row=ttk.Frame(pane,style='Card.TFrame');summary_row.pack(fill='x',pady=(12,12))
        if not comfy:
            save_row=ttk.Frame(summary_row,style='Card.TFrame');save_row.pack(side='right')
            save_mode_switch(save_row,save_mode).pack(side='right')
            ttk.Label(save_row,text='下载保存方式',style='Card.TLabel').pack(side='right',padx=(0,10))
        statusbar=ttk.Frame(summary_row,style='Card.TFrame');statusbar.pack(side='left',fill='x')
        for variable,color in ((count_before,'#27343b'),(count_selected,'#c93f38'),(count_after,'#27343b')):
            ttk.Label(statusbar,textvariable=variable,foreground=color,background='#ffffff',
                       font=('Microsoft YaHei UI',10,'bold')).pack(side='left')
        if not comfy:
            summary_compact=[None]
            def arrange_summary(_event=None):
                compact=summary_row.winfo_width()<save_row.winfo_reqwidth()+statusbar.winfo_reqwidth()+16
                if compact==summary_compact[0]:return
                summary_compact[0]=compact
                save_row.pack_forget();statusbar.pack_forget()
                if compact:
                    save_row.pack(anchor='e',pady=(0,8))
                    statusbar.pack(anchor='w')
                else:
                    save_row.pack(side='right')
                    statusbar.pack(side='left')
            summary_row.bind('<Configure>',arrange_summary,add='+')

        bottom=ttk.Frame(pane,style='Card.TFrame');bottom.pack(fill='x')
        selection_group=ttk.Frame(bottom,style='Card.TFrame');selection_group.pack(side='left')
        confirm_group=ttk.Frame(bottom,style='Card.TFrame');confirm_group.pack(side='right')
        ttk.Button(selection_group,text='全选',command=select_all).pack(side='left')
        ttk.Button(selection_group,text='反选',command=invert_selection).pack(side='left',padx=(8,0))
        ttk.Button(selection_group,text='取消所有',command=clear_selection).pack(side='left',padx=(8,0))
        select_marked_button=ttk.Button(selection_group,text='选择',command=select_highlighted)
        select_marked_button.pack(side='left',padx=(18,0))
        clear_marked_button=ttk.Button(selection_group,text='取消选择',command=clear_highlighted)
        clear_marked_button.pack(side='left',padx=(8,0))
        add_button=ttk.Button(confirm_group,text='确认并选择下载位置' if comfy else '确认并下载',
                               style='Accent.TButton',command=add)
        add_button.pack(side='right')
        continue_button=ttk.Button(confirm_group,text='确认并选择剩余文件',command=lambda:add(True)) if not comfy else None
        bottom_compact=[None]
        def arrange_bottom(_event=None):
            compact=bottom.winfo_width()<selection_group.winfo_reqwidth()+confirm_group.winfo_reqwidth()+16
            if compact==bottom_compact[0]:return
            bottom_compact[0]=compact
            selection_group.pack_forget();confirm_group.pack_forget()
            if compact:
                selection_group.pack(fill='x')
                confirm_group.pack(fill='x',pady=(8,0))
            else:
                selection_group.pack(side='left')
                confirm_group.pack(side='right')
        bottom.bind('<Configure>',arrange_bottom,add='+')
        confirm_group.bind('<Configure>',arrange_bottom,add='+')

        render_columns()
        change_view(view_mode.get())
        draw_view_switch(0.0 if view_mode.get()=='tree' else 1.0)
        win.update_idletasks();win.deiconify();win.lift()

    def render(self, job):
        total=job.get('size',0);done=job.get('downloaded',0)
        pct=f'{min(100,done/total*100):.1f}%' if total else '—'
        avg=done/job['elapsed'] if job.get('elapsed',0)>0 else 0
        eta=human_duration((total-done)/job.get('speed',0)) if total and job.get('speed',0)>0 and job['status'] in ('active','checking') else '—'
        state=STATES.get(job['status'],job['status'])
        if job['status']=='complete':state='✓ 已完成 · 已校验' if job.get('sha256') else '✓ 已完成'
        elif job.get('priority') and job['status'] in ('preparing','waiting','paused','error'):state='★ '+state
        self.tree.item(job['id'],values=(job['filename'],state,pct,f'{human_size(done)} / {human_size(total) if total else "待获取"}',human_size(job.get('speed',0))+'/s',eta,human_size(avg)+'/s' if avg else '—',f'{job["connections"]} 路'),tags=(job['status'],))

    def poller(self):
        keys=['gid','status','totalLength','completedLength','downloadSpeed','errorMessage','errorCode','verifiedLength','verifyIntegrityPending']
        while not self.stop.wait(1):
            try:
                rows=self.engine.call('tellActive',keys)+self.engine.call('tellWaiting',0,10000,keys)+self.engine.call('tellStopped',0,10000,keys)
                self.events.put(lambda rows=rows:self.apply_status(rows))
            except Exception:
                if not self.stop.is_set():self.events.put(lambda:self.note.set('下载引擎暂未响应，正在重试…'))

    def apply_status(self, rows):
        now=time.monotonic();delta=min(now-self.last_poll,10);self.last_poll=now
        by_gid={r['gid']:r for r in rows}
        for job in list(self.jobs.values()):
            if job.get('starting') or job['status'] in ('complete','removing'):continue
            row=by_gid.get(job.get('gid'))
            if not row:continue
            previous_status=job['status']
            if job['status'] in ('active','checking'):job['elapsed']+=delta
            status=row['status']
            job.update(downloaded=int(row.get('completedLength',0)),speed=int(row.get('downloadSpeed',0)))
            if int(row.get('totalLength',0)):job['size']=int(row['totalLength'])
            if status=='active' and (int(row.get('verifiedLength',0))>0 or row.get('verifyIntegrityPending')=='true'):status='checking'
            if status=='removed':status='paused'
            if status=='error':
                code=row.get('errorCode','')
                reason={'3':'文件不存在','5':'下载速度过低，重试未成功','9':'磁盘空间不足','13':'文件已存在','22':'HTTP 请求失败；检查代理或 HF Token','24':'认证失败','32':'SHA-256 校验失败'}.get(code,'网络或服务器错误')
                job['error']=reason+'（代码 '+code+'）。可点击继续 / 重试。'
            if status=='complete':
                try:
                    final=Path(job['path']);partial=final.with_name(final.name+'.hfdownload')
                    if final.exists() and not job.get('replace_existing'):raise ValueError('最终文件已存在，为避免覆盖，保留临时文件。')
                    if not partial.exists() or (job['size'] and partial.stat().st_size!=job['size']):raise ValueError('文件大小不匹配，未标记完成。')
                    if job.get('replace_existing'):partial.replace(final)
                    else:partial.rename(final)
                    cleaned=remove_completed_identity(final)
                    self.note.set('下载完成：'+job['filename'] if cleaned else '下载完成，但来源记录清理失败：'+job['filename'])
                    completed_gid=job.get('gid');job['gid']=None
                    if completed_gid:self.submit(lambda gid=completed_gid:self.engine.call('removeDownloadResult',gid),lambda _:None)
                except Exception as exc:
                    status='error';job['error']=str(exc)
            job['status']=status
            self.render(job)
            if status=='active' and previous_status!='active':
                self.note.set('正在下载：'+job['filename'])
            elif status=='checking' and previous_status!='checking':
                self.note.set('下载完成，正在校验：'+job['filename'])
            elif status=='paused' and previous_status!='paused':
                self.note.set('已暂停：'+job['filename'])
            elif status=='error' and previous_status!='error':
                self.note.set('下载失败：'+job.get('error',job['filename']))
        self.refresh_summary();self.selection_changed()

    def refresh_summary(self):
        finished=sum(j['status']=='complete' for j in self.jobs.values())
        self.summary.set(f'{len(self.jobs)} 个任务 · {finished} 个完成')
        self.speed.set(human_size(sum(j.get('speed',0) for j in self.jobs.values() if j['status'] in ('active','checking')) )+'/s')
        if self.jobs:self.empty_queue.place_forget()
        else:self.empty_queue.place(relx=.5,rely=.5,anchor='center')

    def selection_changed(self, *_):
        ids=self.tree.selection()
        if len(ids)==1 and ids[0] in self.jobs:
            j=self.jobs[ids[0]]
            self.details.set((j.get('error')+' · ' if j.get('error') else '')+j['path'])
        elif len(ids)>1:self.details.set(f'已选中 {len(ids)} 个任务。')
        else:self.details.set('选中任务可查看完整文件名、保存位置和错误信息。')
        selected=[self.jobs[ident] for ident in ids if ident in self.jobs]
        enabled=(any(job['status'] in ('active','waiting','checking') for job in selected),
                 any(job['status'] in ('paused','error') for job in selected),
                 True,bool(selected),bool(selected))
        for button,available in zip(self.task_action_buttons,enabled):
            button.state(['!disabled'] if available else ['disabled'])

    def begin_column_resize(self, event):
        self._resize_column=None
        self._resize_widths=None
        if self.tree.identify_region(event.x,event.y)!='separator':return
        column_id=self.tree.identify_column(event.x)
        try:index=int(column_id[1:])-1
        except (TypeError,ValueError):return
        columns=tuple(self.tree['columns'])
        if not 0<=index<len(columns):return
        self._resize_column=columns[index]
        self._resize_widths={column:int(self.tree.column(column,'width')) for column in columns}

    def finish_column_resize(self, _event):
        resized=self._resize_column
        before=self._resize_widths
        self._resize_column=None
        self._resize_widths=None
        if not resized or not before:return
        columns=list(self.tree['columns'])
        current=int(self.tree.column(resized,'width'))
        delta=current-before[resized]
        if not delta:return
        others=[column for column in columns if column!=resized]
        widths={column:before[column] for column in others}
        if delta>0:
            minimum={column:int(self.tree.column(column,'minwidth')) for column in others}
            available=sum(max(0,widths[column]-minimum[column]) for column in others)
            adjustment=min(delta,available)
            self.tree.column(resized,width=before[resized]+adjustment)
            remaining=adjustment
            active=[column for column in others if widths[column]>minimum[column]]
            while remaining and active:
                share=max(1,(remaining+len(active)-1)//len(active))
                next_active=[]
                for column in active:
                    take=min(share,widths[column]-minimum[column],remaining)
                    widths[column]-=take
                    remaining-=take
                    if widths[column]>minimum[column]:next_active.append(column)
                active=next_active
        else:
            adjustment=-delta
            share,remainder=divmod(adjustment,len(others))
            for index,column in enumerate(others):
                widths[column]+=share+(1 if index<remainder else 0)
        for column,width in widths.items():self.tree.column(column,width=width)

    def begin_task_drag(self, event):
        region=self.tree.identify_region(event.x,event.y)
        if region in ('separator','heading'):
            self._task_drag=None;return
        self.tree.focus_set()
        item=self.tree.identify_row(event.y)
        ctrl=bool(event.state&0x0004);shift=bool(event.state&0x0001)
        children=list(self.tree.get_children())
        if item:
            if shift and self.tree.focus() in children:
                start=children.index(self.tree.focus());end=children.index(item)
                self.tree.selection_set(children[min(start,end):max(start,end)+1])
            elif ctrl:
                if item in self.tree.selection():self.tree.selection_remove(item)
                else:self.tree.selection_add(item)
            else:self.tree.selection_set(item)
            self.tree.focus(item)
        elif not ctrl:self.tree.selection_remove(*self.tree.selection())
        base=set(self.tree.selection()) if ctrl else set()
        self._task_drag={'start_y':event.y,'base':base,'dragged':False}
        self.selection_changed()
        return 'break'

    def drag_select_tasks(self, event):
        state=self._task_drag
        if not state:return
        if abs(event.y-state['start_y'])<4:return 'break'
        state['dragged']=True
        low=min(state['start_y'],event.y);high=max(state['start_y'],event.y)
        swept=[]
        for item in self.tree.get_children():
            box=self.tree.bbox(item)
            if box and box[1]+box[3]>=low and box[1]<=high:swept.append(item)
        chosen=state['base'].union(swept)
        self.tree.selection_set(tuple(chosen))
        if swept:self.tree.focus(swept[-1])
        self.selection_changed()
        return 'break'

    def finish_task_drag(self, _event):
        self._task_drag=None
        self.selection_changed()

    def select_all_tasks(self):
        self.tree.selection_set(*self.jobs)
        self.selection_changed()

    def invert_task_selection(self):
        selected=set(self.tree.selection())
        self.tree.selection_set(*(ident for ident in self.jobs if ident not in selected))
        self.selection_changed()

    def clear_task_selection(self):
        self.tree.selection_remove(*self.jobs)
        self.selection_changed()

    def pause_selected(self):
        self.pause_ids(self.tree.selection())

    def pause_ids(self, ids):
        changed=0
        for ident in ids:
            if ident not in self.jobs:continue
            j=self.jobs[ident]
            if j.get('gid') and j['status'] in ('active','waiting','checking'):
                self.submit(lambda gid=j['gid']:self.engine.call('forcePause',gid),lambda _:None)
                changed+=1
        self.note.set('\u6b63\u5728\u6682\u505c\u9009\u4e2d\u4efb\u52a1\uff0c\u5df2\u4e0b\u8f7d\u5206\u6bb5\u4f1a\u4fdd\u7559\u3002' if changed else '\u9009\u4e2d\u7684\u4efb\u52a1\u5f53\u524d\u65e0\u9700\u6682\u505c\u3002')

    def resume_selected(self):
        self.resume_ids(self.tree.selection())

    def resume_ids(self, ids):
        try: _,proxy,token=self.config_snapshot()
        except Exception as exc:self.note.set(str(exc));return
        changed=0
        for ident in ids:
            if ident not in self.jobs:continue
            j=self.jobs[ident]
            if j['status'] not in ('paused','error'):continue
            try:self.check_job_capacity(ident)
            except Exception as exc:
                self.fail(ident,describe_error(exc));continue
            if not j.get('url'):
                j['status']='preparing';self.render(j)
                self.submit(lambda j=dict(j):resolve_file(j,proxy,token),lambda meta,i=ident:self.prepared(i,meta,proxy,token),lambda msg,i=ident:self.fail(i,msg))
            elif j.get('gid') and j['status']=='paused':
                def resume(gid=j['gid']):
                    self.engine.call('changeOption',gid,{'all-proxy':proxy,'header':['Authorization: Bearer '+token] if token else []})
                    return self.engine.call('unpause',gid)
                self.submit(resume,lambda _,i=ident:self.prioritize_gid(i) if self.jobs.get(i,{}).get('priority') else None,lambda msg,i=ident:self.fail(i,msg))
            else:self.start_job(ident,proxy,token)
            changed+=1
        if not changed:self.note.set('\u9009\u4e2d\u7684\u4efb\u52a1\u5f53\u524d\u65e0\u9700\u5f00\u59cb\u6216\u91cd\u8bd5\u3002')

    def remove_selected(self):
        self.remove_ids(self.tree.selection(), False)

    def remove_and_delete_selected(self):
        ids=[i for i in self.tree.selection() if i in self.jobs]
        if not ids:return
        win=tk.Toplevel(self);win.withdraw();win.title('移除记录并删除文件')
        self.center_dialog(win,760,470)
        win.configure(bg='#f5f7f8')
        surface=RoundedSurface(win,padding=(22,18),stretch_y=True)
        surface.pack(fill='both',expand=True,padx=12,pady=12)
        box=surface.content
        ttk.Label(box,text='移除记录并删除文件',style='DialogTitle.TLabel').pack(anchor='w')
        ttk.Label(box,text=f'已选择 {len(ids)} 个任务。确认后会停止任务，并删除对应的本地文件。',
                  style='Card.TLabel').pack(anchor='w',pady=(10,4))
        ttk.Label(box,text='包括正式文件、未完成文件和续传记录；保存文件夹会保留。',
                  style='Muted.TLabel').pack(anchor='w',pady=(0,12))
        actions=ttk.Frame(box,style='Card.TFrame');actions.pack(side='bottom',fill='x',pady=(14,0))
        def confirm():
            win.destroy();self.remove_ids(ids,True)
        ttk.Button(actions,text='确认移除并删除',style='DeleteConfirm.TButton',command=confirm).pack(side='right')
        cancel=ttk.Button(actions,text='取消',style='Secondary.TButton',command=win.destroy)
        cancel.pack(side='right',padx=(0,8))
        rows=ttk.Frame(box,style='Card.TFrame');rows.pack(fill='both',expand=True)
        listing=ttk.Treeview(rows,columns=('filename','path'),show='headings',style='Files.Treeview',height=8)
        listing.heading('filename',text='仓库文件');listing.column('filename',width=270,minwidth=180,stretch=True)
        listing.heading('path',text='本地保存位置');listing.column('path',width=410,minwidth=250,stretch=True)
        for ident in ids:
            job=self.jobs[ident]
            listing.insert('','end',values=(job['filename'],job['path']))
        vertical=SlimScrollbar(rows,command=listing.yview)
        horizontal=ttk.Scrollbar(rows,orient='horizontal',command=listing.xview)
        listing.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        listing.grid(row=0,column=0,sticky='nsew')
        vertical.grid(row=0,column=1,sticky='ns')
        horizontal.grid(row=1,column=0,sticky='ew')
        rows.rowconfigure(0,weight=1);rows.columnconfigure(0,weight=1)
        win.bind('<Escape>',lambda _:win.destroy())
        win.protocol('WM_DELETE_WINDOW',win.destroy)
        win.update_idletasks();win.deiconify();win.lift();cancel.focus_set()

    def remove_ids(self, ids, delete_files):
        for ident in list(ids):
            if ident not in self.jobs:continue
            job=self.jobs[ident]
            job.update(removing=True,delete_files=delete_files,remove_previous_status=job.get('status','paused'),status='removing',speed=0)
            self.render(job)
            if job.get('starting'):continue
            if job.get('gid'):self.stop_and_finish_remove(ident)
            else:self.finish_remove(ident,delete_files)
        self.persist();self.refresh_summary()
        self.note.set('\u6b63\u5728\u505c\u6b62\u4efb\u52a1\u5e76\u5220\u9664\u5bf9\u5e94\u6587\u4ef6\u3002' if delete_files else '\u5df2\u79fb\u9664\u4efb\u52a1\u8bb0\u5f55\uff0c\u78c1\u76d8\u4e0a\u7684\u6587\u4ef6\u548c\u7eed\u4f20\u8bb0\u5f55\u4ecd\u4fdd\u7559\u3002')

    def stop_and_finish_remove(self, ident):
        if ident not in self.jobs:return
        gid=self.jobs[ident].get('gid')
        if not gid:
            self.finish_remove(ident,self.jobs[ident].get('delete_files',False));return
        def stop_download():self.detach_engine_job(gid)
        def failed(message):
            job=self.jobs.get(ident)
            if not job:return
            previous=job.pop('remove_previous_status','error')
            job.pop('removing',None);job.pop('delete_files',None)
            job.update(status=previous if previous in STATES and previous!='removing' else 'error',error=message)
            self.render(job);self.persist();self.refresh_summary();self.selection_changed();self.note.set(message)
        self.submit(stop_download,lambda _:self.finish_remove(ident,self.jobs.get(ident,{}).get('delete_files',False)),failed)

    def finish_remove(self, ident, delete_files):
        job=self.jobs.get(ident)
        if not job:return
        errors=[]
        if delete_files:
            final=Path(os.path.abspath(job['path']))
            paths=(final,final.with_name(final.name+'.hfdownload'),final.with_name(final.name+'.hfdownload.aria2'),final.with_name(final.name+'.hfdownload.json'))
            for path in paths:
                try:
                    if path.exists() and path.is_file():path.unlink()
                except OSError as exc:errors.append(path.name+'\uff1a'+str(exc))
        self.jobs.pop(ident,None)
        if self.tree.exists(ident):self.tree.delete(ident)
        self.persist();self.refresh_summary();self.selection_changed()
        if errors:self.note.set('\u8bb0\u5f55\u5df2\u79fb\u9664\uff0c\u4f46\u90e8\u5206\u6587\u4ef6\u672a\u5220\u9664\uff1a'+'\uff1b'.join(errors[:2]))
        elif delete_files:self.note.set('\u5df2\u79fb\u9664\u8bb0\u5f55\uff0c\u5e76\u5220\u9664\u6587\u4ef6\u4e0e\u7eed\u4f20\u8bb0\u5f55\u3002')

    def double_click_task(self,event):
        ident=self.tree.identify_row(event.y)
        if not ident or ident not in self.jobs:return
        self.tree.selection_set(ident)
        state=self.jobs[ident]['status']
        if state in ('active','waiting','checking'):self.pause_ids((ident,))
        elif state in ('paused','error'):self.resume_ids((ident,))
        elif state=='complete':self.note.set('\u8be5\u4efb\u52a1\u5df2\u5b8c\u6210\u3002')

    def show_task_menu(self,event):
        ident=self.tree.identify_row(event.y)
        if not ident or ident not in self.jobs:return 'break'
        if ident not in self.tree.selection():self.tree.selection_set(ident)
        if self.task_menu and self.task_menu.winfo_exists():self.task_menu.close()
        single=len(self.tree.selection())==1
        actions=[('打开文件夹','',self.open_folder,True),
                 ('优先下载','',self.prioritize_selected,single and self.jobs[ident]['status'] not in ('complete','removing')),
                 (None,None,None,None),
                 ('复制下载链接','',self.copy_download_link,single),
                 ('打开原始下载页面','',self.open_source_page,single)]
        self.task_menu=EditContextMenu(self.tree,actions)
        self.task_menu.show(event.x_root,event.y_root)
        return 'break'

    def prioritize_selected(self):
        ids=self.tree.selection()
        if len(ids)!=1 or ids[0] not in self.jobs:return
        ident=ids[0];job=self.jobs[ident]
        if job['status'] in ('complete','removing'):return
        if job['status'] in ('active','checking'):
            self.note.set('这个任务已经在下载中。');return
        job['priority']=True
        self.tree.move(ident,'',0)
        self.render(job);self.persist()
        if job['status']=='waiting' and job.get('gid'):
            self.prioritize_gid(ident)
        elif job['status'] in ('paused','error'):
            self.resume_ids((ident,))
        else:
            self.note.set('已设为优先任务，准备完成后会排到下载队列最前。')

    def prioritize_gid(self, ident):
        job=self.jobs.get(ident)
        if not job or not job.get('gid') or job.get('removing'):return
        gid=job['gid']
        def move_to_front():
            status=self.engine.call('tellStatus',gid,['status'])['status']
            if status=='waiting':self.engine.call('changePosition',gid,0,'POS_SET')
            return status
        def done(status):
            if ident not in self.jobs:return
            if status=='waiting':self.note.set('已排到下载队列最前，当前任务完成或有空位后开始：'+job['filename'])
            elif status=='active':self.note.set('优先任务已开始下载：'+job['filename'])
        self.submit(move_to_front,done,lambda msg:self.note.set('调整优先顺序失败：'+msg))

    def copy_download_link(self):
        ids=self.tree.selection()
        if len(ids)!=1 or ids[0] not in self.jobs:return
        job=self.jobs[ids[0]]
        link=job.get('source_url') or job.get('url') or file_url(job)
        self.clipboard_clear();self.clipboard_append(link);self.update()
        self.note.set('已复制文件的直接下载链接。')

    def open_source_page(self):
        ids=self.tree.selection()
        if len(ids)!=1 or ids[0] not in self.jobs:return
        try:
            os.startfile(original_page_url(self.jobs[ids[0]]))
            self.note.set('\u5df2\u6253\u5f00\u539f\u59cb\u4e0b\u8f7d\u9875\u9762\u3002')
        except OSError as exc:
            self.note.set('\u65e0\u6cd5\u6253\u5f00\u4e0b\u8f7d\u9875\u9762\uff1a'+str(exc))

    def open_folder(self):
        ids=self.tree.selection()
        folder=Path(self.jobs[ids[0]]['path']).parent if ids else Path(self.folder.get())
        if folder.is_dir():os.startfile(str(folder))
        else:self.note.set('保存目录尚未创建。')

    def change_parallel(self,*_):
        value=self.parallel.get()
        self.submit(lambda:self.engine.call('changeGlobalOption',{'max-concurrent-downloads':value}),lambda _:self.note.set('同时下载文件数已更新。'))
        self.persist()

    def change_connections(self):
        self.persist()
        self.note.set('\u6bcf\u6587\u4ef6\u8fde\u63a5\u6570\u5df2\u8bbe\u4e3a '+self.connections.get()+' \u8def\uff0c\u4ec5\u5f71\u54cd\u4e4b\u540e\u65b0\u6dfb\u52a0\u7684\u4efb\u52a1\u3002')

    def advanced(self):
        win=tk.Toplevel(self);win.title('HF Token \u4e0e\u4f7f\u7528\u5e2e\u52a9');self.center_dialog(win,680,500)
        win.configure(bg='#f5f7f8')
        surface=RoundedSurface(win,padding=(24,20),stretch_y=True);surface.pack(fill='both',expand=True,padx=12,pady=12)
        box=surface.content
        ttk.Label(box,text='HF Token',style='DialogTitle.TLabel').pack(anchor='w')
        status=tk.StringVar(value='\u5f53\u524d\u72b6\u6001\uff1a'+('\u5df2\u6dfb\u52a0' if self.token.get().strip() else '\u672a\u6dfb\u52a0'))
        ttk.Label(box,textvariable=status,style='Muted.TLabel',foreground='#344550').pack(anchor='w',pady=(4,8))
        ttk.Entry(box,textvariable=self.token,show='\u2022',width=70).pack(fill='x')
        ttk.Label(box,text='获取方法：\n1. 登录 Hugging Face；',style='Card.TLabel',justify='left').pack(anchor='w',pady=(14,0))
        link_row=ttk.Frame(box,style='Card.TFrame');link_row.pack(anchor='w')
        ttk.Label(link_row,text='2. 打开 ',style='Card.TLabel').pack(side='left')
        token_url='https://huggingface.co/settings/tokens'
        token_link=tk.Label(link_row,text=token_url,bg='#ffffff',fg='#246f9b',cursor='hand2',
                            font=('Microsoft YaHei UI',10,'underline'))
        token_link.pack(side='left')
        def open_token_page(_event=None):
            try:os.startfile(token_url)
            except OSError as exc:status.set('无法打开 Token 页面：'+describe_error(exc))
        token_link.bind('<Button-1>',open_token_page)
        ttk.Label(link_row,text='；',style='Card.TLabel').pack(side='left')
        text=('3. 点击 Create new token，名称自定，权限选 Read；\n'
              '4. 复制以 hf_ 开头的 Token，粘贴到上方后保存。\n\n'
              '为什么填写：它可用于下载受限模型、减少匿名请求限制并使所有任务使用同一账号身份。\n\n'
              'Token 保存在当前 Windows 用户的凭据管理器，不存放在软件目录，也不写入队列或设置文件。\n'
              '关闭软件后仍会保留，后续任务都可使用；请勿向他人分享 Token。')
        ttk.Label(box,text=text,style='Card.TLabel',wraplength=605,justify='left').pack(anchor='w')
        buttons=ttk.Frame(box,style='Card.TFrame');buttons.pack(fill='x',pady=(16,0))
        def save():
            value=self.token.get().strip()
            if not value:
                status.set('\u5f53\u524d\u72b6\u6001\uff1a\u8bf7\u5148\u7c98\u8d34 Token');return
            try:
                save_token(value)
                status.set('\u5f53\u524d\u72b6\u6001\uff1a\u5df2\u6dfb\u52a0')
                self.note.set('HF Token \u5df2\u4fdd\u5b58\uff0c\u540e\u7eed\u6240\u6709\u4efb\u52a1\u90fd\u4f1a\u4f7f\u7528\u3002')
            except Exception as exc:
                status.set('\u4fdd\u5b58\u5931\u8d25\uff1a'+describe_error(exc))
        def remove():
            try:
                delete_saved_token()
                self.token.set('');status.set('\u5f53\u524d\u72b6\u6001\uff1a\u672a\u6dfb\u52a0')
                self.note.set('HF Token \u5df2\u5220\u9664\u3002')
            except Exception as exc:
                status.set('\u5220\u9664\u5931\u8d25\uff1a'+describe_error(exc))
        ttk.Button(buttons,text='\u4fdd\u5b58 Token',style='Accent.TButton',command=save).pack(side='left')
        ttk.Button(buttons,text='\u5220\u9664\u5df2\u4fdd\u5b58 Token',command=remove).pack(side='left',padx=8)
        ttk.Button(buttons,text='\u5b8c\u6210',command=win.destroy).pack(side='right')
        win.update_idletasks();win.deiconify();win.lift()

    def persist(self):
        try:
            save_json(self.data/'settings.json',{'schema_version':1,'folder':self.folder.get(),'default_folder':Path(self.folder.get()).expanduser()==self.default_downloads,'proxy':self.proxy.get(),'parallel':int(self.parallel.get()),'connections':int(self.connections.get()),'comfy_root':self.comfy_root.get(),'diffusion_dir':self.diffusion_dir.get(),'comfy_favorites':self.comfy_favorites,'input_rows':self.input_rows},keep_backup=True)
            jobs=[{k:v for k,v in self.jobs[ident].items() if k not in ('gid','starting','removing','delete_files','remove_previous_status')}
                  for ident in self.tree.get_children() if ident in self.jobs]
            save_json(self.data/'queue.json',{'schema_version':QUEUE_SCHEMA_VERSION,'jobs':jobs},keep_backup=True)
        except Exception as exc:self.note.set('无法保存设置：'+describe_error(exc))

    def autosave(self):
        if not self.closing:self.persist();self.after(5000,self.autosave)

    def close_app(self):
        if getattr(self,'closing',False):return
        self.closing=True
        stop=getattr(self,'stop',None)
        if stop is not None:stop.set()
        if 'data' in self.__dict__ and 'tree' in self.__dict__:
            self.persist()
        try:self.withdraw()
        except tk.TclError:pass
        workers=getattr(self,'workers',None)
        if workers is not None:workers.shutdown(wait=False,cancel_futures=True)
        engine=getattr(self,'engine',None)
        try:
            if engine is not None:engine.close()
        finally:
            try:self.destroy()
            except tk.TclError:pass

def main():
    if os.name=='nt':
        try:ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:pass
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateMutexW.restype=wintypes.HANDLE
        name='Local\\HFDesktopDownloader.SingleInstance.v1'
        mutex=kernel.CreateMutexW(None,False,name)
        if ctypes.get_last_error()==183:
            user32=ctypes.windll.user32
            user32.FindWindowW.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p]
            user32.FindWindowW.restype=ctypes.c_void_p
            existing=user32.FindWindowW(None,f'HF 模型下载器 v{APP_VERSION}')
            if existing:
                user32.ShowWindow(existing,9)
                user32.SetForegroundWindow(existing)
            user32.MessageBoxW(existing or None,'HF 模型下载器已经打开。请在现有窗口中操作。','HF 模型下载器',0x40)
            return
    app=App()
    app.mainloop()

if __name__=='__main__':main()
