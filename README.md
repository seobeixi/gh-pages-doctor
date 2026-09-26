# gh-pages-doctor

一条命令查清 GitHub Pages 自定义域名的 HTTPS 到底卡在哪。

```bash
python doctor.py example.com --repo owner/repo --token ghp_xxx
```

---

## 解决什么问题

GitHub Pages 绑定自定义域名后，最常见的问题是：**网站能打开，但 HTTPS 一直不可用**，浏览器报证书错误，仓库设置里的「Enforce HTTPS」是灰的。

这时候你去搜教程，得到的答案几乎都是同一句：

> 用 `openssl` 看证书，看到 `*.github.io` 就说明还在签发中，等等就好。

**这个判断方式有个致命缺陷**：`*.github.io` 只能说明「证书还没签发」，**区分不了下面两种情况**：

| 真实状态 | openssl 看到 | 该怎么办 |
|---|---|---|
| 正常签发中 | `*.github.io` | 等 5-30 分钟 |
| **彻底卡死了** | `*.github.io` | **等下去永远不会好** |

于是你就在那儿干等 —— 等 1 小时、1 天、甚至更久。GitHub 社区里有人**卡了 36 小时、56 小时**，最后全部换平台了事。

**本工具查的是 GitHub Pages API 的 `https_certificate` 字段**，能明确告诉你：

```
null      → GitHub 压根没触发证书申请（需重置域名）
new       → 已排队，正常等待
approved  → 签发完成 ✅
```

---

## 用法

只依赖 Python 3.7+ 标准库，**无需 pip install**。

### 基本检查

```bash
python doctor.py example.com
```

检查 DNS 解析、www CNAME、CAA 记录、实际 TLS 证书。

### 完整诊断（强烈建议）

```bash
python doctor.py example.com --repo owner/repo --token ghp_xxx
```

加上 `--repo` 和 `--token` 才能查 `https_certificate` 字段 —— **这是判断「卡住」的唯一依据**。

token 生成：https://github.com/settings/tokens ，勾选 `repo` 权限即可。

### 证书卡住时重置

```bash
python doctor.py example.com --repo owner/repo --token ghp_xxx --reset
```

执行「移除域名 → 等 30 秒 → 重新添加」，触发全新的 ACME 挑战。

### 签发完成后开启强制 HTTPS

```bash
python doctor.py example.com --repo owner/repo --token ghp_xxx --enforce-https
```

### JSON 输出（便于脚本处理）

```bash
python doctor.py example.com --json > report.json
```

---

## 输出示例

一次真实的诊断（域名证书卡在 `null`）：

```
1. DNS 解析
  ────────────────────────────────────────────────────────
  [--]   AliDNS      → 185.199.108.153, 185.199.109.153, 185.199.110.153, 185.199.111.153
  [--]   DNSPod      → 185.199.108.153, 185.199.109.153, 185.199.110.153, 185.199.111.153
  [OK]   已指向 GitHub Pages 官方 IP（4/4 条匹配）
  [OK]   各公共 DNS 解析结果一致（2 家交叉验证）

  [--]   www CNAME → yourname.github.io
  [OK]   www 已正确指向 github.io

2. CAA 记录（证书签发许可）
  ────────────────────────────────────────────────────────
  [OK]   无 CAA 记录 —— Let's Encrypt 可自由签发

3. TLS 证书
  ────────────────────────────────────────────────────────
  [--]   Subject: *.github.io
  [--]   Issuer : Let's Encrypt, YR1
  [~~]   当前是 GitHub 泛域名兜底证书 —— 说明你的域名证书还没签发
  [--]   关键问题：到底是「正在签」还是「卡死了」？看下一节

4. GitHub Pages 证书状态（关键）
  ────────────────────────────────────────────────────────
  [--]   Pages 状态   : built
  [--]   绑定域名     : example.com
  [--]   强制 HTTPS   : False

  [!!]   https_certificate = null
  [--]   → GitHub 尚未触发证书申请

  这是「卡住」的典型特征，不是「正在签发」。
  DNS 若已正确，等下去也不会自己好。
  解法：移除域名再重新添加，触发新的 ACME 挑战。

体检结论
  ────────────────────────────────────────────────────────
  发现 1 个问题：

    1. 证书卡在 null（GitHub 未触发申请）
```

---

## 检查项说明

| 检查 | 判断依据 | 常见问题 |
|---|---|---|
| **DNS 解析** | 根域名 4 条 A 记录 + www CNAME | 记录类型填错、少填几条、带了仓库名 |
| **CAA 记录** | 是否授权 Let's Encrypt | 有 CAA 但没放行 `letsencrypt.org` → 证书永远签不出来 |
| **TLS 证书** | 实际握手拿到的证书 | 是 `*.github.io` 说明还没签发 |
| **Pages API** | `https_certificate` 字段 | `null` = 卡住，`new` = 等待中 |

### 关于多 DNS 交叉验证

工具默认查 **AliDNS（阿里）和 DNSPod（腾讯）** —— 这两家在**国内网络下可达**。

Cloudflare 和 Google 的 DoH 端点在国内经常被阻断，所以没有默认启用（可用 `include_fallback` 参数手动开启）。

---

## 正确配置速查

**根域名 —— 4 条 A 记录**

```
185.199.108.153
185.199.109.153
185.199.110.153
185.199.111.153
```

**www 子域名 —— 1 条 CNAME**

```
yourname.github.io
```

⚠️ CNAME 的值**只填 `yourname.github.io`**，后面**不要**跟仓库名或路径。这是最常见的错误。

⚠️ 顺序不能反：**先在 GitHub 里填域名，再去 DNS 加记录**。反过来可能被人抢注子域名。

---

## 证书卡住了怎么办

如果 `https_certificate` 持续为 `null` 超过 1 小时：

**第一步：重置域名**

```bash
python doctor.py example.com --repo owner/repo --token ghp_xxx --reset
```

等 5-10 分钟再检查一次。

**第二步：如果还是 null，别等了**

社区案例显示，卡住的证书**没有一例是 GitHub 自己修好的**，最终都换了平台。两个选择：

**方案 A：套 Cloudflare 免费 CDN**（不换平台）
1. Cloudflare DNS 指向 GitHub Pages IP，开启代理（橙色云）
2. SSL/TLS 加密模式设为 `Flexible`
3. 开启 `Always Use HTTPS`
4. **GitHub 侧保持 `Enforce HTTPS` 关闭**（否则会重定向循环）

**方案 B：换托管平台**（更彻底）
- Cloudflare Pages / Vercel / Netlify
- 静态站迁移成本极低，证书**几分钟**就签发

---

## 环境要求

- Python 3.7+
- 无需第三方依赖
- 支持 Windows / macOS / Linux

Windows 用户如果终端中文乱码，先执行：

```cmd
chcp 65001
```

---

## 已知限制

- `--reset` 和 `--enforce-https` 需要 token，且 token 需有 `repo` 权限
- 部分企业网络会拦截 DoH 请求，此时 DNS 检查会失败（可手动用 `nslookup` 验证）
- 证书状态判断依赖 GitHub API，API 本身故障时无法诊断

---

## License

MIT
