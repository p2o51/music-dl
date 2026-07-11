# music-dl · QQ 音乐下载器 (Claude Agent Skill)

一个 [Claude Agent Skill](https://docs.claude.com/en/docs/claude-code/skills)，让 AI agent 能搜索并下载 QQ 音乐的歌曲，支持会员音质（FLAC 无损 / 320kbps / 128kbps / M4A）。也可以脱离 agent 当普通命令行工具直接用。

- **搜索**：中英文歌名、歌手名，走 `musics.fcg` 签名接口（`zzc` 签名），**无需登录即可搜索**。
- **下载**：按搜索结果、`songMid` 或歌单 ID 下载；高音质需要 QQ 音乐会员权限。
- **登录**：手机 QQ **扫码登录**，Cookie 本地缓存，不走任何反代 / 破解服务。

## 安装

把这个目录放到 Claude 的 skills 目录下：

```bash
git clone https://github.com/<your-name>/music-dl.git ~/.claude/skills/music-dl
python3 -m pip install requests qrcode Pillow
```

`qrcode` / `Pillow` 只在扫码登录时用到；Cookie 有效时缺这两个包也不影响搜索下载。

## 作为 Claude Skill 使用

装好后，直接对 Claude 说“帮我下载周杰伦的晴天”之类的话即可；agent 会读取 `SKILL.md` 并按里面的说明调用脚本。`SKILL.md` 里写明了非交互调用规范、扫码登录如何把二维码呈现给用户、音质降级策略等。

## 作为命令行工具直接用

```bash
# 搜索并下载第一条结果（无损）
python3 qq_dl.py search "周杰伦 晴天" --first --quality flac

# 搜索并下载指定序号（1-based）
python3 qq_dl.py search "周杰伦" --index 3 --quality 320

# 已知 songMid，直接下载
python3 qq_dl.py song <songMid> --quality flac

# 下载整个歌单
python3 qq_dl.py playlist <歌单ID> --quality 320

# 重新扫码登录（会把二维码存到 /tmp/qq_login_qr.png）
python3 qq_dl.py --login
```

不带任何参数运行会进入交互式菜单（搜索 / 歌单 / 切音质 / 重登）。

### 音质

| 值 | 说明 |
|------|------|
| `128` | 标准 MP3 128kbps |
| `320` | HQ MP3 320kbps（需会员）|
| `flac` | FLAC 无损（需会员）|
| `ape` | APE 无损（需会员）|
| `m4a` | 默认 M4A |

不指定 `--quality` 时默认 `320`。会员无权限的歌会打印提示并跳过，可降级到 `128` 重试。默认下载到 `~/Music/QQMusic`，用 `--output-dir` 改。

## 登录说明

`--login` 会拉取二维码存到 `/tmp/qq_login_qr.png`，然后同步轮询等待扫码，最长 120 秒。用手机 QQ 扫码确认后，Cookie 写入 `~/.qqmusic_cookie.json`，之后调用自动复用。搜索本身不需要登录，只有下载受版权/会员限制的歌才需要。

## 已知限制

签名算法是逆向 QQ 音乐前端得到的。QQ 音乐后端一旦更换签名算法或接口，搜索可能整体失效（表现为持续“没有找到歌曲”，与 Cookie 无关）。旧的 `client_search_cp` 兜底接口已被官方下线。

## 免责声明

本项目仅供个人学习与技术研究，请遵守 QQ 音乐服务条款，尊重版权，**请勿用于商业或分发用途**。使用本工具产生的一切后果由使用者自行承担。

## License

[MIT](LICENSE)
