#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gh-pages-doctor —— GitHub Pages 自定义域名体检工具

一条命令查清：DNS 对不对、证书卡在哪、该不该重置。

GitHub Pages 绑定自定义域名后，最常见的问题是「HTTPS 一直不可用」。
全网教程都教你用 openssl 看证书，看到 *.github.io 就以为「还在签发中」——
但这个判断方式区分不了「正在签」和「彻底卡死」。

本工具查的是 GitHub API 的 https_certificate 字段，能明确告诉你：
  null      → GitHub 压根没触发证书申请（需重置域名）
  new       → 已排队，正常等待
  approved  → 签发完成

用法：
    python doctor.py example.com
    python doctor.py example.com --repo owner/repo --token ghp_xxx
    python doctor.py example.com --json

只依赖 Python 标准库，无需 pip install。
"""

import argparse
import json
import socket
import ssl
import sys
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

__version__ = '1.0.0'

# GitHub Pages 官方 IP（2026 年现行）
PAGES_IPV4 = [
    '185.199.108.153',
    '185.199.109.153',
    '185.199.110.153',
    '185.199.111.153',
]

# 用于交叉验证的公共 DNS（DoH 端点）
# 注意：国内网络下 Cloudflare / Google 的 DoH 常被阻断，
# 所以默认只用国内可达的两家，另两家作为备选。
DOH_SERVERS = {
    'AliDNS': 'https://dns.alidns.com/resolve',
    'DNSPod': 'https://doh.pub/dns-query',
}

DOH_SERVERS_FALLBACK = {
    'Cloudflare': 'https://cloudflare-dns.com/dns-query',
    'Google': 'https://dns.google/resolve',
}

# Let's Encrypt 允许的 CAA 签发机构
LE_ISSUERS = ('letsencrypt.org', 'pki.goog', 'digicert.com')


# ──────────────────────────── 输出 ────────────────────────────

class C:
    """终端颜色（不支持时自动降级）"""
    _on = sys.stdout.isatty() and sys.platform != 'win32'
    RESET = '\033[0m' if _on else ''
    BOLD = '\033[1m' if _on else ''
    DIM = '\033[2m' if _on else ''
    RED = '\033[31m' if _on else ''
    GREEN = '\033[32m' if _on else ''
    YELLOW = '\033[33m' if _on else ''
    BLUE = '\033[34m' if _on else ''
    CYAN = '\033[36m' if _on else ''


def ok(msg):
    print('  %s[OK]%s   %s' % (C.GREEN, C.RESET, msg))


def bad(msg):
    print('  %s[!!]%s   %s' % (C.RED, C.RESET, msg))


def warn(msg):
    print('  %s[~~]%s   %s' % (C.YELLOW, C.RESET, msg))


def info(msg):
    print('  %s[--]%s   %s' % (C.DIM, C.RESET, msg))


def title(msg):
    print('\n%s%s%s' % (C.BOLD, msg, C.RESET))
    print('  ' + '─' * 56)


# ──────────────────────────── DNS 查询 ────────────────────────────

def doh_query(server_url, name, rtype, timeout=8):
    """通过 DNS over HTTPS 查询，返回 Answer 列表"""
    params = 'name=%s&type=%s' % (name, rtype)
    url = '%s?%s' % (server_url, params)
    req = urllib.request.Request(url, headers={
        'accept': 'application/dns-json',
        'user-agent': 'gh-pages-doctor/%s' % __version__,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return data.get('Answer', [])
    except Exception:
        return None


def resolve_all(name, rtype, include_fallback=False):
    """
    多 DNS 交叉查询，返回 {dns名: [值]}

    默认只用国内可达的 DoH（阿里 + 腾讯）。
    include_fallback=True 时额外查 Cloudflare/Google（国内常被墙）。
    """
    servers = dict(DOH_SERVERS)
    if include_fallback:
        servers.update(DOH_SERVERS_FALLBACK)

    results = {}
    with ThreadPoolExecutor(max_workers=len(servers)) as ex:
        futures = {
            ex.submit(doh_query, url, name, rtype): label
            for label, url in servers.items()
        }
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                results[label] = fut.result()
            except Exception:
                results[label] = None
    return results


def extract_values(answers, wanted_type=None):
    """从 Answer 中提取数据值"""
    if not answers:
        return []
    out = []
    for a in answers:
        if wanted_type is not None and a.get('type') != wanted_type:
            continue
        d = a.get('data')
        if d:
            out.append(str(d).rstrip('.'))
    return out


def check_dns(domain):
    """检查根域名 A 记录 + www CNAME"""
    title('1. DNS 解析')

    result = {'apex_a': [], 'www_target': [], 'consistent': False,
              'is_github_io': domain.endswith('.github.io')}
    www = 'www.%s' % domain

    # GitHub 默认域名走自己的 CDN，不比对 Pages 专用 IP
    if result['is_github_io']:
        info('%s 是 GitHub 默认域名，跳过 A 记录比对' % domain)
        result['apex_a'] = ['(github.io)']
        result['consistent'] = True
        return result

    # 根域名 A 记录
    apex_res = resolve_all(domain, 'A')
    apex_sets = {}
    for label, answers in apex_res.items():
        vals = sorted(extract_values(answers, wanted_type=1))
        apex_sets[label] = vals

    reachable = {k: v for k, v in apex_sets.items() if v}
    if not reachable:
        bad('根域名 %s 查不到 A 记录 —— 域名可能未解析或已过期' % domain)
        return result

    for label, vals in apex_sets.items():
        if vals:
            info('%-11s → %s' % (label, ', '.join(vals)))
        else:
            warn('%-11s → 查询失败' % label)

    # 是否指向 GitHub Pages
    all_ips = set()
    for vals in reachable.values():
        all_ips.update(vals)

    gh_ips = set(PAGES_IPV4)
    hit = all_ips & gh_ips
    if hit:
        ok('已指向 GitHub Pages 官方 IP（%d/%d 条匹配）' % (len(hit), len(gh_ips)))
        result['apex_a'] = sorted(all_ips)
        if len(hit) < len(gh_ips):
            missing = gh_ips - hit
            warn('缺少 %d 条 A 记录：%s' % (len(missing), ', '.join(sorted(missing))))
            info('建议补齐 4 条，任一 IP 故障时能自动切换')
    else:
        bad('未指向 GitHub Pages 官方 IP')
        info('当前: %s' % ', '.join(sorted(all_ips)))
        info('应为: %s' % ', '.join(PAGES_IPV4))

    # 各 DNS 结果是否一致
    unique = {tuple(v) for v in reachable.values()}
    if len(unique) == 1:
        ok('各公共 DNS 解析结果一致（%d 家交叉验证）' % len(reachable))
        result['consistent'] = True
    else:
        warn('各 DNS 解析结果不一致 —— 可能还在传播中，稍等再试')

    # www CNAME
    print()
    www_res = resolve_all(www, 'CNAME')
    cnames = set()
    for label, answers in www_res.items():
        vals = extract_values(answers, wanted_type=5)
        if vals:
            cnames.update(vals)

    if cnames:
        for cn in sorted(cnames):
            info('www CNAME → %s' % cn)
        if any('github.io' in c for c in cnames):
            ok('www 已正确指向 github.io')
        else:
            bad('www 的 CNAME 未指向 github.io')
            info('应为 <用户名>.github.io（注意：不要带仓库名）')
        result['www_target'] = sorted(cnames)
    else:
        # 可能直接用 A 记录
        www_a = resolve_all(www, 'A')
        www_ips = set()
        for answers in www_a.values():
            www_ips.update(extract_values(answers, wanted_type=1))
        if www_ips:
            if www_ips & gh_ips:
                ok('www 用 A 记录指向 GitHub Pages')
            else:
                warn('www 有解析但不是 GitHub Pages IP: %s' % ', '.join(sorted(www_ips)))
            result['www_target'] = sorted(www_ips)
        else:
            warn('www 子域名未配置（非必须，但建议配上）')

    return result


def check_caa(domain):
    """检查 CAA 记录是否拦截 Let's Encrypt"""
    title('2. CAA 记录（证书签发许可）')

    result = {'has_caa': False, 'blocking': False, 'records': []}

    all_records = set()
    for server_url in DOH_SERVERS.values():
        answers = doh_query(server_url, domain, 'CAA')
        if answers:
            for a in answers:
                d = a.get('data', '')
                if d:
                    all_records.add(str(d))

    if not all_records:
        ok('无 CAA 记录 —— Let\'s Encrypt 可自由签发')
        return result

    result['has_caa'] = True
    result['records'] = sorted(all_records)
    for r in sorted(all_records):
        info(r)

    low = ' '.join(all_records).lower()
    allows = any(iss in low for iss in LE_ISSUERS)
    if allows:
        ok('CAA 允许主流 CA 签发')
    else:
        bad('CAA 记录未授权 Let\'s Encrypt —— 这会导致证书永远签不出来')
        result['blocking'] = True
        info('解法：在 DNS 加一条 CAA 记录')
        info('  类型 CAA  值: 0 issue "letsencrypt.org"')

    return result


def _parse_der_cert(der_bytes):
    """
    解析 DER 格式证书（当 CERT_NONE 拿不到 dict 时的兜底）

    注意：CERT_NONE 模式下 getpeercert() 返回空字典，
    必须先用 binary_form=True 拿 DER 字节，再转成 PEM 让 ssl 模块解码。
    """
    import os
    import tempfile
    try:
        pem = ssl.DER_cert_to_PEM_cert(der_bytes)
        fd, path = tempfile.mkstemp(suffix='.pem')
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(pem)
            return ssl._ssl._test_decode_cert(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    except Exception:
        return None


def check_tls(domain, timeout=10):
    """检查实际 TLS 证书"""
    title('3. TLS 证书')

    result = {'subject': None, 'issuer': None, 'is_github_fallback': False,
              'is_correct': False, 'error': None}

    # 直连 GitHub Pages IP，避免本地 DNS 干扰
    last_err = None
    cert = None
    for ip in PAGES_IPV4:
        try:
            # 必须用 CERT_OPTIONAL：CERT_NONE 时 getpeercert() 返回空字典
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_OPTIONAL
            with socket.create_connection((ip, 443), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                    cert = ssock.getpeercert()
                    if cert:
                        break
        except ssl.SSLCertVerificationError:
            # 证书验证失败也要拿到证书内容（这正是我们要诊断的）
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with socket.create_connection((ip, 443), timeout=timeout) as sock:
                    with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                        der = ssock.getpeercert(binary_form=True)
                        if der:
                            cert = ssl.DER_cert_to_PEM_cert(der)
                            cert = _parse_der_cert(der)
                            break
            except Exception as e:
                last_err = e
        except Exception as e:
            last_err = e

    if not cert:
        bad('无法建立 TLS 连接：%s' % last_err)
        result['error'] = str(last_err)
        return result

    # 解析 subject / issuer
    subj_parts = []
    for rdn in cert.get('subject', ()):
        for k, v in rdn:
            if k == 'commonName':
                subj_parts.append(v)
    issuer_parts = []
    for rdn in cert.get('issuer', ()):
        for k, v in rdn:
            if k in ('commonName', 'organizationName'):
                issuer_parts.append(v)

    subject = ', '.join(subj_parts) or '(未知)'
    issuer = ', '.join(issuer_parts) or '(未知)'
    result['subject'] = subject
    result['issuer'] = issuer

    info('Subject: %s' % subject)
    info('Issuer : %s' % issuer)

    if '*.github.io' in subject:
        if domain.endswith('.github.io'):
            # GitHub 默认域名，用泛域名证书是正常的
            ok('这是 GitHub 默认域名，使用 *.github.io 证书属正常')
            result['is_correct'] = True
        else:
            warn('当前是 GitHub 泛域名兜底证书 —— 说明你的域名证书还没签发')
            result['is_github_fallback'] = True
            info('浏览器访问会报「证书不匹配」，这是正常现象')
            info('关键问题：到底是「正在签」还是「卡死了」？看下一节')
    elif domain in subject:
        ok('证书已正确签发给 %s' % domain)
        result['is_correct'] = True
        if cert.get('notAfter'):
            info('有效期至: %s' % cert['notAfter'])
    else:
        warn('证书 subject 不含你的域名：%s' % subject)

    return result


def check_http(domain, timeout=10):
    """检查 HTTP 是否可访问"""
    result = {'reachable': False, 'status': None}
    for ip in PAGES_IPV4:
        try:
            req = urllib.request.Request(
                'http://%s/' % domain,
                headers={'Host': domain, 'user-agent': 'gh-pages-doctor'}
            )
            # 用 IP 直连，手动带 Host 头
            opener = urllib.request.build_opener()
            with opener.open(req, timeout=timeout) as resp:
                result['status'] = resp.status
                result['reachable'] = True
                break
        except urllib.error.HTTPError as e:
            result['status'] = e.code
            result['reachable'] = True
            break
        except Exception:
            continue
    return result


def check_pages_api(repo, token, timeout=15):
    """查 GitHub Pages API 的 https_certificate 字段 —— 核心判断依据"""
    title('4. GitHub Pages 证书状态（关键）')

    result = {'queried': False, 'cert_state': None, 'cert_desc': None,
              'cname': None, 'status': None, 'https_enforced': None,
              'error': None, 'verdict': None}

    if not repo or not token:
        warn('未提供 --repo 和 --token，跳过（这是最关键的检查，强烈建议提供）')
        info('用法: python doctor.py 域名 --repo 用户名/仓库名 --token <token>')
        info('token 生成: https://github.com/settings/tokens （勾 repo 权限）')
        return result

    url = 'https://api.github.com/repos/%s/pages' % repo
    req = urllib.request.Request(url, headers={
        'Authorization': 'token %s' % token,
        'Accept': 'application/vnd.github+json',
        'user-agent': 'gh-pages-doctor/%s' % __version__,
    })

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            bad('仓库 %s 未开启 Pages，或仓库不存在' % repo)
        elif e.code == 401:
            bad('token 无效或权限不足')
        else:
            bad('API 请求失败: HTTP %d' % e.code)
        result['error'] = 'HTTP %d' % e.code
        return result
    except Exception as e:
        bad('API 请求异常: %s' % e)
        result['error'] = str(e)
        return result

    result['queried'] = True
    result['cname'] = data.get('cname')
    result['status'] = data.get('status')
    result['https_enforced'] = data.get('https_enforced')

    info('Pages 状态   : %s' % data.get('status'))
    info('绑定域名     : %s' % (data.get('cname') or '(未绑定)'))
    info('强制 HTTPS   : %s' % data.get('https_enforced'))

    cert = data.get('https_certificate')
    print()

    if cert is None:
        bad('https_certificate = null')
        info('→ GitHub 尚未触发证书申请')
        result['cert_state'] = 'null'
        result['verdict'] = 'stuck'
        print()
        print('  %s这是「卡住」的典型特征，不是「正在签发」。%s' % (C.BOLD, C.RESET))
        print('  DNS 若已正确，等下去也不会自己好。')
        print('  解法：移除域名再重新添加，触发新的 ACME 挑战。')
        print('  %s  python doctor.py %s --repo %s --token <token> --reset%s'
              % (C.CYAN, '你的域名', repo, C.RESET))
    else:
        state = cert.get('state')
        desc = cert.get('description')
        domains = cert.get('domains')
        result['cert_state'] = state
        result['cert_desc'] = desc

        info('cert state   : %s' % state)
        info('cert desc    : %s' % desc)
        if domains:
            info('cert domains : %s' % ', '.join(domains))
        print()

        if state in ('approved', 'issued'):
            ok('证书已签发！')
            result['verdict'] = 'ready'
            if not result['https_enforced']:
                info('建议开启强制 HTTPS（或让本工具代劳）')
                print('  %s  python doctor.py %s --repo %s --token <token> --enforce-https%s'
                      % (C.CYAN, '你的域名', repo, C.RESET))
        elif state == 'new':
            warn('已排队，等待处理中 —— 属于正常流程')
            info('通常几分钟到 1 小时。超过 1 小时无变化可考虑重置')
            result['verdict'] = 'pending'
        elif state in ('authorization_created', 'authorization_pending'):
            warn('ACME 验证进行中 —— 正常流程，耐心等')
            result['verdict'] = 'pending'
        elif state in ('unauthorized', 'bad_authz', 'destroyed'):
            bad('证书申请被拒绝（%s）' % state)
            info('常见原因：DNS 未生效、CAA 记录拦截、域名所有权验证失败')
            result['verdict'] = 'failed'
        else:
            warn('未知状态：%s' % state)
            result['verdict'] = 'unknown'

    return result


def reset_domain(repo, token, domain, timeout=25):
    """移除域名再重新添加，触发新的证书申请"""
    title('执行重置（移除域名 → 重新添加）')

    base = 'https://api.github.com/repos/%s/pages' % repo
    headers = {
        'Authorization': 'token %s' % token,
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
        'user-agent': 'gh-pages-doctor/%s' % __version__,
    }

    def put(payload):
        req = urllib.request.Request(
            base, data=json.dumps(payload).encode('utf-8'),
            headers=headers, method='PUT'
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            return {'_error': 'HTTP %d: %s' % (e.code, e.read().decode('utf-8', 'ignore')[:200])}
        except Exception as e:
            return {'_error': str(e)}

    info('步骤 1/2：移除自定义域名...')
    r1 = put({'cname': None, 'source': {'branch': 'main', 'path': '/'}})
    if r1.get('_error'):
        bad('移除失败: %s' % r1['_error'])
        return False
    info('已移除（cname = %s）' % r1.get('cname'))

    import time
    info('等待 30 秒，让 GitHub 侧状态刷新...')
    time.sleep(30)

    info('步骤 2/2：重新添加域名 %s ...' % domain)
    r2 = put({'cname': domain, 'source': {'branch': 'main', 'path': '/'}})
    if r2.get('_error'):
        bad('添加失败: %s' % r2['_error'])
        return False

    ok('已重新绑定（cname = %s）' % r2.get('cname'))
    print()
    print('  %s重置完成。GitHub 会重新发起证书申请。%s' % (C.BOLD, C.RESET))
    print('  等 5-10 分钟后重新运行本工具，看 cert state 有没有变成 new/approved。')
    print('  %s若仍为 null，说明是 GitHub 侧的问题，建议换托管平台或套 Cloudflare CDN。%s'
          % (C.DIM, C.RESET))
    return True


def enforce_https(repo, token, timeout=25):
    """开启强制 HTTPS"""
    title('开启强制 HTTPS')

    url = 'https://api.github.com/repos/%s/pages' % repo
    headers = {
        'Authorization': 'token %s' % token,
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
        'user-agent': 'gh-pages-doctor/%s' % __version__,
    }
    req = urllib.request.Request(
        url, data=json.dumps({'https_enforced': True}).encode('utf-8'),
        headers=headers, method='PUT'
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        if data.get('https_enforced'):
            ok('强制 HTTPS 已开启')
            return True
        warn('请求成功但 https_enforced 仍为 false')
        return False
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'ignore')[:300]
        if 'certificate does not exist' in body:
            bad('证书还没签发，现在开不了')
            info('先用 --reset 重置域名，或等证书签发后再试')
        else:
            bad('失败: HTTP %d %s' % (e.code, body))
        return False
    except Exception as e:
        bad('异常: %s' % e)
        return False


def print_summary(domain, dns_r, caa_r, tls_r, api_r):
    """输出最终结论"""
    title('体检结论')

    problems = []
    notes = []

    # DNS
    apex = dns_r.get('apex_a') or []
    if dns_r.get('is_github_io'):
        pass  # GitHub 默认域名，不需要 DNS 检查
    elif not apex:
        problems.append('DNS 未解析到 GitHub Pages')
    elif not set(apex) & set(PAGES_IPV4):
        problems.append('A 记录未指向 GitHub Pages 官方 IP')
    elif len(set(apex) & set(PAGES_IPV4)) < 4:
        notes.append('A 记录只有 %d 条，建议补齐 4 条' % len(set(apex) & set(PAGES_IPV4)))

    # CAA
    if caa_r.get('blocking'):
        problems.append('CAA 记录拦截了 Let\'s Encrypt')

    # TLS 连接本身失败
    if tls_r.get('error'):
        problems.append('TLS 连接失败：%s' % tls_r['error'])

    # 证书状态
    verdict = api_r.get('verdict')
    if verdict == 'stuck':
        problems.append('证书卡在 null（GitHub 未触发申请）')
    elif verdict == 'failed':
        problems.append('证书申请被拒绝')
    elif verdict == 'pending':
        notes.append('证书签发中（正常，耐心等）')
    elif verdict is None:
        # 没查 API，只能靠 TLS 判断
        if tls_r.get('is_github_fallback'):
            problems.append('证书未签发（当前是 GitHub 兜底证书）')
            notes.append('未提供 token，无法判断是「正在签」还是「卡住了」')
            notes.append('补上 --repo 和 --token 可精确诊断')
        elif tls_r.get('error'):
            pass  # 已在上面记过
        elif not tls_r.get('is_correct'):
            problems.append('证书状态异常')

    # 输出
    if problems:
        print('  发现 %d 个问题：\n' % len(problems))
        for i, p in enumerate(problems, 1):
            print('    %d. %s' % (i, p))
        print()
    else:
        ok('没发现明显问题')

    if notes:
        print('  说明：')
        for n in notes:
            print('    · %s' % n)
        print()

    if not problems:
        if tls_r.get('is_correct'):
            print('  你的域名已正确配置，HTTPS 可用。')
        return

    print('  %s建议的下一步：%s' % (C.BOLD, C.RESET))
    if caa_r.get('blocking'):
        print('    · 先去 DNS 加 CAA 记录放行 letsencrypt.org')
    if verdict == 'stuck':
        print('    · 证书卡住了，执行重置：')
        print('      %spython doctor.py %s --repo <owner/repo> --token <token> --reset%s'
              % (C.CYAN, domain, C.RESET))
        print('    · 若重置后仍为 null，是 GitHub 侧问题：')
        print('      换 Cloudflare Pages / Vercel / Netlify，或套 Cloudflare CDN')
    elif verdict == 'pending':
        print('    · 正常签发中，等 5-30 分钟再跑一次')
    elif verdict is None and tls_r.get('is_github_fallback'):
        print('    · 补上 --repo 和 --token 重新检查，才能确定下一步')
    if not dns_r.get('is_github_io'):
        if not apex or not (set(apex) & set(PAGES_IPV4)):
            print('    · 检查 DNS：根域名需 4 条 A 记录指向 GitHub Pages IP')
            print('      %s' % ', '.join(PAGES_IPV4))


def main():
    ap = argparse.ArgumentParser(
        description='GitHub Pages 自定义域名体检工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例:
  python doctor.py example.com
  python doctor.py example.com --repo owner/repo --token ghp_xxx
  python doctor.py example.com --repo owner/repo --token ghp_xxx --reset
  python doctor.py example.com --json > report.json

token 生成: https://github.com/settings/tokens （勾选 repo 权限）
        '''
    )
    ap.add_argument('domain', help='要检查的域名，如 example.com')
    ap.add_argument('--repo', help='GitHub 仓库，格式 owner/repo（可选但强烈建议）')
    ap.add_argument('--token', help='GitHub Personal Access Token（可选但强烈建议）')
    ap.add_argument('--reset', action='store_true',
                    help='证书卡住时重置域名（移除后重新添加）')
    ap.add_argument('--enforce-https', action='store_true',
                    help='开启强制 HTTPS')
    ap.add_argument('--json', action='store_true', help='输出 JSON 格式')
    ap.add_argument('--version', action='version', version=__version__)

    args = ap.parse_args()
    domain = args.domain.strip().lower()
    domain = domain.replace('https://', '').replace('http://', '').strip('/')

    if args.json:
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dns_r = check_dns(domain)
            caa_r = check_caa(domain)
            tls_r = check_tls(domain)
            api_r = check_pages_api(args.repo, args.token)
        print(json.dumps({
            'domain': domain,
            'version': __version__,
            'checked_at': datetime.now().isoformat(),
            'dns': dns_r, 'caa': caa_r, 'tls': tls_r, 'pages_api': api_r,
        }, ensure_ascii=False, indent=2))
        return

    print()
    print('%s  gh-pages-doctor v%s  %s' % (C.BOLD, __version__, C.RESET))
    print('  检查域名: %s%s%s' % (C.CYAN, domain, C.RESET))
    print('  时间: %s' % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    dns_r = check_dns(domain)
    caa_r = check_caa(domain)
    tls_r = check_tls(domain)
    api_r = check_pages_api(args.repo, args.token)

    if args.reset:
        if not args.repo or not args.token:
            bad('--reset 需要同时提供 --repo 和 --token')
            sys.exit(2)
        reset_domain(args.repo, args.token, domain)
        return

    if args.enforce_https:
        if not args.repo or not args.token:
            bad('--enforce-https 需要同时提供 --repo 和 --token')
            sys.exit(2)
        enforce_https(args.repo, args.token)
        return

    print_summary(domain, dns_r, caa_r, tls_r, api_r)
    print()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n已中断')
        sys.exit(130)
