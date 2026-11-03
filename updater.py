#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CT3D 在线升级客户端

只做四件事，全部可独立测试：
  1. 从服务器拉 latest.json + latest.json.sig，用内置公钥验签
  2. 把当前版本和线上版本比对，规划出要走的补丁链
  3. 下载补丁包并校验 SHA256（签名保证索引可信，SHA 保证包未损坏）
  4. 解压到 <软件目录>\\_patch\\，交给 apply_patch.py 安装

不 import PyQt5，纯标准库，方便脱离界面单独测。
界面相关的（进度条、提示框、重启）在 main.py 里。

设计取舍
--------
* 走明文 HTTP，但索引有 RSA 签名、补丁包有 SHA256，
  即使链路被劫持也无法让客户机装上伪造的代码。
* 验签用 pow() + hashlib 自己算，不依赖 cryptography ——
  Win7 上那套 libcrypto 的坑已经踩够多了。
* 补丁链逐级下载：v1.20 → v1.21 → v1.22 会依次取，一次装完。
"""
from __future__ import print_function

import base64
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
import zipfile

# ── 服务端地址 ──────────────────────────────────────────────────────────────
# 开源版默认不指定升级服务器；在 app_settings.json 里用 "update_base_url"
# 配置你自己的服务器地址（例：http://10.0.0.8:8000），留空则不检查在线升级。
DEFAULT_BASE_URL = ''

# ── 内置公钥（RSA-2048，私钥在开发机 _releases/keys/，绝不外发）────────────
PUBKEY_N_HEX = (
    'b48682b5b807ea9172371e7472f9a85c2844a0382159347bdd0959dd8f35b8f6'
    'ce92abe149f501b5c39a0d6ac1f58974db618668f336911a17fc2c84b0db5d6d'
    'c8b1064dfc917db60f9a29926ad31d86a20bd333d877ab3211c77e263caf7436'
    'ba862b6e5e443f660e80fcf5cbaec286e70c27333a113d0da01be9ac09e5939'
    '6383a687181552ea0874575c1f0c9fa87aa36b3551910b2c934777a9cb138b'
    'bf4491ccc943fe6ec0ba79897746c51e5f90a5c3269f5d8f7294b1b59face2f'
    'b57c210971302f8b433a6c8b4d9995ec8b104f7925350f2623d67a6b0fd1a4'
    '8a61724ccdc45fafd9180b173fa125e5f0a4330d63599b9594fbdbda8342d5'
    '95b52bd5')
PUBKEY_E = 65537

MAX_CHAIN = 12          # 一个链条最多几跳，防死循环
HTTP_TIMEOUT = 10


class UpdateError(Exception):
    """升级过程中的可预期错误，界面上直接拿这句文字提示用户。"""


# ── 签名 ────────────────────────────────────────────────────────────────────

# PKCS#1 v1.5 里 SHA256 的 DigestInfo 前缀
_SHA256_DIGEST_INFO = bytes.fromhex(
    '3031300d060960864801650304020105000420')


def verify_signature(payload, sig, n_hex=None, e=None):
    """RSA PKCS#1 v1.5 验签（SHA256）。纯整数运算，无第三方依赖。"""
    n_hex = n_hex or PUBKEY_N_HEX
    e = e or PUBKEY_E
    n = int(n_hex, 16)
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    try:
        m = pow(int.from_bytes(sig, 'big'), e, n)
        em = m.to_bytes(k, 'big')
    except (ValueError, OverflowError):
        return False
    if len(em) < 11 or em[0] != 0 or em[1] != 1:
        return False
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(payload).digest()
    pad_len = k - 3 - len(digest_info)
    if pad_len < 8:
        return False
    if em[2:2 + pad_len] != b'\xff' * pad_len:
        return False
    if em[2 + pad_len] != 0:
        return False
    return em[3 + pad_len:] == digest_info


# ── 版本 ────────────────────────────────────────────────────────────────────

def parse_version(v):
    """'v1.21.0' -> (1, 21, 0)。多余的尾巴忽略，取不到就全 0。"""
    if not v:
        return (0,)
    nums = re.findall(r'\d+', str(v))
    return tuple(int(x) for x in nums[:3]) if nums else (0,)


def version_gt(a, b):
    pa, pb = parse_version(a), parse_version(b)
    n = max(len(pa), len(pb))
    pa += (0,) * (n - len(pa))
    pb += (0,) * (n - len(pb))
    return pa > pb


# ── 网络 ────────────────────────────────────────────────────────────────────

def _get(url, timeout=HTTP_TIMEOUT, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'CT3D-Updater',
                'Cache-Control': 'no-cache',
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as ex:
            # 4xx 重试没意义
            if 400 <= ex.code < 500:
                raise UpdateError('服务器返回 %d（%s）' % (ex.code, url))
            last = ex
        except (urllib.error.URLError, socket.timeout, OSError) as ex:
            last = ex
        if i < retries:
            time.sleep(1.0 + i)
    raise UpdateError('连不上升级服务器：%s' % (last,))


def fetch_index(base_url, timeout=HTTP_TIMEOUT):
    """拉索引并验签。验签失败直接抛错，绝不降级使用。"""
    if not (base_url or '').strip():
        raise UpdateError('未配置升级服务器地址（在 设置 → 在线升级服务器 里填写）')
    base = base_url.rstrip('/')
    body = _get(base + '/latest.json', timeout=timeout)
    try:
        sig_txt = _get(base + '/latest.json.sig', timeout=timeout).decode('ascii')
        sig = base64.b64decode(sig_txt.strip())
    except (ValueError, UnicodeDecodeError) as ex:
        raise UpdateError('签名文件格式错误：%s' % ex)
    if not verify_signature(body, sig):
        raise UpdateError('升级索引签名校验失败，已中止（服务器可能被篡改）')
    try:
        return json.loads(body.decode('utf-8'))
    except ValueError as ex:
        raise UpdateError('升级索引不是合法 JSON：%s' % ex)


def plan_update(base_url, current_version, timeout=HTTP_TIMEOUT):
    """返回升级计划 dict；已是最新则返回 None。

    plan = {
      'latest': 'v1.23.0',
      'notes': '...',
      'steps': [entry, entry, ...],   # 按顺序应用
      'total_size': 123456,
      'base_url': '...',
    }
    """
    idx = fetch_index(base_url, timeout=timeout)
    latest = idx.get('latest')
    if not latest or not version_gt(latest, current_version):
        return None

    patches = idx.get('patches') or {}
    steps = []
    cur = current_version
    seen = set()
    while version_gt(latest, cur):
        if cur in seen or len(steps) >= MAX_CHAIN:
            raise UpdateError('补丁链出现环或过长，已中止')
        seen.add(cur)
        entry = patches.get(cur)
        if not entry:
            raise UpdateError(
                '服务器没有 %s 起点的补丁（可能是版本跨度太大）。\n'
                '请联系技术支持要一个全量安装包。' % cur)
        if not entry.get('to') or not entry.get('file') or not entry.get('sha256'):
            raise UpdateError('补丁索引项不完整：%s' % cur)
        steps.append({
            'from': cur,
            'to': entry['to'],
            'file': entry['file'],
            'size': entry.get('size') or 0,
            'sha256': entry['sha256'],
        })
        cur = entry['to']

    return {
        'latest': latest,
        'notes': idx.get('notes') or '',
        'generated': idx.get('generated') or '',
        'steps': steps,
        'total_size': sum(s['size'] for s in steps),
        'base_url': base_url,
    }


def download_patch(base_url, step, dest_dir, progress_cb=None, cancel=None,
                   timeout=60):
    """下载单个补丁包并校验 SHA256，返回本地路径。"""
    url = base_url.rstrip('/') + '/' + step['file'].lstrip('/')
    name = os.path.basename(step['file'])
    dest = os.path.join(dest_dir, name)
    if not os.path.isdir(dest_dir):
        os.makedirs(dest_dir)

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'CT3D-Updater'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            total = int(r.headers.get('Content-Length') or step.get('size') or 0)
            done = 0
            hasher = hashlib.sha256()
            tmp = dest + '.part'
            with open(tmp, 'wb') as f:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise UpdateError('已取消')
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    done += len(chunk)
                    if progress_cb:
                        progress_cb(done, total)
            os.replace(tmp, dest)
    except UpdateError:
        raise
    except Exception as ex:
        raise UpdateError('下载失败：%s' % ex)

    if hasher.hexdigest() != step['sha256']:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise UpdateError('补丁包校验失败（下载内容与索引不符），已删除')
    return dest


# ── 落地 ────────────────────────────────────────────────────────────────────

PATCH_DIR_NAME = '_patch'
LAUNCHER_NAME = u'升级.bat'


def stage_patch(zip_path, target_dir):
    """把补丁 zip 解压到 <target>\\_patch\\，返回该目录。"""
    patch_dir = os.path.join(target_dir, PATCH_DIR_NAME)
    if os.path.isdir(patch_dir):
        shutil.rmtree(patch_dir, ignore_errors=True)
    os.makedirs(patch_dir)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            # 防 zip 路径穿越（../ 写到软件目录外）
            for name in zf.namelist():
                norm = name.replace('\\', '/')
                if norm.startswith('/') or '..' in norm.split('/'):
                    raise UpdateError('补丁包内含非法路径：%s' % name)
            zf.extractall(patch_dir)
    except UpdateError:
        raise
    except Exception as ex:
        raise UpdateError('解压补丁失败：%s' % ex)
    if not os.path.isfile(os.path.join(patch_dir, u'apply_patch.py')):
        raise UpdateError('补丁包不完整：缺少 apply_patch.py')
    return patch_dir


def clear_staged(target_dir):
    """删掉 _patch 目录（用户放弃升级时用）。"""
    shutil.rmtree(os.path.join(target_dir, PATCH_DIR_NAME), ignore_errors=True)


SETTINGS_NAME = 'app_settings.json'


def normalize_base_url(text):
    """把用户输入的地址整理成可用形式；给不出可用地址就返回空串。

    现场最常见的输入是「192.168.1.100:8080」这种不带协议头的写法，
    以前 resolve_base_url 见到就直接丢弃回退默认值，用户会以为设置没生效
    （其实是"看起来没报错、其实没采纳"）。这里统一补 http://，并且把结尾
    多余的斜杠、空格、引号都去掉 —— 这些在复制粘贴时太容易出现。
    """
    u = (text or '').strip().strip('"\'').strip()
    if not u:
        return ''
    # 只留主机 + 端口 + 可选路径，去掉误粘进来的 /latest.json
    low = u.lower()
    if low.startswith('https://'):
        scheme, rest = 'https://', u[8:]
    elif low.startswith('http://'):
        scheme, rest = 'http://', u[7:]
    else:
        scheme, rest = 'http://', u
    rest = rest.split('#', 1)[0].split('?', 1)[0]
    if rest.lower().endswith('/latest.json'):
        rest = rest[:-len('/latest.json')]
    if not rest or ' ' in rest:
        return ''
    host = rest.split('/', 1)[0]
    if not host or host.startswith(':') or host.startswith('/'):
        return ''
    return (scheme + rest).rstrip('/')


def settings_path(app_dir):
    """app_settings.json 的位置（软件目录下，随安装目录走）。"""
    return os.path.join(app_dir or '', SETTINGS_NAME)


def read_app_settings(app_dir):
    """读 app_settings.json，永远返回 dict（不存在/损坏/不是对象都给空 dict）。"""
    try:
        p = settings_path(app_dir)
        if not p or not os.path.isfile(p):
            return {}
        with open(p, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:              # noqa: BLE001
        return {}


def resolve_base_url(app_settings=None):
    """实际用的服务器地址：优先 app_settings.json 里的 update_base_url。

    非法/空值回退内置默认（DEFAULT_BASE_URL），保证老客户机行为不变。
    """
    if isinstance(app_settings, dict):
        u = normalize_base_url(app_settings.get('update_base_url'))
        if u:
            return u
    return DEFAULT_BASE_URL


def load_base_url(app_dir):
    """直接从软件目录读一次配置，给界面起手用（等价 read + resolve）。"""
    return resolve_base_url(read_app_settings(app_dir))


# ── 自测入口 ────────────────────────────────────────────────────────────────

def _selftest_verify():
    """对内置公钥做一次自检：签名算法与公钥匹配。"""
    import tempfile
    keydir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), '_releases', 'keys', 'update_pubkey.json')
    if not os.path.isfile(keydir):
        return 'skip'
    with open(keydir, 'r', encoding='utf-8') as f:
        pub = json.load(f)
    return 'ok' if pub['n_hex'] == PUBKEY_N_HEX and pub['e'] == PUBKEY_E \
        else 'mismatch'


if __name__ == '__main__':
    base = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE_URL
    cur = sys.argv[2] if len(sys.argv) > 2 else 'v0.0.0'
    print('服务器  : %s' % base)
    print('当前版本: %s' % cur)
    print('公钥自检: %s' % _selftest_verify())
    try:
        plan = plan_update(base, cur)
    except UpdateError as e:
        print('检查失败: %s' % e)
        sys.exit(1)
    if not plan:
        print('已是最新版本')
        sys.exit(0)
    print('可升级到: %s（共 %d 步，%d 字节）'
          % (plan['latest'], len(plan['steps']), plan['total_size']))
    for s in plan['steps']:
        print('  %s -> %s  %s' % (s['from'], s['to'], s['file']))
