---
name: music-dl
description: 下载音乐 FLAC/MP3。当用户想下载歌曲时使用。支持中文/英文歌名、歌手名搜索。
---

# QQ 音乐下载器

用本 skill 目录下的 `qq_dl.py` 搜索并下载歌曲，支持会员音质（FLAC 无损 / 320kbps / 128kbps / M4A）。

> 下文命令里的脚本路径以默认安装位置 `~/.claude/skills/music-dl/qq_dl.py` 为例；如果你把 skill 装在别处，替换成实际路径即可。

登录方式是扫码登录（不是账号密码，也不走任何反代/破解服务），Cookie 缓存在本地，正常情况下不需要每次都重新登录。

## 非交互调用（默认这样用，禁止用会阻塞的调法）

**重要：不要不带参数直接运行脚本（`python3 qq_dl.py`），也不要用 `search`/`playlist` 但不带 `--first`/`--index`/歌单 ID 的裸调用去驱动交互式菜单——那些分支会调用 `input()` 等待终端输入，agent 在非交互环境里会直接卡死。**

### 搜索并下载（默认第一条结果）

```bash
python3 ~/.claude/skills/music-dl/qq_dl.py search "周杰伦 晴天" --first --quality flac
```

### 搜索并下载指定序号（先看搜索结果里的序号，1-based）

```bash
python3 ~/.claude/skills/music-dl/qq_dl.py search "周杰伦" --index 3 --quality 320
```

### 已知 songMid，直接下载（不搜索，天然非交互）

```bash
python3 ~/.claude/skills/music-dl/qq_dl.py song <songMid> --quality flac
```

`<songMid>` 是 QQ 音乐内部曲目 ID，一般不会提前知道，通常先用 `search` 拿到候选再下载，这个子命令主要用于已经从搜索结果里拿到 mid 的场景。

### 指定音质

`--quality` 可选：`128`（标准 MP3）/ `320`（HQ MP3，需会员）/ `flac`（无损，需会员）/ `m4a`（默认音质）/ `ape`（无损 APE）。不写默认 `320`。

高音质（`320`/`flac`）依赖账号的 QQ 音乐会员权限，如果会员已过期或歌曲无权限，脚本会打印 `⚠️ 无法获取链接（可能需要会员或歌曲不可用）` 且不下载。这种情况下降级重试一次更低音质：

```bash
python3 ~/.claude/skills/music-dl/qq_dl.py search "周杰伦 晴天" --first --quality 320
```

### 指定输出目录

```bash
python3 ~/.claude/skills/music-dl/qq_dl.py search "晴天" --first --quality flac --output-dir ~/Downloads
```

不指定时默认下载到 `~/Music/QQMusic`。文件名格式为 `歌手 - 歌曲名.扩展名`；如果目标文件已存在且非空，会跳过下载。

### 下载完成后

下载成功会打印 `✅ 已保存: <完整路径>`，把这个路径和文件大小回报给用户（可以用 `ls -la` 确认文件确实落盘、大小 > 1MB，避免把下载失败的空文件/半截文件当成功）。

## Cookie / 登录

- Cookie 缓存在 `~/.qqmusic_cookie.json`，脚本每次运行会自动读取。
- 脚本本身没有严格的"过期时间"字段可判断——`is_logged_in` 只检查 Cookie 里 `uin`/`qm_keyst` 是否存在，不代表一定还有效。真正是否有效，以实际调用（搜索/下载）是否成功为准。
- 如果 Cookie 不存在，或者搜索/下载持续失败（且确认不是"无权限/需要会员"那种情况），可能是登录过期，需要重新登录：

  ```bash
  python3 ~/.claude/skills/music-dl/qq_dl.py --login
  ```

  不带子命令的 `--login` 会在登录成功后直接退出（不会进入交互菜单）；也可以在任意子命令上加 `--login` 强制先重新登录再执行。

- **登录会触发扫码流程，这是唯一允许的阻塞点**：脚本会把二维码图片保存到 `/tmp/qq_login_qr.png`，然后开始轮询等待扫码，**最长等待 120 秒（每 2 秒轮询一次，60 次）**，超时会打印 `⏰ 扫码超时` 并退出。
- **agent 必须在发起登录后，立刻用发送文件/图片的方式把 `/tmp/qq_login_qr.png` 展示给用户**，让用户拿手机 QQ 扫码确认登录，然后等待脚本轮询返回结果（脚本这一步是同步阻塞等待，不需要 agent 反复调用，跑一次到它自己返回或超时即可）。
- **只有用户明确表示要登录、且用户当前在场能看到并扫描二维码时，才发起登录**。用户不在场时不要主动发起登录——二维码会在用户回来之前就过期。
- 登录成功后 Cookie 会自动写回 `~/.qqmusic_cookie.json`，之后的调用不再需要登录。

## 依赖

脚本依赖 `requests`、`qrcode`、`Pillow`（登录二维码生成需要）。缺失时先安装：

```bash
python3 -m pip install requests qrcode Pillow
```

（`qrcode`/`Pillow` 只在登录环节用到；如果 Cookie 已经有效，缺了这两个包也不影响搜索下载，脚本会跳过终端二维码渲染但仍把二维码图片存到 `/tmp/qq_login_qr.png`。）

## 已知限制

- 搜索走 QQ 音乐 `musics.fcg` 的签名接口（`music.search.SearchCgiService` + `zzc` 签名），主节点 `u.y.qq.com`，备用节点 `u6.y.qq.com`。搜索**不需要登录**即可用；下载才需要登录/会员权限。
- 签名算法是逆向 QQ 音乐前端得到的，QQ 音乐后端一旦更换签名算法或接口，搜索可能整体失效（表现为持续"没有找到歌曲"，与 Cookie 是否有效无关）。遇到这种情况优先怀疑是接口变更，而不是登录过期。旧的 `client_search_cp` 兜底接口已被官方下线，不再可用。
- 本工具仅供个人学习与技术研究，请遵守 QQ 音乐服务条款，尊重版权，勿用于商业或分发用途。
