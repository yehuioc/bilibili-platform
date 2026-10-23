"""启动或复用本项目私人库服务；不默认启动外部浏览器。"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / 'private' / 'library'


def current():
    state = PRIVATE / 'server.json'
    if not state.is_file():
        return None
    try:
        value = json.loads(state.read_text(encoding='utf-8'))
        url = value['url']
        parsed = urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1':
            return None
        with httpx.Client(base_url=url, trust_env=False, timeout=2) as client:
            client.get('/')
            response = client.get('/api/state')
            if response.status_code == 200 and 'videos' in response.json():
                return url
    except (OSError, ValueError, KeyError, httpx.HTTPError):
        return None
    return None


def main():
    PRIVATE.mkdir(parents=True, exist_ok=True)
    url = current()
    if not url:
        command = [sys.executable, '-X', 'utf8', '-B', '-m', 'bili_platform.web',
                   '--workspace', str(PRIVATE), '--port', '6861']
        settings = PRIVATE / 'settings.json'
        cookie = os.environ.get('BILIBILI_COOKIE_FILE')
        if settings.is_file():
            config = json.loads(settings.read_text(encoding='utf-8'))
            cookie = config.get('cookie_file') or cookie
            for key, option in (('dataset_file', '--dataset'), ('results_file', '--results'),
                                ('annotations_file', '--annotations')):
                if config.get(key):
                    command += [option, config[key]]
        if cookie:
            command += ['--cookie-file', cookie]
        with (PRIVATE / 'server.log').open('ab') as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for _ in range(40):
            if process.poll() is not None:
                raise RuntimeError('启动失败，请查看 private/library/server.log')
            time.sleep(0.25)
            url = current()
            if url:
                break
        if not url:
            raise RuntimeError('服务尚未就绪，请查看 private/library/server.log')
    print(f'B站工作台已就绪：{url}/connection')
    print('在 Codex 内置浏览器打开以上地址；Chrome扩展侧栏保持连接。')
    print(f'私人数据：{PRIVATE}')
    return url


if __name__ == '__main__':
    main()
