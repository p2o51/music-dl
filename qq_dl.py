#!/usr/bin/env python3
from __future__ import annotations
"""
QQ音乐下载器 - 支持二维码扫码登录 + 会员音质
==========================================

使用方法:
  python qq_music_dl.py              # 启动 → 扫码登录 → 交互式搜索下载
  python qq_music_dl.py search 周杰伦  # 搜索并下载
  python qq_music_dl.py song 001ABC   # 直接下载指定 songMid
  python qq_music_dl.py playlist 123  # 下载整个歌单

依赖:
  pip install requests qrcode Pillow

音质选择 (有会员可用高品质):
  --quality 128    标准MP3 128kbps
  --quality 320    HQ MP3 320kbps (需会员)
  --quality flac   FLAC 无损 (需会员)
  --quality m4a    默认M4A格式

Cookie 保存位置: ~/.qqmusic_cookie.json
"""

import base64
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode

try:
    import requests
except ImportError:
    print("请先安装 requests: pip install requests")
    sys.exit(1)

try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False

# ─── 配置 ──────────────────────────────────────────────────────────────

COOKIE_FILE = Path.home() / ".qqmusic_cookie.json"
DOWNLOAD_DIR = Path.home() / "Music" / "QQMusic"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
REFERER = "https://y.qq.com/"
GUID = "10000000000000000000000000000000"  # comm.guid，搜索/vkey 均不校验其内容，固定即可

QUALITY_MAP = {
    "128":  {"prefix": "M500", "ext": ".mp3",  "label": "标准 128kbps"},
    "320":  {"prefix": "M800", "ext": ".mp3",  "label": "HQ 320kbps"},
    "flac": {"prefix": "F000", "ext": ".flac", "label": "无损 FLAC"},
    "ape":  {"prefix": "A000", "ext": ".ape",  "label": "无损 APE"},
    "m4a":  {"prefix": "C400", "ext": ".m4a",  "label": "标准 M4A"},
}

# ─── 签名算法 (纯 Python 实现) ──────────────────────────────────────────

# QQ 音乐 musics.fcg 当前签名算法 (zzc)：SHA1 → 索引重排 + 逐字节异或 + Base64
# 等价于 luren-dc/QQMusicApi、jsososo/QQMusicApi 的实现 (2026 版)。
# 旧版基于 MD5 + 固定 salt 的 zzb 算法已被服务端弃用，签名会被拒绝 (返回 code 2000)。
_SIGN_PART1_IDX = [23, 14, 6, 36, 16, 7, 19]
_SIGN_PART2_IDX = [16, 1, 32, 12, 19, 27, 8, 5]
_SIGN_SCRAMBLE = [89, 39, 179, 150, 218, 82, 58, 252, 177, 52,
                  186, 123, 120, 64, 242, 133, 143, 161, 121, 179]


def get_sign(payload) -> str:
    """
    计算 musics.fcg 的 zzc 签名。

    payload 必须是最终 POST 出去的请求体本身 (str 或 bytes)。服务端会对收到的
    原始 body 重新计算签名并逐字节比对，因此签名对象必须与实际发送的字节完全一致
    ——调用方应对同一份 utf-8 字节既签名又发送。
    """
    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    h = hashlib.sha1(raw).hexdigest().upper()  # 40 位大写十六进制
    part1 = "".join(h[i] for i in _SIGN_PART1_IDX)
    part2 = "".join(h[i] for i in _SIGN_PART2_IDX)
    scrambled = bytearray(20)
    for i, v in enumerate(_SIGN_SCRAMBLE):
        scrambled[i] = v ^ int(h[i * 2:i * 2 + 2], 16)
    b64 = re.sub(rb"[\\/+=]", b"", base64.b64encode(scrambled)).decode()
    return ("zzc" + part1 + b64 + part2).lower()


# ─── Cookie 管理 ────────────────────────────────────────────────────────

class CookieManager:
    def __init__(self):
        self.cookies: dict = {}
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Referer": REFERER,
            "Origin": "https://y.qq.com",
        })

    def load(self) -> bool:
        """从文件加载 Cookie"""
        if not COOKIE_FILE.exists():
            return False
        try:
            self.cookies = json.loads(COOKIE_FILE.read_text())
            self._apply_cookies()
            return bool(self.cookies)
        except Exception:
            return False

    def save(self):
        """保存 Cookie 到文件"""
        COOKIE_FILE.write_text(json.dumps(self.cookies, ensure_ascii=False, indent=2))

    def set(self, cookie_dict: dict):
        """设置 Cookie"""
        self.cookies.update(cookie_dict)
        self._apply_cookies()
        self.save()

    def _apply_cookies(self):
        """将 Cookie 应用到 session"""
        for k, v in self.cookies.items():
            self.session.cookies.set(k, str(v), domain=".qq.com")

    @property
    def uin(self) -> str:
        uin = self.cookies.get("uin", self.cookies.get("wxuin", "0"))
        return re.sub(r"\D", "", str(uin)) or "0"

    @property
    def music_key(self) -> str:
        return self.cookies.get("qm_keyst", self.cookies.get("qqmusic_key", ""))

    @property
    def p_skey(self) -> str:
        return self.cookies.get("p_skey", self.cookies.get("skey", ""))

    @property
    def gtk(self) -> int:
        """计算 g_tk (用于旧接口)"""
        skey = self.p_skey
        h = 5381
        for c in skey:
            h += (h << 5) + ord(c)
        return h & 0x7FFFFFFF

    @property
    def is_logged_in(self) -> bool:
        return bool(self.uin and self.uin != "0" and self.music_key)


# ─── 二维码登录 ─────────────────────────────────────────────────────────

class QQrLogin:
    """
    QQ 互联二维码登录
    流程: 获取二维码 → 展示给用户扫码 → 轮询确认 → 获取 Cookie → 跳转 y.qq.com 拿 music_key
    """

    PTLOGIN_APPID = 716027609  # QQ 音乐的 appid
    PTLOGIN_DAID = 383

    def __init__(self, cm: CookieManager):
        self.cm = cm
        self.sess = requests.Session()
        self.sess.headers.update({"User-Agent": UA})

    def _get_ptqr_token(self, qrsig: str) -> int:
        """计算 ptqrtoken"""
        e = 0
        for c in qrsig:
            e += (e << 5) + ord(c)
        return 2147483647 & e

    def step1_get_qrcode(self) -> tuple[str, str]:
        """获取登录二维码，返回 (qrsig, 二维码内容URL)"""
        url = (
            f"https://xui.ptlogin2.qq.com/cgi-bin/xlogin?"
            f"appid={self.PTLOGIN_APPID}&daid={self.PTLOGIN_DAID}"
            f"&style=33&theme=2&login_text=%E6%8E%88%E6%9D%83%E5%B9%B6%E7%99%BB%E5%BD%95"
            f"&hide_title_bar=1&low_login=0&qlogin_auto_login=1&no_verifyimg=1"
            f"&link_target=blank&fallback=1"
            f"&pt_3rd_aid=0&pt_feedback_link=&redirect_url="
            f"{quote('https://y.qq.com/portal/profile.html')}"
        )

        resp = self.sess.get(url)
        # 从页面提取 pt_login_sig 参数
        pt_login_sig = ""
        sig_match = re.search(r'pt_login_sig\s*=\s*["\']([^"\']+)["\']', resp.text)
        if sig_match:
            pt_login_sig = sig_match.group(1)

        # 请求二维码图片
        qr_url = (
            f"https://ssl.ptlogin2.qq.com/ptqrshow?"
            f"appid={self.PTLOGIN_APPID}&e=2&l=M&s=3&d=72&v=4"
            f"&t=0.{int(time.time()*1000)}"
            f"&daid={self.PTLOGIN_DAID}&pt_3rd_aid=0"
        )
        if pt_login_sig:
            qr_url += f"&pt_login_sig={pt_login_sig}"

        qr_resp = self.sess.get(qr_url)
        qrsig = self.sess.cookies.get("qrsig", "")

        # 保存二维码图片
        qr_path = Path("/tmp/qq_login_qr.png")
        qr_path.write_bytes(qr_resp.content)

        # 生成终端二维码
        qr_content = f"pt_login_sig={pt_login_sig}" if pt_login_sig else ""
        # 实际上 ptlogin2 的二维码内容就是 ptqrshow 的 URL 本身
        # 终端显示需要用 qrcode 库解析图片

        return qrsig, pt_login_sig

    def step2_poll_scan(self, qrsig: str, pt_login_sig: str) -> str | None:
        """轮询扫码状态，成功返回跳转 URL"""
        ptqrtoken = self._get_ptqr_token(qrsig)

        print("\n⏳ 等待扫码...")
        print("   请用手机 QQ 扫描上方二维码")
        print("   如果二维码过期，会自动刷新\n")

        poll_count = 0
        while poll_count < 60:  # 最多等 120 秒
            url = (
                f"https://ssl.ptlogin2.qq.com/ptqrlogin?"
                f"u1={quote('https://y.qq.com/portal/profile.html')}"
                f"&ptqrtoken={ptqrtoken}"
                f"&ptredirect=0&h=1&t=1&g=1&from_ui=1"
                f"&ptlang=2052&action=0-0-{int(time.time()*1000)}"
                f"&js_ver=20102616&js_type=1"
                f"&login_sig={pt_login_sig}"
                f"&pt_uistyle=40&aid={self.PTLOGIN_APPID}"
                f"&daid={self.PTLOGIN_DAID}&"
            )

            resp = self.sess.get(url, cookies={"qrsig": qrsig})
            text = resp.text

            if "二维码未失效" in text:
                # 还没扫
                pass
            elif "二维码认证中" in text:
                print("✅ 已扫码，请在手机上确认登录...")
            elif "二维码已失效" in text:
                print("🔄 二维码已过期，重新获取...")
                return "EXPIRED"
            elif "登录成功" in text:
                # 提取跳转 URL
                url_match = re.search(r"'(https?://[^']+)'", text)
                if url_match:
                    print("✅ 登录成功！")
                    return url_match.group(1)
                else:
                    print("❌ 登录成功但无法获取跳转地址")
                    return None
            else:
                # 未知状态
                if "error" in text.lower():
                    print(f"⚠️ 未知响应: {text[:200]}")

            poll_count += 1
            time.sleep(2)

        print("⏰ 扫码超时")
        return None

    def step3_get_cookies(self, redirect_url: str) -> dict:
        """跟随跳转 URL 获取完整 Cookie"""
        # 先访问 ptlogin2 的跳转，获取基础 Cookie (uin, skey, p_skey 等)
        resp = self.sess.get(redirect_url, allow_redirects=True)

        cookies = {}
        for domain_cookies in self.sess.cookies._cookies.values():
            for path_cookies in domain_cookies.values():
                for name, cookie in path_cookies.items():
                    cookies[name] = cookie.value

        # 提取 QQ 号
        qq_num = re.sub(r"\D", "", cookies.get("uin", cookies.get("wxuin", "")))
        if not qq_num:
            # 尝试从 URL 提取
            uin_match = re.search(r"uin=(\d+)", redirect_url)
            if uin_match:
                qq_num = uin_match.group(1)
        cookies["uin"] = qq_num

        # 现在访问 y.qq.com 获取 music_key (qm_keyst)
        print("🔑 获取 QQ 音乐凭证...")
        music_resp = self.sess.get(
            "https://y.qq.com/",
            headers={"Referer": "https://y.qq.com/"},
            allow_redirects=True,
        )

        # 合并 y.qq.com 的 Cookie
        for domain_cookies in self.sess.cookies._cookies.values():
            for path_cookies in domain_cookies.values():
                for name, cookie in path_cookies.items():
                    cookies[name] = cookie.value

        # 如果没有 qm_keyst，尝试通过 QQ 音乐的刷新接口获取
        if not cookies.get("qm_keyst") and not cookies.get("qqmusic_key"):
            print("🔄 尝试刷新 music_key...")
            self._refresh_music_key(cookies)

        return cookies

    def _refresh_music_key(self, cookies: dict):
        """通过 QQ 音乐刷新接口获取 music_key"""
        uin = cookies.get("uin", "0")
        skey = cookies.get("p_skey", cookies.get("skey", ""))

        # 计算GTK
        gtk = 5381
        for c in skey:
            gtk += (gtk << 5) + ord(c)
        gtk &= 0x7FFFFFFF

        # 先尝试直接访问 y.qq.com 登录确认页
        try:
            resp = self.sess.get(
                "https://y.qq.com/portal/profile.html",
                headers={"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items() if v)},
            )
            # 再次收集 Cookie
            for domain_cookies in self.sess.cookies._cookies.values():
                for path_cookies in domain_cookies.values():
                    for name, cookie in path_cookies.items():
                        cookies[name] = cookie.value
        except Exception:
            pass

    def login(self) -> bool:
        """完整二维码登录流程"""
        print("\n" + "=" * 50)
        print("  🎵 QQ 音乐登录 - 扫码授权")
        print("=" * 50)

        while True:
            qrsig, pt_login_sig = self.step1_get_qrcode()

            # 显示二维码
            if HAS_QRCODE:
                try:
                    # 从图片文件生成终端二维码
                    from PIL import Image
                    img = Image.open("/tmp/qq_login_qr.png")
                    qr = qrcode.QRCode(border=1)
                    # 用 ptqrshow URL 作为内容太长，改用图片解析
                    # 直接显示图片路径 + 终端提示
                    print(f"\n📱 二维码已保存到: /tmp/qq_login_qr.png")
                    print(f"   请用手机 QQ 扫描该图片登录\n")

                    # 尝试用 qrcode 库解码图片
                    try:
                        from pyzbar.pyzbar import decode as qr_decode
                        decoded = qr_decode(img)
                        if decoded:
                            qr_obj = qrcode.QRCode(border=1)
                            qr_obj.add_data(decoded[0].data.decode())
                            qr_obj.make(fit=True)
                            qr_obj.print_ascii(invert=True)
                    except ImportError:
                        # 没有解码库，直接尝试显示图片
                        try:
                            img.show()
                            print("📷 已在图片查看器中打开二维码")
                        except Exception:
                            pass
                except Exception:
                    print(f"📱 二维码已保存到: /tmp/qq_login_qr.png")
                    print("   请用手机 QQ 扫描登录")
            else:
                print(f"\n📱 二维码已保存到: /tmp/qq_login_qr.png")
                print("   请用手机 QQ 扫描登录")
                print("   (安装 qrcode 库可终端显示: pip install qrcode[pil])")

            # 轮询扫码
            result = self.step2_poll_scan(qrsig, pt_login_sig)

            if result == "EXPIRED":
                continue  # 重新获取二维码
            elif result is None:
                return False

            # 获取 Cookie
            cookies = self.step3_get_cookies(result)
            self.cm.set(cookies)

            if self.cm.is_logged_in:
                print(f"\n🎉 登录成功！QQ 号: {self.cm.uin}")
                return True
            else:
                print("\n⚠️ 登录似乎成功，但未获取到完整凭证")
                print(f"   uin: {self.cm.uin}")
                print(f"   qm_keyst: {'有' if self.cm.music_key else '无'}")
                print("\n尝试用浏览器手动获取 Cookie...")
                self._manual_cookie_input()
                return self.cm.is_logged_in

    def _manual_cookie_input(self):
        """手动输入 Cookie 的备选方案"""
        print("\n📋 备选方案: 手动输入 Cookie")
        print("   1. 在浏览器打开 https://y.qq.com 并登录")
        print("   2. 按 F12 → Application → Cookies → https://y.qq.com")
        print("   3. 找到以下 Cookie 值:\n")
        print("      uin = (你的QQ号)")
        print("      qm_keyst = (一串长字符)")
        print("      p_skey = (一串长字符)")
        print()

        uin = input("请输入 uin (QQ号): ").strip()
        keyst = input("请输入 qm_keyst: ").strip()
        pskey = input("请输入 p_skey: ").strip()

        if uin and keyst:
            self.cm.set({
                "uin": uin,
                "qm_keyst": keyst,
                "qqmusic_key": keyst,
                "p_skey": pskey,
            })


# ─── QQ 音乐 API ───────────────────────────────────────────────────────

class QQMusicAPI:
    def __init__(self, cm: CookieManager):
        self.cm = cm

    def _request_fcg(self, data: dict, use_sign: bool = True, host: str = "u.y.qq.com") -> dict:
        """
        请求 musicu.fcg / musics.fcg
        use_sign=True 时走签名通道 (musics.fcg)
        use_sign=False 时走明文通道 (musicu.fcg)
        host 仅对签名通道生效，可切换到备用边缘节点 (如 u6.y.qq.com)。
        """
        data_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)

        if use_sign:
            # 对即将发送的原始 utf-8 字节签名，保证签名对象 == POST body，
            # 否则服务端重算签名会不一致而拒绝 (code 2000)。
            body = data_str.encode("utf-8")
            sign = get_sign(body)
            url = f"https://{host}/cgi-bin/musics.fcg?sign={sign}"
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
            }
            resp = self.cm.session.post(url, data=body, headers=headers)
        else:
            url = "https://u.y.qq.com/cgi-bin/musicu.fcg"
            params = {
                "-": "getplaysongvkey",
                "g_tk": 5381,
                "loginUin": self.cm.uin,
                "hostUin": 0,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "notice": 0,
                "platform": "yqq.json",
                "needNewCode": 0,
                "data": data_str,
            }
            resp = self.cm.session.get(url, params=params)

        try:
            return resp.json()
        except Exception:
            # 可能是 Zlib 压缩
            import zlib
            try:
                raw = resp.content
                # 去掉可能的 5 字节前缀
                for i in range(min(10, len(raw))):
                    if raw[i] == 0x78:  # zlib header
                        return json.loads(zlib.decompress(raw[i:]))
                return json.loads(raw)
            except Exception:
                return {"code": -1, "error": resp.text[:500]}

    def _search_payload(self, keyword: str, page: int, num: int) -> dict:
        """构造 musics.fcg 桌面端搜索请求体 (search / 备用节点共用)。"""
        return {
            "comm": {
                "ct": 19,
                "cv": 2201,
                "chid": "0",
                "g_tk": self.cm.gtk,
                "uin": self.cm.uin or "0",
                "authst": self.cm.music_key,
                "guid": GUID,
            },
            "req_1": {
                # 注意 module 为 SearchCgiService，旧代码多写了一个 Search 导致 req_1 报错
                "module": "music.search.SearchCgiService",
                "method": "DoSearchForQQMusicDesktop",
                "param": {
                    "search_type": 0,
                    "query": keyword,
                    "page_num": page,
                    "num_per_page": num,
                    "grp": True,
                },
            },
        }

    def search(self, keyword: str, page: int = 1, num: int = 20) -> list[dict]:
        """搜索歌曲 (主节点 u.y.qq.com)。"""
        data = self._search_payload(keyword, page, num)
        result = self._request_fcg(data, use_sign=True)

        try:
            items = result["req_1"]["data"]["body"]["song"]["list"]
        except (KeyError, TypeError):
            # 回退到旧搜索接口
            return self._search_legacy(keyword, page, num)

        songs = []
        for item in items:
            song = {
                "mid": item.get("mid", ""),
                "name": item.get("name", "").replace("\u00a0", " "),
                "singer": ", ".join(s.get("name", "") for s in item.get("singer", [])),
                "album": item.get("album", {}).get("name", ""),
                "album_mid": item.get("album", {}).get("mid", ""),
                "duration": item.get("interval", 0),
            }
            songs.append(song)

        return songs

    def _search_legacy(self, keyword: str, page: int = 1, num: int = 20) -> list[dict]:
        """
        备用搜索通道。

        旧的 client_search_cp (c.y.qq.com/soso) 接口已被 QQ 音乐下线 (裸请求恒返回
        HTTP 500)，无法再作为独立回退。这里改为在备用边缘节点 u6.y.qq.com 上重试
        同一个签名搜索请求，用于应对主节点 u.y.qq.com 偶发不可用的情况。
        """
        data = self._search_payload(keyword, page, num)
        result = self._request_fcg(data, use_sign=True, host="u6.y.qq.com")
        try:
            items = result["req_1"]["data"]["body"]["song"]["list"]
        except (KeyError, TypeError):
            return []

        songs = []
        for item in items:
            album = item.get("album") or {}
            songs.append({
                "mid": item.get("mid", ""),
                "name": item.get("name", ""),
                "singer": ", ".join(s.get("name", "") for s in item.get("singer", [])),
                "album": album.get("name", ""),
                "album_mid": album.get("mid", ""),
                "duration": item.get("interval", 0),
            })

        return songs

    def get_song_url(self, song_mid: str, quality: str = "320") -> str | None:
        """获取歌曲播放/下载 URL"""
        quality_info = QUALITY_MAP.get(quality, QUALITY_MAP["320"])

        # 第一次请求: 不指定 filename，获取默认 purl 和 media_mid
        data_first = {
            "req_0": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    "guid": "327783793",
                    "songmid": [song_mid],
                    "songtype": [0],
                    "uin": self.cm.uin,
                    "loginflag": 1,
                    "platform": "20",
                },
            },
            "comm": {
                "uin": int(self.cm.uin) if self.cm.uin.isdigit() else 0,
                "format": "json",
                "ct": 19,
                "cv": 0,
                "authst": self.cm.music_key,
            },
        }

        result = self._request_fcg(data_first, use_sign=False)

        try:
            req0 = result["req_0"]["data"]
            purl = req0["midurlinfo"][0].get("purl", "")
            sip = req0.get("sip", [])
        except (KeyError, TypeError, IndexError):
            return None

        if not purl:
            # purl 为空 = 无权限 (非会员/付费歌曲)
            print(f"  ⚠️ 无法获取链接 (可能需要会员或歌曲不可用)")
            return None

        # 从 purl 提取 media_mid
        # purl 格式: "C400{mediaMid}.m4a?vkey=..." 或 "M500{mediaMid}.mp3?vkey=..."
        media_mid = ""
        filename_part = purl.split("?")[0]  # "C400XXXXX.m4a"
        if len(filename_part) > 4:
            media_mid = filename_part[4:].rsplit(".", 1)[0]  # 去掉前缀和后缀

        # 第二次请求: 指定目标品质的 filename
        if media_mid and quality != "m4a":
            filename = f"{quality_info['prefix']}{media_mid}{quality_info['ext']}"

            data_second = {
                "req_0": {
                    "module": "vkey.GetVkeyServer",
                    "method": "CgiGetVkey",
                    "param": {
                        "filename": [filename],
                        "guid": "327783793",
                        "songmid": [song_mid],
                        "songtype": [0],
                        "uin": self.cm.uin,
                        "loginflag": 1,
                        "platform": "20",
                    },
                },
                "comm": {
                    "uin": int(self.cm.uin) if self.cm.uin.isdigit() else 0,
                    "format": "json",
                    "ct": 19,
                    "cv": 0,
                    "authst": self.cm.music_key,
                },
            }

            result2 = self._request_fcg(data_second, use_sign=False)
            try:
                purl2 = result2["req_0"]["data"]["midurlinfo"][0].get("purl", "")
                if purl2:
                    purl = purl2
            except (KeyError, TypeError, IndexError):
                pass

        # 拼接最终 URL
        domain = sip[1] if len(sip) > 1 else (sip[0] if sip else "https://ws.stream.qqmusic.qq.com/")
        if not domain.startswith("http"):
            domain = f"https://{domain}"

        full_url = f"{domain}{purl}" if purl.startswith("/") or not purl.startswith("http") else purl
        if not full_url.startswith("http"):
            full_url = f"{domain}/{purl}"

        return full_url

    def get_playlist_songs(self, playlist_id: int) -> list[dict]:
        """获取歌单歌曲列表"""
        url = "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"
        params = {
            "type": 1,
            "json": 1,
            "utf8": 1,
            "onlysong": 0,
            "disstid": playlist_id,
            "song_begin": 0,
            "song_num": 500,
            "g_tk": self.cm.gtk,
            "format": "json",
        }

        resp = self.cm.session.get(url, params=params)
        try:
            data = resp.json()
            songlist = data["cdlist"][0]["songlist"]
        except (KeyError, TypeError, IndexError, json.JSONDecodeError):
            return []

        songs = []
        for item in songlist:
            song = {
                "mid": item.get("songmid", item.get("mid", "")),
                "name": item.get("songname", item.get("title", "")),
                "singer": ", ".join(s.get("name", "") for s in item.get("singer", [])),
                "album": item.get("albumname", item.get("album", {}).get("name", "")),
                "album_mid": item.get("albummid", item.get("album", {}).get("mid", "")),
                "duration": item.get("interval", 0),
            }
            songs.append(song)
        return songs

    def get_user_playlists(self) -> list[dict]:
        """获取用户歌单列表"""
        url = "https://c6.y.qq.com/rsc/fcgi-bin/fcg_user_created_diss"
        params = {
            "format": "json",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "platform": "yqq.json",
            "needNewCode": 1,
            "uin": self.cm.uin,
            "g_tk": self.cm.gtk,
            "hostuin": self.cm.uin,
            "sin": 0,
            "size": 200,
        }

        resp = self.cm.session.get(url, params=params)
        try:
            data = resp.json()
            disslist = data["data"]["disslist"]
        except (KeyError, TypeError):
            return []

        playlists = []
        for pl in disslist:
            playlists.append({
                "id": pl.get("tid", 0),
                "name": pl.get("diss_name", ""),
                "count": pl.get("song_cnt", 0),
            })
        return playlists


# ─── 下载功能 ───────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    """清理文件名"""
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def download_file(url: str, output_path: Path, show_progress: bool = True):
    """下载文件，带进度条"""
    resp = requests.get(url, stream=True, headers={"User-Agent": UA, "Referer": REFERER})
    total = int(resp.headers.get("Content-Length", 0))

    output_path.parent.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    with open(output_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=32768):
            f.write(chunk)
            downloaded += len(chunk)
            if show_progress and total > 0:
                pct = downloaded / total * 100
                bar = "█" * int(pct / 2) + "░" * (50 - int(pct / 2))
                print(f"\r  下载中 [{bar}] {pct:.1f}%", end="", flush=True)

    if show_progress:
        print()  # 换行


def format_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


# ─── 交互式界面 ─────────────────────────────────────────────────────────

def interactive_search(api: QQMusicAPI, quality: str):
    """交互式搜索下载"""
    while True:
        print("\n" + "─" * 50)
        keyword = input("🔍 搜索歌曲 (输入 q 退出): ").strip()
        if keyword.lower() in ("q", "quit", "exit"):
            break
        if not keyword:
            continue

        songs = api.search(keyword)
        if not songs:
            print("  没有找到歌曲")
            continue

        print(f"\n  搜索结果:")
        print(f"  {'序号':>4}  {'歌曲':<30}  {'歌手':<20}  {'时长':>5}")
        print(f"  {'─'*4}  {'─'*30}  {'─'*20}  {'─'*5}")

        for i, song in enumerate(songs, 1):
            name = song["name"][:28]
            singer = song["singer"][:18]
            dur = format_duration(song["duration"])
            print(f"  {i:>4}  {name:<30}  {singer:<20}  {dur:>5}")

        choice = input("\n  输入序号下载 (多选用逗号分隔, a=全部, q=返回): ").strip()
        if choice.lower() == "q":
            continue

        indices = []
        if choice.lower() == "a":
            indices = list(range(len(songs)))
        else:
            try:
                indices = [int(x.strip()) - 1 for x in choice.split(",")]
            except ValueError:
                print("  ⚠️ 无效输入")
                continue

        for idx in indices:
            if 0 <= idx < len(songs):
                download_song(api, songs[idx], quality)


def download_song(api: QQMusicAPI, song: dict, quality: str):
    """下载单首歌曲"""
    mid = song["mid"]
    name = song["name"]
    singer = song["singer"]

    quality_info = QUALITY_MAP.get(quality, QUALITY_MAP["320"])
    filename = sanitize_filename(f"{singer} - {name}{quality_info['ext']}")
    output_path = DOWNLOAD_DIR / filename

    if output_path.exists() and output_path.stat().st_size > 0:
        print(f"  ⏭️  已存在: {filename}")
        return

    print(f"  🎵 {singer} - {name} [{quality_info['label']}]")

    url = api.get_song_url(mid, quality)
    if not url:
        return

    try:
        download_file(url, output_path)
        print(f"  ✅ 已保存: {output_path}")
    except Exception as e:
        print(f"  ❌ 下载失败: {e}")
        if output_path.exists():
            output_path.unlink()


def interactive_playlists(api: QQMusicAPI, quality: str):
    """交互式歌单下载"""
    playlists = api.get_user_playlists()
    if not playlists:
        print("\n  ⚠️ 没有获取到歌单 (可能未登录)")
        return

    print(f"\n  📋 你的歌单:")
    print(f"  {'序号':>4}  {'歌单名':<30}  {'歌曲数':>6}")
    print(f"  {'─'*4}  {'─'*30}  {'─'*6}")

    for i, pl in enumerate(playlists, 1):
        name = pl["name"][:28]
        print(f"  {i:>4}  {name:<30}  {pl['count']:>6}")

    choice = input("\n  输入序号下载歌单 (q=返回): ").strip()
    if choice.lower() == "q":
        return

    try:
        idx = int(choice) - 1
    except ValueError:
        return

    if 0 <= idx < len(playlists):
        pl = playlists[idx]
        print(f"\n  📥 下载歌单: {pl['name']} ({pl['count']} 首)")

        songs = api.get_playlist_songs(pl["id"])
        for i, song in enumerate(songs, 1):
            print(f"\n  [{i}/{len(songs)}]", end="")
            download_song(api, song, quality)


# ─── 主入口 ─────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="QQ音乐下载器 - 支持扫码登录 + 会员音质")
    parser.add_argument("command", nargs="?", default="", help="命令: search/搜索, song/歌曲, playlist/歌单")
    parser.add_argument("query", nargs="?", default="", help="搜索关键词 / songMid / 歌单ID")
    parser.add_argument("--quality", "-q", default="320", choices=list(QUALITY_MAP.keys()), help="音质 (128/320/flac/ape/m4a)")
    parser.add_argument("--login", action="store_true", help="强制重新登录")
    parser.add_argument("--cookie", type=str, help="直接传入 Cookie 字符串")
    parser.add_argument("--index", "-i", type=int, default=None, help="仅下载 search 结果中的第 N 条 (1-based)，用于非交互调用")
    parser.add_argument("--first", action="store_true", help="仅下载 search 结果中的第 1 条，等价于 --index 1")
    parser.add_argument("--output-dir", "-o", type=str, default=None, help="下载输出目录，默认 ~/Music/QQMusic")

    args = parser.parse_args()

    if args.output_dir:
        global DOWNLOAD_DIR
        DOWNLOAD_DIR = Path(args.output_dir).expanduser()

    # 初始化
    cm = CookieManager()
    api = QQMusicAPI(cm)

    # 加载或获取 Cookie
    if args.cookie:
        # 直接传入 Cookie
        cookie_dict = {}
        for part in args.cookie.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                cookie_dict[k.strip()] = v.strip()
        cm.set(cookie_dict)
    elif args.login or not cm.load() or not cm.is_logged_in:
        # 需要登录
        qr_login = QQrLogin(cm)
        if not qr_login.login():
            print("\n❌ 登录失败")
            sys.exit(1)

    if args.login and not args.command:
        # 非交互登录: 不带子命令时登录完成即退出, 不进入交互菜单
        print(f"\n✅ 登录完成, Cookie 已保存: QQ {cm.uin}")
        sys.exit(0)

    print(f"\n✅ 当前登录: QQ {cm.uin}")
    print(f"   音质选择: {QUALITY_MAP[args.quality]['label']}")

    # 执行命令
    if args.command in ("search", "搜索", "s"):
        if not args.query:
            print("❌ 请提供搜索关键词")
            sys.exit(1)
        songs = api.search(args.query)
        if not songs:
            print("没有找到歌曲")
            sys.exit(1)

        print(f"\n搜索结果:")
        for i, song in enumerate(songs, 1):
            print(f"  {i}. {song['singer']} - {song['name']} [{format_duration(song['duration'])}]")

        if args.first or args.index is not None:
            # 非交互模式: 只下载指定序号的一条
            idx = 0 if args.first else (args.index - 1)
            if not (0 <= idx < len(songs)):
                print(f"❌ 序号超出范围 (共 {len(songs)} 条结果)")
                sys.exit(1)
            download_song(api, songs[idx], args.quality)
        else:
            # 未指定 --first / --index 时，默认下载全部结果 (非交互，不会阻塞)
            for song in songs:
                download_song(api, song, args.quality)

    elif args.command in ("song", "歌曲"):
        if not args.query:
            print("❌ 请提供 songMid")
            sys.exit(1)
        song = {"mid": args.query, "name": args.query, "singer": "Unknown", "duration": 0}
        download_song(api, song, args.quality)

    elif args.command in ("playlist", "歌单", "pl"):
        if not args.query:
            interactive_playlists(api, args.quality)
        else:
            try:
                playlist_id = int(args.query)
            except ValueError:
                print("❌ 歌单ID必须是数字")
                sys.exit(1)
            songs = api.get_playlist_songs(playlist_id)
            print(f"歌单共 {len(songs)} 首歌")
            for i, song in enumerate(songs, 1):
                print(f"\n[{i}/{len(songs)}]", end="")
                download_song(api, song, args.quality)

    else:
        # 交互模式
        while True:
            print("\n" + "=" * 50)
            print("  🎵 QQ 音乐下载器")
            print("=" * 50)
            print("  1. 搜索下载")
            print("  2. 下载歌单")
            print("  3. 切换音质 (当前: {})".format(QUALITY_MAP[args.quality]["label"]))
            print("  4. 重新登录")
            print("  5. 退出")

            choice = input("\n请选择: ").strip()

            if choice == "1":
                interactive_search(api, args.quality)
            elif choice == "2":
                interactive_playlists(api, args.quality)
            elif choice == "3":
                print("\n可选音质:")
                for k, v in QUALITY_MAP.items():
                    marker = " ← 当前" if k == args.quality else ""
                    print(f"  {k}: {v['label']}{marker}")
                q = input("选择音质: ").strip()
                if q in QUALITY_MAP:
                    args.quality = q
                    print(f"✅ 已切换到 {QUALITY_MAP[q]['label']}")
            elif choice == "4":
                qr_login = QQrLogin(cm)
                qr_login.login()
            elif choice in ("5", "q", "quit"):
                print("👋 再见！")
                break


if __name__ == "__main__":
    main()
