"""HF file resolution and local aria2 RPC. No GUI dependency."""
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import time
from urllib import request, parse, error

HOSTS = {'huggingface.co', 'www.huggingface.co', 'hf.co'}

def parse_hf_url(text):
    text = text.strip().replace('\\_', '_')
    p = parse.urlsplit(text)
    if p.scheme != 'https' or p.hostname not in HOSTS or p.username or p.password:
        raise ValueError('请输入 https://huggingface.co/ 开头的模型或文件链接。')
    parts = [parse.unquote(x) for x in p.path.strip('/').split('/') if x]
    kind = 'model'
    if parts and parts[0] == 'datasets':
        kind, parts = 'dataset', parts[1:]
    markers = ('resolve', 'blob', 'tree')
    marker = 2 if len(parts) > 2 and parts[2] in markers else (1 if len(parts) > 2 and parts[1] in markers else None)
    if marker is None:
        if not 1 <= len(parts) <= 2:
            raise ValueError('不能识别仓库地址，请使用仓库首页或文件的下载链接。')
        repo, revision, filename = '/'.join(parts), 'main', ''
    else:
        if marker not in (1, 2) or len(parts) < marker + 2:
            raise ValueError('文件链接缺少仓库名或版本。')
        repo, revision = '/'.join(parts[:marker]), parts[marker + 1]
        filename = '/'.join(parts[marker + 2:]) if parts[marker] != 'tree' else ''
        folder = '/'.join(parts[marker + 2:]) if parts[marker] == 'tree' else ''
        if parts[marker] != 'tree' and not filename:
            raise ValueError('文件链接中没有文件名。')
    if marker is None:
        folder = ''
    for value in (repo, revision, filename, folder):
        if any(x in ('.', '..') for x in value.split('/')) or '\\' in value or '\x00' in value:
            raise ValueError('链接中包含不安全的路径。')
    return {'repo': repo, 'revision': revision, 'filename': filename, 'folder': folder, 'kind': kind}

def file_url(info, filename=None, revision=None):
    prefix = 'datasets/' if info['kind'] == 'dataset' else ''
    return ('https://huggingface.co/' + prefix + parse.quote(info['repo'], safe='/') + '/resolve/'
            + parse.quote(revision or info['revision'], safe='') + '/'
            + parse.quote(filename or info['filename'], safe='/'))

def file_page_url(info):
    prefix = 'datasets/' if info['kind'] == 'dataset' else ''
    return ('https://huggingface.co/' + prefix + parse.quote(info['repo'], safe='/') + '/blob/'
            + parse.quote(info['revision'], safe='') + '/'
            + parse.quote(info['filename'], safe='/'))

def validate_proxy(value):
    value = value.strip()
    if not value:
        return ''
    if '://' not in value:
        value = 'http://' + value
    p = parse.urlsplit(value)
    if p.scheme != 'http' or not p.hostname or p.username or p.password or p.path not in ('', '/') or p.query or p.fragment:
        raise ValueError('代理请填写 HTTP／混合端口，例如 http://127.0.0.1:10808；留空使用直连。')
    try:
        if not p.port:
            raise ValueError()
    except ValueError:
        raise ValueError('代理地址必须包含有效端口。')
    return value.rstrip('/')

def safe_piece(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).rstrip(' .')
    if not name or name.upper().split('.')[0] in {'CON','PRN','AUX','NUL', *('COM'+str(i) for i in range(1,10)), *('LPT'+str(i) for i in range(1,10))}:
        name = '_' + name
    return name

def output_path(folder, info, flatten=False, repository_folder=True):
    pieces = info['filename'].split('/')
    if not pieces or any(x in ('', '.', '..') for x in pieces):
        raise ValueError('不安全的文件路径。')
    target = Path(folder).resolve()
    if repository_folder:
        target /= safe_piece(info['repo'].replace('/', '--'))
    for piece in (pieces[-1:] if flatten else pieces):
        target /= safe_piece(piece)
    if not target.is_relative_to(Path(folder).resolve()):
        raise ValueError('文件不能保存到所选目录之外。')
    if len(str(target)) > 220:
        raise ValueError('保存路径太长，请选择较短的保存目录。')
    return target

def remove_completed_identity(final):
    final = Path(final)
    partial = final.with_name(final.name + '.hfdownload')
    if final.is_file() and not partial.exists():
        identity = final.with_name(final.name + '.hfdownload.json')
        try:
            identity.unlink(missing_ok=True)
        except OSError:
            return False
    return True

def detect_model_type(filename):
    parts = filename.lower().replace('\\', '/').strip('/').split('/')
    name = parts[-1]
    # A token in the file's own name is more specific than a parent directory.
    name_rules = (
        (r'(?<![a-z0-9])loras?(?![a-z0-9])', 'LoRA'),
        (r'(?<![a-z0-9])vae(?![a-z0-9])', 'VAE'),
        (r'controlnets?', 'ControlNet'),
        (r'(?<![a-z0-9])(?:text[_-]?encoders?|clip(?:[_-]?vision)?|t5xxl)(?![a-z0-9])', 'CLIP / \u6587\u672c\u7f16\u7801\u5668'),
        (r'(?<![a-z0-9])(?:upscalers?|esrgan|realesrgan)(?![a-z0-9])', '\u653e\u5927\u6a21\u578b'),
        (r'(?<![a-z0-9])(?:embeddings?|textual[_-]?inversion)(?![a-z0-9])', '\u5d4c\u5165'),
        (r'(?<![a-z0-9])(?:unet|diffusion[_-]?models?|transformers?|dit)(?![a-z0-9])', 'UNet / \u6269\u6563\u6a21\u578b'),
        (r'(?<![a-z0-9])checkpoints?(?![a-z0-9])', '\u4e3b\u6a21\u578b'),
    )
    for pattern, kind in name_rules:
        if re.search(pattern, name):
            return kind
    folder_types = {
        'lora':'LoRA', 'loras':'LoRA', 'vae':'VAE',
        'text_encoder':'CLIP / \u6587\u672c\u7f16\u7801\u5668', 'text_encoders':'CLIP / \u6587\u672c\u7f16\u7801\u5668',
        'clip':'CLIP / \u6587\u672c\u7f16\u7801\u5668', 'clip_vision':'CLIP / \u6587\u672c\u7f16\u7801\u5668',
        't5':'CLIP / \u6587\u672c\u7f16\u7801\u5668', 'llm':'CLIP / \u6587\u672c\u7f16\u7801\u5668',
        'controlnet':'ControlNet', 'controlnets':'ControlNet',
        'upscale_models':'\u653e\u5927\u6a21\u578b', 'upscalers':'\u653e\u5927\u6a21\u578b',
        'esrgan':'\u653e\u5927\u6a21\u578b', 'realesrgan':'\u653e\u5927\u6a21\u578b',
        'embeddings':'\u5d4c\u5165', 'embedding':'\u5d4c\u5165', 'textual_inversion':'\u5d4c\u5165',
        'unet':'UNet / \u6269\u6563\u6a21\u578b', 'diffusion_models':'UNet / \u6269\u6563\u6a21\u578b',
        'transformer':'UNet / \u6269\u6563\u6a21\u578b', 'transformers':'UNet / \u6269\u6563\u6a21\u578b',
        'dit':'UNet / \u6269\u6563\u6a21\u578b', 'checkpoint':'\u4e3b\u6a21\u578b', 'checkpoints':'\u4e3b\u6a21\u578b',
    }
    for folder in reversed(parts[:-1]):
        kind = folder_types.get(folder.replace('-', '_'))
        if kind:
            return kind
    return '\u5176\u4ed6'

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def opener(proxy='', no_redirect=False):
    handlers = [request.ProxyHandler({'http': proxy, 'https': proxy} if proxy else {})]
    if no_redirect:
        handlers.append(NoRedirect())
    return request.build_opener(*handlers)

def headers(token):
    return {'User-Agent': 'HF-Desktop-Downloader/1.0', **({'Authorization':'Bearer ' + token} if token else {})}

def describe_error(exc):
    if isinstance(exc, error.HTTPError):
        if exc.code in (401,403):
            return '访问被拒绝：请确认已获得模型授权，并在高级设置填写 HF Token。'
        if exc.code == 404:
            return '文件或仓库不存在，请检查链接、文件名和版本。'
        if exc.code == 429:
            return 'HF 请求频率受限，请稍后重试，或填写自己的 HF Token。'
        return 'HF 请求失败，HTTP ' + str(exc.code)
    # Do not expose headers, tokens or temporary signed URLs in the UI.
    text = str(exc)
    text = re.sub(r'https?://\S+', '[地址]', text)
    return text[:240]

def resolve_file(info, proxy='', token=''):
    req = request.Request(file_url(info), headers=headers(token), method='HEAD')
    try:
        response = opener(proxy, True).open(req, timeout=25)
    except error.HTTPError as exc:
        if exc.code not in (301,302,303,307,308):
            raise
        response = exc
    with response:
        h = response.headers
        size = h.get('X-Linked-Size') or h.get('Content-Length')
        digest = (h.get('X-Linked-Etag') or h.get('ETag') or '').strip('"')
        commit = h.get('X-Repo-Commit')
        if not commit or not re.fullmatch(r'[0-9a-f]{40,64}', commit):
            raise ValueError('无法取得 HF 文件版本，请确认代理能访问官方源。')
        result = dict(info, revision=commit, size=int(size or 0))
        result['sha256'] = digest if re.fullmatch(r'[0-9a-f]{64}', digest) else ''
        result['url'] = file_url(result)
        return result

def list_repo(info, proxy='', token=''):
    plural = 'datasets' if info['kind'] == 'dataset' else 'models'
    folder = info.get('folder', '')
    tree_path = parse.quote(info['revision'], safe='')
    if folder:
        tree_path += '/' + parse.quote(folder, safe='/')
    url = f"https://huggingface.co/api/{plural}/{parse.quote(info['repo'], safe='/')}/tree/{tree_path}?recursive=true&limit=1000"
    files = []
    op = opener(proxy)
    for _ in range(100):
        with op.open(request.Request(url, headers=headers(token)), timeout=30) as response:
            rows = json.load(response)
            links = response.headers.get('Link', '')
        for row in rows:
            path = row.get('path', '')
            if row.get('type') == 'file' and (not folder or path == folder or path.startswith(folder + '/')):
                files.append({'path': path, 'size': row.get('size', 0)})
        match = re.search(r'<([^>]+)>;\s*rel="next"', links)
        if not match:
            return files
        url = match.group(1)
        next_page=parse.urlsplit(url)
        if next_page.scheme!='https' or next_page.hostname not in HOSTS or next_page.username or next_page.password:
            raise ValueError('拒绝非官方的分页地址。')
    raise ValueError('仓库文件过多，请直接粘贴所需文件链接。')

class Engine:
    def __init__(self, binary, data_dir, concurrent=3):
        self.secret = secrets.token_hex(24)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.endpoint = f'http://127.0.0.1:{self.port}/jsonrpc'
        self.http = opener('')
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.log = (data_dir / 'engine.log').open('w', encoding='utf-8')
        env = {k:v for k,v in os.environ.items() if k.lower() not in ('http_proxy','https_proxy','all_proxy','no_proxy')}
        cmd = [str(binary), '--no-conf=true', '--enable-rpc=true', '--rpc-listen-all=false', '--rpc-listen-port='+str(self.port), '--rpc-secret='+self.secret,
               '--max-concurrent-downloads='+str(concurrent), '--max-download-result=10000', '--keep-unfinished-download-result=true',
               '--file-allocation=none', '--auto-save-interval=5', '--console-log-level=warn', '--summary-interval=0', '--enable-color=false', '--no-netrc=true', '--check-certificate=true']
        self.process = subprocess.Popen(cmd, env=env, stdout=self.log, stderr=subprocess.STDOUT,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        for _ in range(60):
            try:
                self.call('getVersion')
                return
            except Exception:
                if self.process.poll() is not None:
                    break
                time.sleep(.1)
        self.close()
        raise RuntimeError('下载引擎启动失败，请确认 aria2c.exe 未被安全软件隔离。')

    def call(self, method, *params):
        body = json.dumps({'jsonrpc':'2.0','id':'hf','method':'aria2.'+method,'params':['token:'+self.secret, *params]}).encode()
        req = request.Request(self.endpoint, data=body, headers={'Content-Type':'application/json'})
        try:
            response = self.http.open(req, timeout=3)
        except error.HTTPError as exc:
            if exc.code != 400:
                raise
            response = exc
        with response:
            data = json.load(response)
        if 'error' in data:
            raise RuntimeError(data['error']['message'])
        return data['result']

    def add(self, job, proxy='', token='', paused=False):
        final = Path(job['path'])
        if final.exists() and (not job.get('replace_existing') or not final.is_file()):
            raise ValueError('目标文件已存在。为避免覆盖，请换一个保存目录。')
        final.parent.mkdir(parents=True, exist_ok=True)
        identity = final.with_name(final.name + '.hfdownload.json')
        partial = final.with_name(final.name + '.hfdownload')
        expected = {'url': job['url'], 'sha256': job.get('sha256', '')}
        if partial.exists():
            try:
                previous = json.loads(identity.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                raise ValueError('发现来源不明的未完成文件，请换一个保存目录。')
            if previous != expected:
                raise ValueError('未完成文件属于不同版本，请换一个保存目录，避免混用分段。')
        save_json(identity, expected)
        opts = {'dir':str(final.parent), 'out':final.name+'.hfdownload', 'max-connection-per-server':str(job['connections']),
                'split':str(job['connections']), 'min-split-size':'4M', 'continue':'true', 'auto-file-renaming':'false',
                'allow-overwrite':'false', 'all-proxy':proxy, 'max-tries':'10', 'retry-wait':'5', 'timeout':'60',
                'connect-timeout':'20', 'lowest-speed-limit':'20K', 'pause':'true' if paused else 'false', 'check-integrity':'true'}
        if job.get('sha256'):
            opts['checksum'] = 'sha-256='+job['sha256']
        if token:
            opts['header'] = ['Authorization: Bearer '+token]
        return self.call('addUri', [job['url']], opts)

    def close(self):
        if getattr(self, 'process', None) and self.process.poll() is None:
            try:
                self.call('forcePauseAll')
                self.call('shutdown')
                self.process.wait(timeout=8)
            except Exception:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        if getattr(self, 'log', None):
            self.log.close()

def save_json(path, value, keep_backup=False):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    backup = path.with_suffix(path.suffix + '.bak')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    if keep_backup and path.exists():
        try:
            current=path.read_text(encoding='utf-8')
            json.loads(current)
            backup.write_text(current,encoding='utf-8')
        except (OSError,ValueError):
            pass
    tmp.replace(path)

def human_size(value):
    value = float(value)
    for unit in ('B','KiB','MiB','GiB','TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}' if unit != 'B' else f'{value:.0f} B'
        value /= 1024
