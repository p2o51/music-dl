# music-dl · QQ 音乐下载器 (Claude Agent Skill)

一个 [Claude Agent Skill](https://docs.claude.com/en/docs/claude-code/skills)，让 AI agent 能搜索并下载 QQ 音乐的歌曲，支持会员音质（FLAC 无损 / 320kbps / 128kbps / M4A）。也可以脱离 agent 当普通命令行工具直接用。

- **搜索**：中英文歌名、歌手名，走 `musics.fcg` 签名接口（`zzc` 签名），**无需登录即可搜索**。
- **下载**：按搜索结果、`songMid`、整张专辑或歌单 ID 下载；高音质需要 QQ 音乐会员权限。`--quality best` 每首取能拿到的最高音质。
- **元数据**：自动写入歌名、歌手、专辑、专辑艺人、音轨号 / 碟号、发行日期、流派、唱片公司、歌词，以及专辑原图级别的高清封面。
- **不留坏文件**：下载后检查字节数、时长，并完整解码校验（FLAC 比对自带 MD5，逐位无损），写完标签再回读一遍。任何一步不过关都不会生成正式文件。
- **登录**：手机 QQ 或微信**扫码登录**，Cookie 本地缓存；微信登录的凭证会自动续期。不走任何反代 / 破解服务。

## 安装

把这个目录放到 Claude 的 skills 目录下：

```bash
git clone https://github.com/p2o51/music-dl.git ~/.claude/skills/music-dl
python3 -m pip install requests mutagen Pillow qrcode
brew install ffmpeg   # 可选但推荐：下载后完整解码校验
```

`mutagen`（写标签）和 `Pillow`（检查封面、生成二维码图片）下载时必需；`qrcode` 只在 QQ 登录时用于终端显示。没装 ffmpeg 时跳过完整解码，其他检查照常。

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

# 搜专辑（只列出结果），再按 albumMid 下载整张，存成 专辑名/01 歌名.flac
python3 qq_dl.py album "伊能静 关不住"
python3 qq_dl.py album <albumMid> --quality best

# 下载整个歌单
python3 qq_dl.py playlist <歌单ID> --quality 320

# 重新扫码登录（会把二维码存到 /tmp/qq_login_qr.png）
python3 qq_dl.py --login                  # 沿用上次的方式，没登录过用手机 QQ
python3 qq_dl.py --login --login-type wx  # 微信扫码
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
| `best` | 无损优先，这首没有无损就退到 320，再没有就 128 |

不指定 `--quality` 时默认 `320`。会员无权限的歌会打印提示并跳过，可降级到 `128` 重试。指定的音质这首歌没有时也会跳过，不会拿别的音质顶替。默认下载到 `~/Music/QQMusic`，用 `--output-dir` 改。

### 文件夹结构

`--layout flat`（`search` / `song` / `playlist` 的默认）存成 `歌手 - 歌名.flac`；`--layout album`（`album` 的默认）存成 `专辑名/01 歌名.flac`，多碟专辑是 `1-01 歌名.flac`。

## 下载结果

每首歌成功后会打印：

```
  🏷️  叶惠美 · 第 3/11 轨 · 2003-07-31 · Pop · 歌词 ✓ · 封面 1477×1477 JPEG（专辑原图）
  🔍 校验通过：大小与服务器一致 · 时长 4:29 · 解码后与 FLAC 自带 MD5 一致（逐位无损） · 标签和封面回读一致
  ✅ 已保存: ~/Music/QQMusic/周杰伦 - 晴天.flac
```

- 多碟专辑按"第几张碟的第几轨"标注（QQ 原始数据是整张专辑连续编号）。
- MP3 存成 ID3v2.3（UTF-16），并删掉容易乱码的 ID3v1；M4A 保留原有的无缝播放信息。
- 多位歌手除了写成「A, B」给人看，还会写一个多值的 ARTISTS 字段，Navidrome 等服务器靠它把每位歌手分开（只看「A, B」会当成一位叫「A, B」的歌手）。
- 封面优先用专辑原图，不合格（解不开、太小、不是方图、超过 3000 px 或 8 MB）就退到 1200 / 800 版。没有专辑图时用歌手图。

## 登录说明

`--login` 会拉取二维码存到 `/tmp/qq_login_qr.png`（已放大并加白边，方便在手机上长按识别），然后同步等待扫码：手机 QQ 最长 120 秒，微信最长 180 秒。确认后 Cookie 写入 `~/.qqmusic_cookie.json`（权限 600），之后调用自动复用。

QQ 账号和微信账号在 QQ 音乐里是两个不同的账号，会员开在哪个就用哪个登录。微信登录的凭证只有 3 天有效期，脚本启动时会用 refresh token 自动续期；续期也失败了才需要重新扫码。

登录失效时，取链接接口的表现和"没有会员"一样（所有音质都拿不到链接）。脚本会额外查一次会员接口区分两种情况，失效时提示 `登录已失效（不是会员问题）`。

搜索本身不需要登录，只有下载受版权/会员限制的歌才需要。

## 已知限制

签名算法是逆向 QQ 音乐前端得到的。QQ 音乐后端一旦更换签名算法或接口，搜索可能整体失效（表现为持续“没有找到歌曲”，与 Cookie 无关）。旧的 `client_search_cp` 兜底接口已被官方下线。

QQ 登录没有自动续期。写 APE 标签的代码没有用真实文件测过（曲库里很少有 APE 音源），FLAC / MP3 / M4A 都实测过。

## 免责声明

本项目仅供个人学习与技术研究，请遵守 QQ 音乐服务条款，尊重版权，**请勿用于商业或分发用途**。使用本工具产生的一切后果由使用者自行承担。

## License

[MIT](LICENSE)
