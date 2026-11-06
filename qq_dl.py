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
  python qq_music_dl.py --login --login-type wx  # 微信扫码登录

依赖:
  pip install requests mutagen Pillow qrcode
  (可选) ffmpeg: 下载后完整解码一遍，检查音频有没有损坏

音质选择 (有会员可用高品质):
  --quality 128    标准MP3 128kbps
  --quality 320    HQ MP3 320kbps (需会员)
  --quality flac   FLAC 无损 (需会员)
  --quality master 臻品母带 FLAC，24 bit / 192 kHz (需超级会员；是 QQ 用算法从无损加工出来的，不是录音室母带)
  --quality m4a    默认M4A格式

Cookie 保存位置: ~/.qqmusic_cookie.json
"""

import base64
import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
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
    "master": {"prefix": "AI00", "ext": ".flac", "label": "臻品母带 FLAC"},
    "ape":  {"prefix": "A000", "ext": ".ape",  "label": "无损 APE"},
    "m4a":  {"prefix": "C400", "ext": ".m4a",  "label": "标准 M4A"},
}
# --quality best：按这个顺序取第一个有音源的音质。
# 不含臻品母带：它和无损在 8 kHz 以下逐采样一致，高频是重新生成的，没有无损的歌甚至是从 320k MP3 算出来的
BEST_ORDER = ["flac", "320", "128"]
BEST_LABEL = "最佳（无损优先，没有就退到 320 / 128）"

# album --spatial 另存的全景声版用到的文件，不在 --quality 里。
# 杜比全景声是唱片公司交付的单独混音（服务器上是 .mp4，存成 .m4a）；臻品全景声是 QQ 从立体声成品算出来的
SPATIAL_MAP = {
    "dolby768": {"prefix": "D003", "ext": ".m4a", "remote_ext": ".mp4", "label": "杜比全景声 768k"},
    "dolby640": {"prefix": "D002", "ext": ".m4a", "remote_ext": ".mp4", "label": "杜比全景声 640k"},
    "dolby448": {"prefix": "D001", "ext": ".m4a", "remote_ext": ".mp4", "label": "杜比全景声 448k"},
    "atmos51": {"prefix": "Q001", "ext": ".flac", "label": "臻品全景声 5.1 FLAC"},
}
SPATIAL_DOLBY = ["dolby768", "dolby640", "dolby448"]
SPATIAL_VERSION = "全景声"  # 写进 ALBUMVERSION，Navidrome 据此把它当成同名专辑的另一个版本
# 纯文本歌词的时间轴：Gemini 听整首歌对出每句开唱的时间。只有设了 API key 才启用。
# 音频必须放在用户消息里：放进工具调用的返回结果（Antigravity 的 read_file 就是这样）时间会整体漂几秒到几十秒
GEMINI_MODEL = "gemini-4-argon"
AI_LYRICS = {"enabled": True, "transcribe": False}
REPLAYGAIN_REFERENCE = -18.0  # LUFS；杜比版按规范做在 -18 左右，臻品全景声跟立体声一样在 -9 左右，混在一张专辑里要靠它拉平


def quality_label(quality: str) -> str:
    return BEST_LABEL if quality == "best" else QUALITY_MAP[quality]["label"]

QR_PATH = Path("/tmp/qq_login_qr.png")  # QQ / 微信登录二维码都存这里
WX_APPID = "wx48db31d50e334801"  # y.qq.com 网页版在微信开放平台的 appid
KEY_REFRESH_MARGIN = 12 * 3600  # 微信登录的 musickey 只有 3 天，过期前 12 小时内就自动续期

COVER_MAX_SIDE = 3000  # 原图超过这个边长就退到 1200 版，避免把十几 MB 的图塞进每首歌
COVER_MAX_BYTES = 8 * 1024 * 1024
COVER_MIN_SIDE = 300

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
        """保存 Cookie 到文件（里面有登录凭证，只给自己读写）"""
        COOKIE_FILE.write_text(json.dumps(self.cookies, ensure_ascii=False, indent=2))
        os.chmod(COOKIE_FILE, 0o600)

    def set(self, cookie_dict: dict):
        """设置 Cookie"""
        self.cookies.update(cookie_dict)
        self._apply_cookies()
        self.save()

    def replace(self, cookie_dict: dict):
        """整体替换 Cookie。重新登录时用，免得上一个账号的 uin / key 残留下来串号"""
        self.cookies = dict(cookie_dict)
        self.session.cookies.clear()
        self._apply_cookies()
        self.save()

    def update_from_wx_login(self, data: dict, replace: bool = False):
        """把 music.login.LoginServer.Login 的返回写进 Cookie（微信扫码登录 / 续期共用）"""
        new = {
            "wxuin": str(data["musicid"]),
            "qm_keyst": data["musickey"],
            "qqmusic_key": data["musickey"],
            "tmeLoginType": "1",
            "login_type": "2",
            "_key_created": int(time.time()),
            "_key_expires_in": int(data.get("keyExpiresIn") or 0),
        }
        for src, dst in (("openid", "wxopenid"), ("refresh_token", "wxrefresh_token"),
                         ("unionid", "wxunionid"), ("encryptUin", "euin"), ("refresh_key", "refresh_key")):
            if data.get(src):
                new[dst] = str(data[src])
        if replace:
            self.replace(new)
        else:
            self.set(new)

    def _apply_cookies(self):
        """将 Cookie 应用到 session"""
        for k, v in self.cookies.items():
            if k.startswith("_"):  # _ 开头的是本地记录（如 key 签发时间），不发给服务端
                continue
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

    @property
    def login_type(self) -> str:
        """"wx" = 微信登录（key 以 W_X_ 开头），"qq" = QQ 登录"""
        if str(self.cookies.get("tmeLoginType", "")) == "1" or self.music_key.startswith("W_X_"):
            return "wx"
        return "qq"

    @property
    def account_label(self) -> str:
        return f"{'微信' if self.login_type == 'wx' else 'QQ'} {self.uin}"


def save_qr_image(content: bytes) -> Path:
    """保存登录二维码。放大并加白边，方便发到手机上长按识别，或者用另一台设备扫。"""
    try:
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(content)).convert("RGB")
        scale = max(1, 560 // max(img.size))
        if scale > 1:
            img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
        ImageOps.expand(img, border=32, fill="white").save(QR_PATH, "PNG")
    except Exception:
        QR_PATH.write_bytes(content)
    return QR_PATH


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
        save_qr_image(qr_resp.content)

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
            self.cm.replace(cookies)

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


class WxQrLogin:
    """
    微信扫码登录（和 y.qq.com 网页版走同一条路）
    流程: 微信开放平台 qrconnect 拿二维码 → 长轮询扫码状态拿 code → music.login.LoginServer.Login 换 musickey
    """

    POLL_TIMEOUT = 180  # 秒

    def __init__(self, cm: CookieManager, api: "QQMusicAPI"):
        self.cm = cm
        self.api = api
        self.sess = requests.Session()
        self.sess.headers.update({"User-Agent": UA})

    def _get_qrcode(self) -> str:
        """拉取二维码并保存图片，返回 uuid"""
        resp = self.sess.get("https://open.weixin.qq.com/connect/qrconnect", params={
            "appid": WX_APPID,
            "redirect_uri": "https://y.qq.com/portal/wx_redirect.html?login_type=2&surl=https://y.qq.com/",
            "response_type": "code",
            "scope": "snsapi_login",
            "state": "STATE",
            "href": "https://y.qq.com/mediastyle/music_v17/src/css/popup_wechat.css#wechat_redirect",
        })
        m = re.search(r"/connect/qrcode/([\w-]+)", resp.text)
        if not m:
            raise RuntimeError("微信开放平台没有返回二维码")
        uuid = m.group(1)
        save_qr_image(self.sess.get(f"https://open.weixin.qq.com/connect/qrcode/{uuid}").content)
        return uuid

    def _poll(self, uuid: str) -> str | None:
        """长轮询扫码状态，确认后返回微信 code。408=等待扫码 404=已扫码 405=已确认 403=取消 402=过期"""
        deadline = time.time() + self.POLL_TIMEOUT
        last = None
        while time.time() < deadline:
            params = {"uuid": uuid, "_": int(time.time() * 1000)}
            if last:
                params["last"] = last
            try:
                text = self.sess.get(
                    "https://lp.open.weixin.qq.com/connect/l/qrconnect",
                    params=params, headers={"Referer": "https://open.weixin.qq.com/"}, timeout=40,
                ).text
            except requests.RequestException:
                time.sleep(2)
                continue

            m = re.search(r"wx_errcode=(\d+);window\.wx_code='([^']*)'", text)
            if not m:
                time.sleep(2)
                continue
            errcode, code = m.groups()
            if errcode == "404" and last != "404":
                print("✅ 已扫码，请在手机上确认登录...")
            last = errcode

            if errcode == "405":
                return code
            if errcode == "403":
                print("❌ 已在手机上取消登录")
                return None
            if errcode == "402":
                print("⏰ 二维码已过期")
                return None

        print("⏰ 扫码超时")
        return None

    def login(self) -> bool:
        print("\n" + "=" * 50)
        print("  🎵 QQ 音乐登录 - 微信扫码授权")
        print("=" * 50)

        uuid = self._get_qrcode()
        print(f"\n📱 二维码已保存到: {QR_PATH}")
        print("   请用微信扫码（在手机上看到这张图时可以长按识别）")
        print(f"\n⏳ 等待扫码...（最长 {self.POLL_TIMEOUT} 秒）")

        code = self._poll(uuid)
        if not code:
            return False

        data = self.api.wx_login(code)
        if not data:
            print("❌ 微信已确认，但换取 QQ 音乐凭证失败")
            return False
        self.cm.update_from_wx_login(data, replace=True)
        print(f"\n🎉 登录成功！{self.cm.account_label}")
        return True


# ─── QQ 音乐 API ───────────────────────────────────────────────────────

class QQMusicAPI:
    def __init__(self, cm: CookieManager):
        self.cm = cm
        self._login_valid: bool | None = None
        self._album_cache: dict[str, dict] = {}
        self._cover_cache: dict[str, dict | None] = {}
        self.last_url_problem = ""

    def _musics(self, **reqs) -> dict:
        """一次签名请求里带上多个模块调用，返回 {请求名: 结果}"""
        comm = {
            "ct": 24,
            "cv": 0,
            "format": "json",
            "uin": int(self.cm.uin) if self.cm.uin.isdigit() else 0,
            "authst": self.cm.music_key,
        }
        return self._request_fcg({"comm": comm, **reqs}, use_sign=True)

    # ── 登录状态 ──

    def _wx_login_call(self, param: dict, with_auth: bool) -> dict | None:
        comm = {"tmeLoginType": "1", "ct": 24, "cv": 0, "format": "json", "uin": 0}
        if with_auth:
            comm["uin"] = int(self.cm.uin) if self.cm.uin.isdigit() else 0
            comm["authst"] = self.cm.music_key
        req = {"module": "music.login.LoginServer", "method": "Login", "param": param}
        r0 = self._request_fcg({"comm": comm, "req_0": req}).get("req_0", {})
        data = r0.get("data") or {}
        if r0.get("code") == 0 and data.get("musickey") and data.get("musicid"):
            return data
        return None

    def wx_login(self, code: str) -> dict | None:
        """用微信扫码拿到的 code 换 QQ 音乐的 musickey"""
        return self._wx_login_call({"code": code, "strAppid": WX_APPID}, with_auth=False)

    def refresh_login(self) -> bool:
        """用 refresh_token 续期微信登录的 musickey（新 key 3 天有效，同时换一个新的 refresh_token）"""
        c = self.cm.cookies
        if self.cm.login_type != "wx" or not c.get("wxrefresh_token") or not self.cm.uin.isdigit():
            return False
        data = self._wx_login_call({
            "refresh_key": c.get("refresh_key", ""),
            "refresh_token": c["wxrefresh_token"],
            "musickey": self.cm.music_key,
            "musicid": int(self.cm.uin),
        }, with_auth=True)
        if not data:
            return False
        self.cm.update_from_wx_login(data)
        self._login_valid = True
        return True

    def login_valid(self) -> bool | None:
        """
        用会员查询接口探一下 musickey 还有没有效。
        失效时这个接口返回 code 1000，而取链接接口只会返回空 purl，看起来和"没有会员"一模一样。
        返回 None 表示判断不了。
        """
        if self._login_valid is None and self.cm.is_logged_in:
            data = {
                "comm": {
                    "uin": int(self.cm.uin) if self.cm.uin.isdigit() else 0,
                    "format": "json", "ct": 24, "cv": 0,
                    "authst": self.cm.music_key,
                    "tmeLoginType": "1" if self.cm.login_type == "wx" else "2",
                },
                "req_0": {"module": "userInfo.VipQueryServer", "method": "SRFVipQuery_V2",
                          "param": {"uin_list": [self.cm.uin]}},
            }
            code = self._request_fcg(data, use_sign=False).get("req_0", {}).get("code")
            if code == 0:
                self._login_valid = True
            elif code == 1000:
                self._login_valid = False
        return self._login_valid

    def ensure_fresh_login(self):
        """微信登录的 key 快过期或已过期时，自动用 refresh_token 续期"""
        c = self.cm.cookies
        if self.cm.login_type != "wx" or not c.get("wxrefresh_token"):
            return
        created, ttl = c.get("_key_created"), c.get("_key_expires_in")
        if created and ttl and time.time() < created + ttl - KEY_REFRESH_MARGIN:
            return
        if self.refresh_login():
            print("🔄 已自动续期微信登录（新凭证 3 天有效）")
        elif self.login_valid() is False:
            print("⚠️ 微信登录已过期，自动续期也失败了，需要重新扫码：qq_dl.py --login --login-type wx")

    def relogin_hint(self) -> str:
        return "qq_dl.py --login" + (" --login-type wx" if self.cm.login_type == "wx" else "")

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
                "name": (item.get("title") or item.get("name") or "").replace("\u00a0", " "),
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
                "name": (item.get("title") or item.get("name") or "").replace("\u00a0", " "),
                "singer": ", ".join(s.get("name", "") for s in item.get("singer", [])),
                "album": album.get("name", ""),
                "album_mid": album.get("mid", ""),
                "duration": item.get("interval", 0),
            })

        return songs

    def get_song_url(self, song_mid: str, quality: str = "320", quiet: bool = False) -> str | None:
        """
        获取指定音质的下载 URL。这首歌没有这个音质时返回 None，不会拿别的音质顶替，
        原因写在 self.last_url_problem 里（quiet=False 时也直接打印出来）。
        """
        quality_info = QUALITY_MAP.get(quality) or SPATIAL_MAP.get(quality) or QUALITY_MAP["320"]
        self.last_url_problem = ""

        def fail(problem: str) -> None:
            self.last_url_problem = problem
            if not quiet:
                print(f"  ⚠️ {problem}")

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
            fail("无法获取链接：接口返回异常")
            return None

        if not purl:
            # purl 为空 = 无权限 (非会员/付费歌曲)，或者登录已失效 (此时服务端按匿名处理)
            if self.login_valid() is False:
                fail(f"无法获取链接：登录已失效（不是会员问题），请重新登录：{self.relogin_hint()}")
            else:
                fail("无法获取链接 (可能需要会员或歌曲不可用)")
            return None

        # 从 purl 提取 media_mid
        # purl 格式: "C400{mediaMid}.m4a?vkey=..." 或 "M500{mediaMid}.mp3?vkey=..."
        media_mid = ""
        filename_part = purl.split("?")[0]  # "C400XXXXX.m4a"
        if len(filename_part) > 4:
            media_mid = filename_part[4:].rsplit(".", 1)[0]  # 去掉前缀和后缀

        # 第二次请求: 指定目标品质的 filename
        if media_mid and quality != "m4a":
            filename = f"{quality_info['prefix']}{media_mid}{quality_info.get('remote_ext', quality_info['ext'])}"

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
            except (KeyError, TypeError, IndexError):
                purl2 = ""
            if not purl2:
                # 以前这里会退回默认的 m4a 链接，结果把 m4a 存成 .flac / .mp3
                fail(f"这首歌没有{quality_info['label']}音源")
                return None
            purl = purl2
        elif not media_mid and quality != "m4a":
            fail("无法获取链接：拿不到音频文件 ID")
            return None

        # 拼接最终 URL
        # sip 里第一个（aqqmusic.tc.qq.com）实测 5–16 MB/s，第二个（sjyN.stream.qqmusic.qq.com）只有约 0.5 MB/s，
        # 一首 150 MB 的臻品母带要下 5 分钟
        domain = sip[0] if sip else "https://aqqmusic.tc.qq.com/"
        if not domain.startswith("http"):
            domain = f"https://{domain}"

        full_url = f"{domain}{purl}" if purl.startswith("/") or not purl.startswith("http") else purl
        if not full_url.startswith("http"):
            full_url = f"{domain}/{purl}"

        return full_url

    # ── 元数据 ──

    def get_song_meta(self, song_mid: str) -> dict | None:
        """歌曲详情 + 歌词 + 专辑信息，整理成写标签用的字段"""
        r = self._musics(
            detail={"module": "music.pf_song_detail_svr", "method": "get_song_detail_yqq",
                    "param": {"song_mid": song_mid, "song_type": 0}},
            lyric={"module": "music.musichallSong.PlayLyricInfo", "method": "GetPlayLyricInfo",
                   "param": {"songMID": song_mid, "trans": 0, "roma": 0}},
        )
        try:
            data = r["detail"]["data"]
            ti = data["track_info"]
        except (KeyError, TypeError):
            return None
        if not ti or not ti.get("mid"):
            return None

        info = data.get("info") or {}

        def info_values(key: str) -> list[str]:
            return [c["value"] for c in (info.get(key) or {}).get("content", []) if c.get("value")]

        singers = ti.get("singer") or []
        album = ti.get("album") or {}
        meta = {
            "mid": ti["mid"],
            "title": (ti.get("title") or ti.get("name") or "").replace(" ", " "),
            "artists": [s["name"] for s in singers if s.get("name")],
            "singer_mids": [s["mid"] for s in singers if s.get("mid")],
            "album": album.get("name", ""),
            "album_mid": album.get("mid", ""),
            "album_artists": [],
            "track": None, "track_total": None,
            "disc": None, "disc_total": None,
            "date": _clean_date(album.get("time_public") or ti.get("time_public")),
            "genre": ", ".join(info_values("genre")),
            "label": next(iter(info_values("company")), ""),
            "lyrics": _decode_lyric(r.get("lyric")),
            "duration": ti.get("interval", 0),
            "file": ti.get("file") or {},  # 各档音质的文件大小，0 = 没有这一档
        }

        if meta["album_mid"]:
            alb = self._album_info(meta["album_mid"])
            meta["album_artists"] = alb.get("artists", [])
            meta["date"] = alb.get("date") or meta["date"]
            meta["label"] = meta["label"] or alb.get("label", "")
            meta["genre"] = meta["genre"] or alb.get("genre", "")
            # QQ 的 index_album 是整张专辑连续编号（第二张碟从 14 接着数），index_cd 从 0 开始，
            # 这里换算成"第几张碟的第几轨"
            tracks = alb.get("tracks", [])
            mine = next((t for t in tracks if t["mid"] == meta["mid"]), None)
            if mine:
                same_disc = [t for t in tracks if t["cd"] == mine["cd"]]
                if all(t["index"] > 0 for t in same_disc):
                    same_disc.sort(key=lambda t: t["index"])
                meta["track"] = next(i for i, t in enumerate(same_disc, 1) if t["mid"] == meta["mid"])
                meta["track_total"] = len(same_disc)
                meta["disc"] = mine["cd"] + 1
                meta["disc_total"] = max(t["cd"] for t in tracks) + 1
            elif (ti.get("index_album") or 0) > 0:
                meta["track"] = ti["index_album"]
                meta["disc"] = (ti.get("index_cd") or 0) + 1
        if not meta["album_artists"]:
            meta["album_artists"] = meta["artists"]
        return meta

    def _album_info(self, album_mid: str) -> dict:
        if album_mid in self._album_cache:
            return self._album_cache[album_mid]

        r = self._musics(
            album={"module": "music.musichallAlbum.AlbumInfoServer", "method": "GetAlbumDetail",
                   "param": {"albumMid": album_mid}},
            songs={"module": "music.musichallAlbum.AlbumSongList", "method": "GetAlbumSongList",
                   "param": {"albumMid": album_mid, "begin": 0, "num": 100, "order": 2}},
        )
        result: dict = {}
        try:
            d = r["album"]["data"]
            basic = d.get("basicInfo") or {}
            result["name"] = (basic.get("albumName") or "").replace(" ", " ")
            result["artists"] =[s["name"] for s in (d.get("singer") or {}).get("singerList", []) if s.get("name")]
            result["date"] = _clean_date(basic.get("publishDate"))
            result["label"] = (d.get("company") or {}).get("name", "")
            result["genre"] = basic.get("genre", "")
        except (KeyError, TypeError, AttributeError):
            pass

        try:
            sd = r["songs"]["data"]
            items = list(sd.get("songList") or [])
            total = sd.get("totalNum") or len(items)
            while len(items) < total:
                more = self._musics(songs={
                    "module": "music.musichallAlbum.AlbumSongList", "method": "GetAlbumSongList",
                    "param": {"albumMid": album_mid, "begin": len(items), "num": 100, "order": 2},
                })["songs"]["data"].get("songList") or []
                if not more:
                    break
                items += more
            result["tracks"] = [
                {"mid": si["mid"], "cd": si.get("index_cd") or 0, "index": si.get("index_album") or 0,
                 "name": (si.get("title") or si.get("name") or "").replace(" ", " "),
                 "singer": ", ".join(s.get("name", "") for s in si.get("singer") or []),
                 "duration": si.get("interval", 0)}
                for si in (it.get("songInfo") or {} for it in items) if si.get("mid")
            ]
        except (KeyError, TypeError, AttributeError):
            pass

        self._album_cache[album_mid] = result
        return result

    def search_albums(self, keyword: str, num: int = 10) -> list[dict]:
        """搜索专辑（同一个签名搜索接口，search_type=2）"""
        data = self._search_payload(keyword, 1, num)
        data["req_1"]["param"]["search_type"] = 2
        result = self._request_fcg(data, use_sign=True)
        try:
            items = result["req_1"]["data"]["body"]["album"]["list"]
        except (KeyError, TypeError):
            return []
        return [{
            "mid": a.get("albumMID", ""),
            "name": a.get("albumName", "").replace(" ", " "),
            "singer": a.get("singerName") or ", ".join(s.get("name", "") for s in a.get("singer_list") or []),
            "date": a.get("publicTime", ""),
            "count": a.get("song_count", 0),
        } for a in items if a.get("albumMID")]

    def get_album_songs(self, album_mid: str) -> tuple[str, list[dict]]:
        """专辑名 + 按"第几张碟、第几轨"排好序的曲目（和搜索结果同样的字段）"""
        info = self._album_info(album_mid)
        tracks = sorted(info.get("tracks", []), key=lambda t: (t["cd"], t["index"]))
        songs = [{"mid": t["mid"], "name": t["name"], "singer": t["singer"], "album": info.get("name", ""),
                  "album_mid": album_mid, "duration": t["duration"]} for t in tracks]
        return info.get("name", ""), songs

    def fetch_cover(self, meta: dict) -> dict | None:
        """
        取最清晰的可用封面：专辑原图 → 1200 → 800；没有专辑封面时按 QQ 音乐的做法用歌手图。
        每张图都完整解码检查过，不合格（解不开、太小、不是方图、太大）就换下一档。
        """
        album_mid = meta.get("album_mid") or ""
        singer_mid = next(iter(meta.get("singer_mids") or []), "")
        cache_key = album_mid or f"singer:{singer_mid}"
        if cache_key in self._cover_cache:
            return self._cover_cache[cache_key]

        candidates = []
        if album_mid:
            candidates += [(f"T002M000{album_mid}", "专辑原图"), (f"T002R1200x1200M000{album_mid}", "专辑 1200"),
                           (f"T002R800x800M000{album_mid}", "专辑 800")]
        if singer_mid:
            candidates += [(f"T001M000{singer_mid}", "歌手图原图"), (f"T001R1200x1200M000{singer_mid}", "歌手图 1200")]

        cover = None
        for name, source in candidates:
            try:
                resp = self.cm.session.get(f"https://y.gtimg.cn/music/photo_new/{name}.jpg", timeout=30)
            except requests.RequestException:
                continue
            if resp.status_code != 200 or not resp.content:
                continue
            cover = _prepare_cover(resp.content)
            if cover:
                cover["source"] = source
                break

        self._cover_cache[cache_key] = cover
        return cover

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
    """下载文件，带进度条。HTTP 出错或字节数和 Content-Length 对不上都会抛异常"""
    resp = requests.get(url, stream=True, headers={"User-Agent": UA, "Referer": REFERER}, timeout=60)
    resp.raise_for_status()
    total = int(resp.headers.get("Content-Length", 0))

    output_path.parent.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    try:
        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=32768):
                f.write(chunk)
                downloaded += len(chunk)
                if show_progress and total > 0:
                    pct = downloaded / total * 100
                    bar = "█" * int(pct / 2) + "░" * (50 - int(pct / 2))
                    print(f"\r  下载中 [{bar}] {pct:.1f}%", end="", flush=True)
    finally:
        # 中途断线也要换行，否则后面的报错会和进度条挤在同一行，被过滤进度条的人一起滤掉
        if show_progress:
            print()
    if total and downloaded != total:
        raise IOError(f"只收到 {downloaded} / {total} 字节")


# ─── 元数据 / 封面 / 校验 ───────────────────────────────────────────────

def _clean_date(value) -> str:
    """QQ 的日期偶尔是 0000-00-00 之类的占位，只保留有效的 年 / 年-月 / 年-月-日"""
    parts = str(value or "").strip()[:10].split("-")
    if not parts or not re.fullmatch(r"\d{4}", parts[0]) or parts[0] == "0000":
        return ""
    keep = [parts[0]]
    for p in parts[1:3]:
        if not re.fullmatch(r"\d{2}", p) or p == "00":
            break
        keep.append(p)
    return "-".join(keep)


def _decode_lyric(resp) -> str:
    """GetPlayLyricInfo 返回的是 base64 的 LRC；crypt=1 时是加密的 QRC，跳过"""
    try:
        d = resp["data"]
        if d.get("crypt") or not d.get("lyric"):
            return ""
        return html.unescape(base64.b64decode(d["lyric"]).decode("utf-8")).strip()
    except Exception:
        return ""


def inspect_image(data: bytes) -> dict | None:
    """完整解码一遍图片（截断、损坏的图在这里会失败），返回格式和尺寸"""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            return {"format": im.format, "width": im.width, "height": im.height, "mode": im.mode}
    except Exception:
        return None


def _prepare_cover(data: bytes) -> dict | None:
    """检查一张候选封面，合格就返回写标签要用的信息，不合格返回 None"""
    info = inspect_image(data)
    if not info:
        return None
    w, h = info["width"], info["height"]
    if min(w, h) < COVER_MIN_SIDE or not 0.8 <= w / h <= 1.25:
        return None
    if max(w, h) > COVER_MAX_SIDE or len(data) > COVER_MAX_BYTES:
        return None
    if info["format"] not in ("JPEG", "PNG"):
        # 极少数接口给的是 WebP 等格式，很多播放器不认，转成 JPEG
        from PIL import Image
        buf = io.BytesIO()
        with Image.open(io.BytesIO(data)) as im:
            im.convert("RGB").save(buf, "JPEG", quality=95)
        data, info = buf.getvalue(), inspect_image(buf.getvalue())
        if not info:
            return None
    return {
        "data": data,
        "mime": "image/png" if info["format"] == "PNG" else "image/jpeg",
        "format": info["format"],
        "width": info["width"],
        "height": info["height"],
        "depth": 32 if info["mode"] in ("RGBA", "LA") else 24,
    }


def _tag_fields(meta: dict) -> dict:
    """各格式共用的标签内容（空值不写）"""
    return {
        "title": meta.get("title") or "",
        "artist": ", ".join(meta.get("artists") or []),
        "album": meta.get("album") or "",
        "album_artist": ", ".join(meta.get("album_artists") or []),
        "date": meta.get("date") or "",
        "genre": meta.get("genre") or "",
        "label": meta.get("label") or "",
        "lyrics": meta.get("lyrics") or "",
    }


def _extra_tags(meta: dict) -> dict:
    """专辑版本（只有全景声版有）和曲目 ReplayGain"""
    extra = {}
    if meta.get("album_version"):
        extra["ALBUMVERSION"] = meta["album_version"]
        # Navidrome 按「专辑艺人 + 专辑名 + 版本 + 发行日期」认专辑。M4A 的 ©day 它会同时当发行日期，
        # FLAC / MP3 的日期不会，混在一张全景声专辑里就会被拆成两张，所以这里显式写上
        if meta.get("date"):
            extra["RELEASEDATE"] = meta["date"]
    if meta.get("replaygain"):
        gain, peak = meta["replaygain"]
        extra["REPLAYGAIN_TRACK_GAIN"] = f"{gain:+.2f} dB"
        extra["REPLAYGAIN_TRACK_PEAK"] = f"{peak:.6f}"
    return extra


# ─── AI 歌词时间轴 ───────────────────────────────────────────────────────

_gemini_key_cache: list = []


def gemini_key() -> str | None:
    """环境变量 GEMINI_API_KEY，或 macOS 钥匙串里名为 gemini-api-key 的条目。都没有返回 None"""
    if not _gemini_key_cache:
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key and shutil.which("security"):
            r = subprocess.run(["security", "find-generic-password", "-s", "gemini-api-key", "-w"],
                               capture_output=True, text=True)
            key = r.stdout.strip() if r.returncode == 0 else ""
        _gemini_key_cache.append(key or None)
    return _gemini_key_cache[0]


_LRC_TIME = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\]")


def lyrics_timed(text: str) -> bool:
    """一半以上的歌词行带 [mm:ss] 就算已经有时间轴"""
    lines = [l.strip() for l in text.splitlines() if l.strip() and not re.match(r"^\[[a-z]+:", l.strip())]
    return bool(lines) and sum(1 for l in lines if _LRC_TIME.match(l)) * 2 >= len(lines)


_ALIGN_PROMPT = """完整听一遍这段音频。下面是这首歌的歌词，每行一句，顺序就是演唱顺序：

<lyrics>
{lyrics}
</lyrics>

任务：给每一行歌词标上这一句人声开始唱出的时间，输出 LRC。
要求：
- 时间格式 [mm:ss.xx]（精确到百分之一秒），时间是这一句第一个字发声的时刻，不是前奏或上一句结尾。
- 每行歌词原样保留、一行不多一行不少、顺序不变；时间必须单调递增。
- 第一行如果是「歌名 - 歌手」这种标题行，或者是作词作曲之类的署名行，时间写 [00:00.00]。
- 只输出 LRC 正文，不要任何解释、代码块标记或别的文字。"""

_TRANSCRIBE_PROMPT = """完整听一遍这段音频，听写出全部人声歌词，并给每一句标上开始唱的时间，输出 LRC。
要求：
- 时间格式 [mm:ss.xx]，是这一句第一个字发声的时刻；时间单调递增。
- 按自然乐句断行，一行一句；重复的段落也要按实际演唱次数写全。
- 听不清的词按最可能的写，不要写「听不清」之类的说明；纯音乐段不写。
- 只输出 LRC 正文，不要任何解释、代码块标记或别的文字。"""


def ai_time_lyrics(path: Path, plain: str, duration: float) -> tuple[str | None, str]:
    """
    plain 非空：给这份纯文本歌词对时间；为空：从零听写。
    返回 (LRC 或 None, 说明)。歌词行必须和原文一字不差、时间单调递增、不超过歌曲时长，否则重试一次后放弃
    """
    key = gemini_key()
    if not key:
        return None, "没有 Gemini API key"
    if not shutil.which("ffmpeg"):
        return None, "没装 ffmpeg"
    # 转成 128k MP3 再传：无损文件太大，请求体有上限
    tmp = path.with_name(f"{path.stem}.ai-lyrics.mp3")
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path), "-map", "0:a:0", "-ac", "2",
                        "-b:a", "128k", str(tmp)], check=True)
        audio = base64.b64encode(tmp.read_bytes()).decode()
    except Exception as e:
        return None, f"转码失败: {e}"
    finally:
        tmp.unlink(missing_ok=True)
    want = [l.strip() for l in plain.splitlines() if l.strip()]
    prompt = _ALIGN_PROMPT.format(lyrics="\n".join(want)) if want else _TRANSCRIBE_PROMPT
    body = {"contents": [{"role": "user", "parts": [{"inlineData": {"mimeType": "audio/mpeg", "data": audio}},
                                                    {"text": prompt}]}],
            "generationConfig": {"thinkingConfig": {"thinkingLevel": "medium"}}}
    problem = ""
    for _ in (1, 2):
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                              headers={"x-goog-api-key": key}, json=body, timeout=600)
            if r.status_code != 200:
                problem = f"Gemini 返回 HTTP {r.status_code}"
                continue
            parts = r.json()["candidates"][0]["content"].get("parts", [])
        except Exception as e:
            problem = f"请求 Gemini 失败: {e}"
            continue
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        lines = [l.strip() for l in text.strip().strip("`").splitlines() if l.strip()]
        stamps = [_LRC_TIME.match(l) for l in lines]
        if not lines or not all(stamps):
            problem = "返回的不是 LRC"
            continue
        secs = [int(m[1]) * 60 + float(m[2]) for m in stamps]
        if any(b < a for a, b in zip(secs, secs[1:])):
            problem = "时间不是单调递增"
            continue
        if duration and secs[-1] > duration + 1:
            problem = "时间超出了歌曲长度"
            continue
        if want and [_LRC_TIME.sub("", l).strip() for l in lines] != want:
            problem = "返回的歌词行和原文对不上"
            continue
        return "\n".join(lines), (f"Gemini 对出 {len(lines)} 行时间轴" if want else f"Gemini 听写出 {len(lines)} 行")
    return None, problem


def _maybe_time_lyrics(path: Path, meta: dict) -> dict:
    """下载流程里用：纯文本歌词补时间轴；没歌词且开了 --transcribe-lyrics 就听写。失败保留原歌词"""
    lyr = meta.get("lyrics") or ""
    if not AI_LYRICS["enabled"] or not gemini_key() or lyrics_timed(lyr):
        return meta
    if not lyr and not AI_LYRICS["transcribe"]:
        return meta
    timed, how = ai_time_lyrics(path, lyr, float(meta.get("duration") or 0))
    if timed:
        print(f"  🎤 {how}")
        return {**meta, "lyrics": timed}
    print(f"  ⚠️ 歌词时间轴没做成（{how}），{'保留纯文本歌词' if lyr else '不写歌词'}")
    return meta


def _read_lyrics(path: Path) -> str:
    import mutagen
    tags = mutagen.File(path).tags or {}
    ext = path.suffix.lower()
    if ext == ".mp3":
        uslt = tags.getall("USLT")
        return uslt[0].text if uslt else ""
    for k in ("LYRICS", "lyrics", "UNSYNCEDLYRICS", "\xa9lyr", "Lyrics"):
        if k in tags:
            v = tags[k]
            v = v[0] if isinstance(v, list) else v
            return str(v)
    return ""


def _write_lyrics(path: Path, text: str):
    """只改歌词这一个标签"""
    ext = path.suffix.lower()
    if ext == ".flac":
        from mutagen.flac import FLAC
        audio = FLAC(path); audio["LYRICS"] = text; audio.save()
    elif ext == ".mp3":
        from mutagen.id3 import ID3, USLT, Encoding
        tags = ID3(path)
        tags.setall("USLT", [USLT(encoding=Encoding.UTF16, lang="XXX", desc="", text=text)])
        tags.save(path, v1=0, v2_version=3, v23_sep=None)
    elif ext == ".m4a":
        from mutagen.mp4 import MP4
        audio = MP4(path); audio["\xa9lyr"] = [text]; audio.save()
    elif ext == ".ape":
        from mutagen.apev2 import APEv2
        tags = APEv2(path); tags["Lyrics"] = text; tags.save(path)


def time_lyrics_in_files(target: Path, transcribe: bool) -> list[str]:
    """lyrics 命令：给已下载的文件（或整个文件夹）补歌词时间轴。只改歌词标签，改完回读确认"""
    import mutagen
    files = sorted(p for p in ([target] if target.is_file() else target.rglob("*"))
                   if p.suffix.lower() in (".flac", ".mp3", ".m4a", ".ape") and ".part" not in p.name)
    results = []
    for i, path in enumerate(files, 1):
        print(f"\n[{i}/{len(files)}] {path.name}")
        lyr = _read_lyrics(path)
        if lyrics_timed(lyr):
            print("  ⏭️  已经有时间轴")
            results.append("skip"); continue
        if not lyr and not transcribe:
            print("  ⏭️  没有歌词（加 --transcribe-lyrics 让 Gemini 从零听写）")
            results.append("skip"); continue
        timed, how = ai_time_lyrics(path, lyr, mutagen.File(path).info.length)
        if not timed:
            print(f"  ❌ {how}")
            results.append("fail"); continue
        part = path.with_name(f"{path.stem}.part{path.suffix}")
        shutil.copy2(path, part)  # 在副本上改，回读一致再换回去
        try:
            _write_lyrics(part, timed)
            if _read_lyrics(part).strip() != timed.strip():
                raise ValueError("歌词回读不一致")
        except Exception as e:
            part.unlink(missing_ok=True)
            print(f"  ❌ 写歌词失败: {e}")
            results.append("fail"); continue
        part.replace(path)
        print(f"  🎤 {how}，已写入")
        results.append("ok")
    return results


def measure_replaygain(path: Path) -> tuple[float, float] | None:
    """用 ffmpeg 的 EBU R128 量整首响度，换算成 ReplayGain 2.0 的增益和采样峰值。没装 ffmpeg 返回 None"""
    if not shutil.which("ffmpeg"):
        return None
    measured = _loudness(["-i", str(path), "-af", "ebur128=peak=sample", "-f", "null", "-"])
    return (REPLAYGAIN_REFERENCE - measured[0], measured[1]) if measured else None


def _loudness(args: list[str]) -> tuple[float, float] | None:
    """跑一遍 ffmpeg ebur128，返回 (整体响度 LUFS, 采样峰值线性值)"""
    out = subprocess.run(["ffmpeg", "-nostats", "-hide_banner", *args], capture_output=True, text=True,
                         errors="replace").stderr
    summary = out[out.rfind("Summary:"):]
    loud = re.search(r"I:\s+(-?[\d.]+) LUFS", summary)
    peak = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", summary)
    if not loud:
        return None
    peak_db = float(peak.group(1)) if peak and peak.group(1) != "-inf" else 0.0
    return float(loud.group(1)), 10 ** (peak_db / 20)


def _write_replaygain(path: Path, tags: dict):
    """只改 ReplayGain 这几个标签，其他标签和音频不动"""
    ext = path.suffix.lower()
    if ext == ".flac":
        from mutagen.flac import FLAC
        audio = FLAC(path)
        for key, value in tags.items():
            audio[key] = value
        audio.save()
    elif ext == ".mp3":
        from mutagen.id3 import ID3, TXXX, Encoding
        id3 = ID3(path)
        for key, value in tags.items():
            id3.setall(f"TXXX:{key}", [TXXX(encoding=Encoding.UTF16, desc=key, text=value)])
        id3.save(path, v1=0, v2_version=3, v23_sep=None)  # 和 embed_metadata 一样存成 v2.3
    elif ext == ".m4a":
        from mutagen.mp4 import MP4, MP4FreeForm
        audio = MP4(path)
        for key, value in tags.items():
            audio[f"----:com.apple.iTunes:{key}"] = [MP4FreeForm(value.encode("utf-8"))]
        audio.save()
    elif ext == ".ape":
        from mutagen.apev2 import APEv2
        ape = APEv2(path)
        for key, value in tags.items():
            ape[key] = value
        ape.save(path)


def _read_tag(path: Path, key: str) -> str:
    import mutagen
    tags = mutagen.File(path).tags or {}
    for k in (key, f"TXXX:{key}", f"----:com.apple.iTunes:{key}"):
        if k in tags:
            v = tags[k]
            v = v.text[0] if hasattr(v, "text") else v  # MP3 的 TXXX 帧
            v = v[0] if isinstance(v, list) else v
            return v.decode("utf-8") if isinstance(v, bytes) else str(v)  # MP4 自定义标签读回来是 bytes
    return ""


def write_album_replaygain(paths: list[Path]) -> bool:
    """
    给一整张立体声专辑写专辑增益：把所有曲目统一成 48 kHz 立体声接起来量一次响度，峰值取各首最大值。
    曲目增益也按同一次测量重写一遍，已存在的旧文件也能补上。
    全景声版不调用这个：杜比和臻品全景声响度差约 10 dB，用一个专辑增益拉不平。
    """
    if not shutil.which("ffmpeg"):
        print("  ⚠️ 没装 ffmpeg，不写专辑增益")
        return False
    tracks = [_loudness(["-i", str(p), "-map", "0:a:0", "-af", "ebur128=peak=sample", "-f", "null", "-"])
              for p in paths]
    ins = [a for p in paths for a in ("-i", str(p))]
    chains = [f"[{n}:a:0]aresample=48000,aformat=sample_fmts=flt:channel_layouts=stereo[a{n}]"
              for n in range(len(paths))]
    graph = ";".join(chains) + ";" + "".join(f"[a{n}]" for n in range(len(paths))) + \
        f"concat=n={len(paths)}:v=0:a=1,ebur128=peak=sample[out]"
    album = _loudness([*ins, "-filter_complex", graph, "-map", "[out]", "-f", "null", "-"])
    if album is None or None in tracks:
        print("  ⚠️ 量不出响度，不写专辑增益")
        return False
    album_gain = f"{REPLAYGAIN_REFERENCE - album[0]:+.2f} dB"
    album_peak = f"{max(t[1] for t in tracks):.6f}"
    for path, (loud, peak) in zip(paths, tracks):
        tags = {
            "REPLAYGAIN_TRACK_GAIN": f"{REPLAYGAIN_REFERENCE - loud:+.2f} dB",
            "REPLAYGAIN_TRACK_PEAK": f"{peak:.6f}",
            "REPLAYGAIN_ALBUM_GAIN": album_gain,
            "REPLAYGAIN_ALBUM_PEAK": album_peak,
        }
        part = path.with_name(f"{path.stem}.part{path.suffix}")
        shutil.copy2(path, part)  # 在副本上改，回读一致再换回去，改坏了原文件还在
        try:
            _write_replaygain(part, tags)
            if any(_read_tag(part, k) != v for k, v in tags.items()):
                raise ValueError("回读不一致")
        except Exception as e:
            part.unlink(missing_ok=True)
            print(f"  ❌ {path.name} 写专辑增益失败: {e}")
            return False
        part.replace(path)
    print(f"  🔊 专辑响度 {album[0]:.1f} LUFS，专辑增益 {album_gain}，{len(paths)} 首都已写入")
    return True


def embed_metadata(path: Path, ext: str, meta: dict, cover: dict | None):
    """把标签和封面写进音频文件。保留文件里原有的其他标签（比如 M4A 的无缝播放信息）"""
    f = _tag_fields(meta)
    track, track_total = meta.get("track"), meta.get("track_total")
    disc, disc_total = meta.get("disc"), meta.get("disc_total")
    artists = meta.get("artists") or []

    if ext == ".flac":
        from mutagen.flac import FLAC, Picture
        audio = FLAC(path)
        vorbis = {
            "TITLE": f["title"], "ARTIST": f["artist"], "ALBUM": f["album"], "ALBUMARTIST": f["album_artist"],
            "DATE": f["date"], "GENRE": f["genre"], "LABEL": f["label"], "LYRICS": f["lyrics"],
            "TRACKNUMBER": track, "TRACKTOTAL": track_total, "TOTALTRACKS": track_total,
            "DISCNUMBER": disc, "DISCTOTAL": disc_total, "TOTALDISCS": disc_total,
        }
        for key, value in vorbis.items():
            if value:
                audio[key] = str(value)
        for key, value in _extra_tags(meta).items():
            audio[key] = value
        if len(artists) > 1:
            audio["ARTISTS"] = artists  # 多值字段，Navidrome / Picard 等按这个拆分歌手
        elif "ARTISTS" in audio:
            del audio["ARTISTS"]
        if cover:
            pic = Picture()
            pic.type = 3  # front cover
            pic.mime = cover["mime"]
            pic.width, pic.height, pic.depth = cover["width"], cover["height"], cover["depth"]
            pic.data = cover["data"]
            audio.clear_pictures()
            audio.add_picture(pic)
        audio.save(deleteid3=True)  # FLAC 前面夹带的 ID3 头是非标准的，顺手去掉

    elif ext == ".mp3":
        from mutagen.id3 import (ID3, APIC, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TPUB, TRCK, TXXX, USLT,
                                 Encoding, ID3NoHeaderError)
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        enc = Encoding.UTF16  # 存成 ID3v2.3，v2.3 不支持 UTF-8，用 UTF-16 才不会乱码
        for frame_cls, value in ((TIT2, f["title"]), (TPE1, f["artist"]), (TALB, f["album"]),
                                 (TPE2, f["album_artist"]), (TDRC, f["date"]), (TCON, f["genre"]),
                                 (TPUB, f["label"])):
            if value:
                tags.setall(frame_cls.__name__, [frame_cls(encoding=enc, text=value)])
        if track:
            tags.setall("TRCK", [TRCK(encoding=enc, text=f"{track}/{track_total}" if track_total else str(track))])
        if disc:
            tags.setall("TPOS", [TPOS(encoding=enc, text=f"{disc}/{disc_total}" if disc_total else str(disc))])
        if len(artists) > 1:
            # TPE1 是给人看的「A, B」，Navidrome 不按逗号拆；多值的 TXXX:ARTISTS 它优先用来拆歌手
            tags.setall("TXXX:ARTISTS", [TXXX(encoding=enc, desc="ARTISTS", text=artists)])
        else:
            tags.delall("TXXX:ARTISTS")
        if f["lyrics"]:
            tags.setall("USLT", [USLT(encoding=enc, lang="XXX", desc="", text=f["lyrics"])])
        for key, value in _extra_tags(meta).items():
            tags.setall(f"TXXX:{key}", [TXXX(encoding=enc, desc=key, text=value)])
        if cover:
            tags.setall("APIC", [APIC(encoding=enc, mime=cover["mime"], type=3, desc="", data=cover["data"])])
        # v1=0: 删掉 ID3v1（老 MP3 里常是 GBK 编码，最容易乱码）
        # v23_sep=None: 多值用 \0 分隔而不是「/」，否则 ARTISTS 读回来是一个「A/B」
        tags.save(path, v1=0, v2_version=3, v23_sep=None)

    elif ext == ".m4a":
        from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
        audio = MP4(path)
        if audio.tags is None:
            audio.add_tags()
        for key, value in (("\xa9nam", f["title"]), ("\xa9ART", f["artist"]), ("\xa9alb", f["album"]),
                           ("aART", f["album_artist"]), ("\xa9day", f["date"]), ("\xa9gen", f["genre"]),
                           ("\xa9lyr", f["lyrics"])):
            if value:
                audio[key] = [value]
        if f["label"]:
            audio["----:com.apple.iTunes:LABEL"] = [MP4FreeForm(f["label"].encode("utf-8"))]
        for key, value in _extra_tags(meta).items():
            audio[f"----:com.apple.iTunes:{key}"] = [MP4FreeForm(value.encode("utf-8"))]
        if len(artists) > 1:
            audio["----:com.apple.iTunes:ARTISTS"] = [MP4FreeForm(a.encode("utf-8")) for a in artists]
        else:
            audio.pop("----:com.apple.iTunes:ARTISTS", None)
        if track:
            audio["trkn"] = [(int(track), int(track_total or 0))]
        if disc:
            audio["disk"] = [(int(disc), int(disc_total or 0))]
        if cover:
            fmt = MP4Cover.FORMAT_PNG if cover["format"] == "PNG" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(cover["data"], imageformat=fmt)]
        audio.save()

    elif ext == ".ape":
        from mutagen.apev2 import APEv2, APEValue, BINARY, APENoHeaderError
        try:
            tags = APEv2(path)
        except APENoHeaderError:
            tags = APEv2()
        for key, value in (("Title", f["title"]), ("Artist", f["artist"]), ("Album", f["album"]),
                           ("Album Artist", f["album_artist"]), ("Year", f["date"]), ("Genre", f["genre"]),
                           ("Label", f["label"]), ("Lyrics", f["lyrics"])):
            if value:
                tags[key] = value
        if track:
            tags["Track"] = f"{track}/{track_total}" if track_total else str(track)
        if disc:
            tags["Disc"] = f"{disc}/{disc_total}" if disc_total else str(disc)
        if cover:
            name = b"cover.png" if cover["format"] == "PNG" else b"cover.jpg"
            tags["Cover Art (Front)"] = APEValue(name + b"\x00" + cover["data"], BINARY)
        tags.save(path)


def _read_back(path: Path, ext: str) -> dict:
    """从文件里读回标题、歌手、专辑和封面，用来确认写进去的东西完好"""
    import mutagen
    audio = mutagen.File(path)
    tags = audio.tags or {}

    def first(key):
        v = tags.get(key)
        if v is None:
            return ""
        if hasattr(v, "text"):  # ID3 frame
            return str(v.text[0]) if v.text else ""
        if isinstance(v, list):
            return str(v[0]) if v else ""
        return str(v)

    if ext == ".flac":
        out = {"title": first("title"), "artist": first("artist"), "album": first("album"),
               "cover": audio.pictures[0].data if audio.pictures else None}
    elif ext == ".mp3":
        apic = tags.getall("APIC")
        out = {"title": first("TIT2"), "artist": first("TPE1"), "album": first("TALB"),
               "cover": apic[0].data if apic else None}
    elif ext == ".m4a":
        covr = tags.get("covr")
        out = {"title": first("\xa9nam"), "artist": first("\xa9ART"), "album": first("\xa9alb"),
               "cover": bytes(covr[0]) if covr else None}
    else:
        art = tags.get("Cover Art (Front)")
        out = {"title": first("Title"), "artist": first("Artist"), "album": first("Album"),
               "cover": art.value.split(b"\x00", 1)[1] if art else None}
    out["length"] = audio.info.length
    return out


def _decode_check(path: Path, audio, expected: float) -> tuple[str, str] | None:
    """
    用 ffmpeg 把音频完整解码一遍，返回 (问题描述, 用了什么检查)，问题描述为空串表示没问题；没装 ffmpeg 返回 None。

    FLAC 自带整段音频的 MD5，解码后比对它就能确认逐位无损（等同 `flac -t`）。
    不看 ffmpeg 的报错：QQ 的部分 FLAC 会让 ffmpeg 的解析器在曲尾误报 "invalid sync code"，
    但解码结果和 MD5 完全一致。其他格式没有内置校验值，看解码报错 + 实际解码出的时长
    （截断的 MP3 头里的 Xing 帧照样写着完整时长，ffmpeg 解到结尾也不报错，只能数解出来的采样）。
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    info = audio.info
    raw_fmt = {8: "s8", 16: "s16le", 24: "s24le", 32: "s32le"}.get(getattr(info, "bits_per_sample", 0))
    if getattr(info, "md5_signature", 0) and raw_fmt:
        p = subprocess.Popen([ffmpeg, "-nostdin", "-v", "quiet", "-i", str(path), "-map", "0:a", "-f", raw_fmt, "-"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        md5, size = hashlib.md5(), 0
        for chunk in iter(lambda: p.stdout.read(1 << 20), b""):
            md5.update(chunk)
            size += len(chunk)
        p.wait()
        samples = size // (info.channels * info.bits_per_sample // 8)
        if samples != info.total_samples:
            return f"只解码出 {samples} 个采样，文件头记录的是 {info.total_samples} 个", "FLAC MD5"
        if md5.digest() != info.md5_signature.to_bytes(16, "big"):
            return "解码出的音频和 FLAC 自带的 MD5 对不上", "FLAC MD5"
        return "", "解码后与 FLAC 自带 MD5 一致（逐位无损）"

    # 降到 8 kHz 单声道 16 bit 只为数时长，每秒 16000 字节
    p = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a",
                        "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True)
    errors = p.stderr.decode("utf-8", "replace").strip()
    if p.returncode != 0 or errors:
        return (errors.splitlines()[0] if errors else f"ffmpeg 退出码 {p.returncode}")[:200], "ffmpeg 解码"
    decoded = len(p.stdout) / 16000
    target = expected or info.length
    if decoded < target - 3:
        return f"只解码出 {decoded:.0f} 秒，应为 {target:.0f} 秒", "ffmpeg 解码"
    return "", "ffmpeg 完整解码无报错、时长完整"


def check_audio(path: Path, meta: dict) -> tuple[str, str]:
    """
    检查音频本身：能被识别、时长对得上（不是试听片段）、能完整解码。
    返回 (问题描述, 做了哪种解码检查)，问题描述为空串表示没问题。
    """
    import mutagen
    try:
        audio = mutagen.File(path)
    except Exception as e:
        return f"无法解析音频: {e}", ""
    if audio is None:
        return "无法识别的音频格式（可能下载到的是错误页面）", ""
    expected = meta.get("duration") or 0
    if expected and audio.info.length < expected - 3:
        return f"时长只有 {audio.info.length:.0f} 秒，应为 {expected} 秒（可能是试听片段或文件不完整）", ""
    decoded = _decode_check(path, audio, expected)
    if decoded is None:
        return "", "未装 ffmpeg，跳过完整解码"
    problem, how = decoded
    return (f"音频解码出错: {problem}" if problem else ""), how


def verify_tags(path: Path, ext: str, meta: dict, cover: dict | None) -> list[str]:
    """回读标签和封面，确认和写进去的一致"""
    problems = []
    try:
        got = _read_back(path, ext)
    except Exception as e:
        return [f"写完标签后文件读不出来: {e}"]
    f = _tag_fields(meta)
    for key, label in (("title", "歌名"), ("artist", "歌手"), ("album", "专辑")):
        if f[key] and got[key] != f[key]:
            problems.append(f"{label}标签回读不一致: {got[key]!r}")
    if cover:
        if got["cover"] != cover["data"]:
            problems.append("封面没有完整写进文件")
        elif not inspect_image(got["cover"]):
            problems.append("文件里的封面解码失败")
    return problems


def _meta_summary(meta: dict, cover: dict | None) -> str:
    parts = [meta["album"] or "无专辑"]
    if meta.get("track"):
        parts.append(f"第 {meta['track']}/{meta['track_total']} 轨" if meta.get("track_total") else f"第 {meta['track']} 轨")
    if (meta.get("disc_total") or 0) > 1:
        parts.append(f"第 {meta['disc']}/{meta['disc_total']} 张碟")
    parts += [x for x in (meta.get("date"), meta.get("genre")) if x]
    parts.append("歌词 ✓" if meta.get("lyrics") else "无歌词")
    parts.append(f"封面 {cover['width']}×{cover['height']} {cover['format']}（{cover['source']}）" if cover else "无封面")
    return " · ".join(parts)


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


def song_path(meta: dict, ext: str, layout: str) -> Path:
    """
    flat：  歌手 - 歌名.flac
    album： 专辑名/01 歌名.flac（多碟专辑是 1-01 歌名.flac）；没有专辑或音轨号时退回 歌手 - 歌名
    """
    title = meta["title"]
    singer = ", ".join(meta["artists"])
    if layout == "album" and meta.get("album"):
        track, disc = meta.get("track"), meta.get("disc") or 1
        if not track:
            stem = f"{singer} - {title}"
        elif (meta.get("disc_total") or 1) > 1:
            stem = f"{disc}-{track:02d} {title}"
        else:
            stem = f"{track:02d} {title}"
        return DOWNLOAD_DIR / sanitize_filename(meta["album"]) / sanitize_filename(f"{stem}{ext}")
    return DOWNLOAD_DIR / sanitize_filename(f"{singer} - {title}{ext}")


def download_song(api: QQMusicAPI, song: dict, quality: str, layout: str = "flat",
                  saved: list[Path] | None = None) -> str:
    """
    下载单首歌曲：下到 .part 临时文件 → 检查音频 → 写标签和封面 → 回读校验 → 改成正式文件名。
    任何一步不过关都不会留下正式文件，所以"已存在就跳过"跳过的一定是完整的文件。
    返回 "ok" / "skip" / "fail"。传了 saved 时，下好或已存在的文件路径会加进去。
    """
    mid = song["mid"]
    meta = api.get_song_meta(mid)
    if not meta:
        print("  ⚠️ 拿不到歌曲详情，只能写入搜索结果里的基本信息")
        meta = {
            "mid": mid, "title": song["name"], "artists": [song["singer"]], "singer_mids": [],
            "album": song.get("album", ""), "album_mid": song.get("album_mid", ""), "album_artists": [song["singer"]],
            "track": None, "track_total": None, "disc": None, "disc_total": None,
            "date": "", "genre": "", "label": "", "lyrics": "", "duration": song.get("duration", 0),
        }
    meta["title"] = meta["title"] or song["name"]
    meta["artists"] = meta["artists"] or [song["singer"]]
    name, singer = meta["title"], ", ".join(meta["artists"])

    candidates = BEST_ORDER if quality == "best" else [quality]
    for q in candidates:
        existing = song_path(meta, QUALITY_MAP[q]["ext"], layout)
        if existing.exists() and existing.stat().st_size > 0:
            print(f"  ⏭️  已存在: {existing.relative_to(DOWNLOAD_DIR)}")
            if saved is not None:
                saved.append(existing)
            return "skip"

    print(f"  🎵 {singer} - {name} [{quality_label(quality)}]")

    url = None
    for q in candidates:
        url = api.get_song_url(mid, q, quiet=len(candidates) > 1)
        if url:
            break
    if not url:
        if len(candidates) > 1:
            print(f"  ⚠️ {api.last_url_problem}")
        return "fail"
    quality_info = QUALITY_MAP[q]
    if len(candidates) > 1:
        print(f"  🎚️  音质：{quality_info['label']}")
    output_path = song_path(meta, quality_info["ext"], layout)
    result = _save_track(api, url, output_path, meta)
    if result == "ok" and saved is not None:
        saved.append(output_path)
    return result


def _save_track(api: QQMusicAPI, url: str, output_path: Path, meta: dict) -> str:
    """下到 .part → 检查音频 → 写标签和封面 → 回读校验 → 改成正式文件名。返回 "ok" / "fail" """
    ext = output_path.suffix
    output_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = output_path.with_name(f"{output_path.stem}.part{ext}")

    problem = ""
    for attempt in (1, 2):
        try:
            download_file(url, part_path)
            problem, _ = check_audio(part_path, meta)
        except Exception as e:
            problem = f"下载失败: {e}"
        if not problem:
            break
        if attempt == 1:
            print(f"  ⚠️ {problem}，重新下载一次...")
    if problem:
        print(f"  ❌ {problem}")
        part_path.unlink(missing_ok=True)
        return "fail"

    cover = api.fetch_cover(meta)
    if not cover:
        print("  ⚠️ 没找到合格的封面图（专辑和歌手都没有可用的高清图），文件里不会有封面")

    how = ""
    try:
        meta = {**meta, "replaygain": measure_replaygain(part_path)}
        meta = _maybe_time_lyrics(part_path, meta)
        embed_metadata(part_path, ext, meta, cover)
        problems = verify_tags(part_path, ext, meta, cover)
        after, how = check_audio(part_path, meta)  # 写标签后再验一次，确认没把音频写坏
        if after:
            problems.append(f"写完标签后{after}")
        if meta.get("album_version") and _read_album_version(part_path) != meta["album_version"]:
            problems.append("专辑版本标签回读不一致")
    except Exception as e:
        problems = [f"写标签失败: {e}"]
    if problems:
        print(f"  ❌ 元数据校验没通过: {'; '.join(problems)}")
        part_path.unlink(missing_ok=True)
        return "fail"

    part_path.replace(output_path)
    print(f"  🏷️  {_meta_summary(meta, cover)}")
    print(f"  🔍 校验通过：大小与服务器一致 · 时长 {format_duration(meta['duration'])} · {how} · 标签和封面回读一致")
    print(f"  ✅ 已保存: {output_path}")
    return "ok"


def _read_album_version(path: Path) -> str:
    import mutagen
    tags = mutagen.File(path).tags or {}
    for key in ("ALBUMVERSION", "TXXX:ALBUMVERSION", "----:com.apple.iTunes:ALBUMVERSION"):
        if key in tags:
            v = tags[key]
            v = v[0] if isinstance(v, list) else v
            return v.decode("utf-8") if isinstance(v, bytes) else str(v)  # MP4 自定义标签读回来是 bytes
    return ""


def _remote_exists(url: str) -> bool:
    """取链接接口对没有的文件有时也给链接（比如 RS01），要到 CDN 上确认一下"""
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Referer": REFERER, "Range": "bytes=0-0"}, timeout=15)
        return r.status_code in (200, 206)
    except requests.RequestException:
        return False


def download_spatial_album(api: QQMusicAPI, songs: list[dict]) -> list[str] | None:
    """
    专辑里有杜比全景声时，另存一份完整的全景声版到「专辑名（全景声）/」，标签写 ALBUMVERSION=全景声。
    每首按 杜比全景声 → 臻品全景声 5.1 → 立体声（无损 / 320 / 128）取第一个有的，保证这一版也是整张专辑。
    专辑里一首杜比全景声都没有时返回 None，不存这一版。
    """
    metas = [api.get_song_meta(s["mid"]) for s in songs]
    if not any(m and m["file"].get("size_dolby") for m in metas):
        return None
    results = []
    for i, (song, meta) in enumerate(zip(songs, metas), 1):
        print(f"\n[全景声 {i}/{len(songs)}]", end="")
        results.append(_download_spatial_track(api, song, meta))
    return results


def _download_spatial_track(api: QQMusicAPI, song: dict, meta: dict | None) -> str:
    if not meta:
        print(f"  ❌ 拿不到《{song['name']}》的歌曲详情")
        return "fail"
    meta = {**meta, "album_version": SPATIAL_VERSION}
    folder_meta = {**meta, "album": f"{meta['album']}（{SPATIAL_VERSION}）"}  # 只改文件夹名，ALBUM 标签不变
    sizes = meta["file"]
    candidates = (SPATIAL_DOLBY if sizes.get("size_dolby") else []) + \
                 (["atmos51"] if (sizes.get("size_new") or [0, 0, 0])[2] else []) + BEST_ORDER
    for ext in {(SPATIAL_MAP.get(q) or QUALITY_MAP[q])["ext"] for q in candidates}:
        existing = song_path(folder_meta, ext, "album")
        if existing.exists() and existing.stat().st_size > 0:
            print(f"  ⏭️  已存在: {existing.relative_to(DOWNLOAD_DIR)}")
            return "skip"

    print(f"  🎵 {', '.join(meta['artists'])} - {meta['title']} [全景声版]")
    for q in candidates:
        url = api.get_song_url(meta["mid"], q, quiet=True)
        if url and _remote_exists(url):
            break
    else:
        print("  ⚠️ 这首歌没有任何可下载的音源")
        return "fail"
    info = SPATIAL_MAP.get(q) or QUALITY_MAP[q]
    print(f"  🎚️  音质：{info['label']}" + ("（没有全景声，用立体声补齐）" if q in BEST_ORDER else ""))
    return _save_track(api, url, song_path(folder_meta, info["ext"], "album"), meta)


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
    parser.add_argument("command", nargs="?", default="", help="命令: search/搜索, song/歌曲, album/专辑, playlist/歌单, lyrics/歌词（给本地文件补歌词时间轴）")
    parser.add_argument("query", nargs="?", default="", help="搜索关键词 / songMid / albumMid 或专辑关键词 / 歌单ID")
    parser.add_argument("--quality", "-q", default="320", choices=list(QUALITY_MAP.keys()) + ["best"],
                        help="音质 (128/320/flac/master/ape/m4a；best = 无损优先，没有就退到 320 / 128)")
    parser.add_argument("--layout", choices=["flat", "album"], default=None,
                        help="flat = 歌手 - 歌名.flac；album = 专辑名/01 歌名.flac。album 命令默认 album，其他默认 flat")
    parser.add_argument("--login", action="store_true", help="强制重新登录")
    parser.add_argument("--login-type", choices=["qq", "wx"], default=None,
                        help="扫码登录方式: qq=手机 QQ, wx=微信。默认沿用上次的登录方式，没登录过则用 qq")
    parser.add_argument("--cookie", type=str, help="直接传入 Cookie 字符串")
    parser.add_argument("--index", "-i", type=int, default=None, help="仅下载 search 结果中的第 N 条 (1-based)，用于非交互调用")
    parser.add_argument("--first", action="store_true", help="仅下载 search 结果中的第 1 条，等价于 --index 1")
    parser.add_argument("--output-dir", "-o", type=str, default=None, help="下载输出目录，默认 ~/Music/QQMusic")
    parser.add_argument("--no-ai-lyrics", action="store_true",
                        help="不用 Gemini 给纯文本歌词补时间轴（默认有 API key 就补）")
    parser.add_argument("--transcribe-lyrics", action="store_true",
                        help="没有歌词的歌，让 Gemini 从零听写歌词（需要 API key）")
    parser.add_argument("--spatial", action="store_true",
                        help="album 专用：专辑里有杜比全景声时，另存一份完整的全景声版到「专辑名（全景声）/」")

    args = parser.parse_args()
    AI_LYRICS["enabled"] = not args.no_ai_lyrics
    AI_LYRICS["transcribe"] = args.transcribe_lyrics

    if args.command in ("lyrics", "歌词", "ly"):
        # 只处理本地文件，不需要登录 QQ
        if not args.query:
            print("❌ 请提供音频文件或文件夹路径")
            sys.exit(1)
        if not gemini_key():
            print("❌ 没有 Gemini API key：设环境变量 GEMINI_API_KEY，或存进钥匙串（security add-generic-password -a \"$USER\" -s gemini-api-key -U -w）")
            sys.exit(1)
        results = time_lyrics_in_files(Path(args.query).expanduser(), args.transcribe_lyrics)
        print(f"\n🎤 ✅ 补上 {results.count('ok')} 首 · ⏭️ 跳过 {results.count('skip')} 首 · ❌ 失败 {results.count('fail')} 首")
        sys.exit(2 if "fail" in results else 0)

    if args.output_dir:
        global DOWNLOAD_DIR
        DOWNLOAD_DIR = Path(args.output_dir).expanduser()

    # 初始化
    cm = CookieManager()
    api = QQMusicAPI(cm)
    loaded = cm.load()
    login_type = args.login_type or (cm.login_type if loaded and cm.is_logged_in else "qq")

    def do_login() -> bool:
        if login_type == "wx":
            return WxQrLogin(cm, api).login()
        return QQrLogin(cm).login()

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
    elif args.login or not loaded or not cm.is_logged_in:
        # 需要登录
        if not do_login():
            print("\n❌ 登录失败")
            sys.exit(1)
    else:
        api.ensure_fresh_login()

    if args.login and not args.command:
        # 非交互登录: 不带子命令时登录完成即退出, 不进入交互菜单
        print(f"\n✅ 登录完成, Cookie 已保存: {cm.account_label}")
        sys.exit(0)

    print(f"\n✅ 当前登录: {cm.account_label}")
    print(f"   音质选择: {quality_label(args.quality)}")
    is_album_cmd = args.command in ("album", "专辑", "al")
    layout = args.layout or ("album" if is_album_cmd else "flat")

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
            download_song(api, songs[idx], args.quality, layout)
        else:
            # 未指定 --first / --index 时，默认下载全部结果 (非交互，不会阻塞)
            for song in songs:
                download_song(api, song, args.quality, layout)

    elif args.command in ("song", "歌曲"):
        if not args.query:
            print("❌ 请提供 songMid")
            sys.exit(1)
        song = {"mid": args.query, "name": args.query, "singer": "Unknown", "duration": 0}
        download_song(api, song, args.quality, layout)

    elif is_album_cmd:
        if not args.query:
            print("❌ 请提供 albumMid 或专辑关键词")
            sys.exit(1)
        album_mid = ""
        if re.fullmatch(r"[0-9A-Za-z]{14}", args.query) and api._album_info(args.query).get("tracks"):
            album_mid = args.query
        else:
            albums = api.search_albums(args.query)
            if not albums:
                print("没有找到专辑")
                sys.exit(1)
            print("\n专辑搜索结果:")
            for i, a in enumerate(albums, 1):
                print(f"  {i}. {a['singer']} - {a['name']} · {a['date']} · {a['count']} 首 · {a['mid']}")
            if not (args.first or args.index is not None):
                # 不带序号只列出结果，不下载（一次下 10 张专辑不是想要的）
                print("\n加 --first 或 --index N 下载其中一张，或者 album <albumMid>")
                sys.exit(0)
            idx = 0 if args.first else (args.index - 1)
            if not (0 <= idx < len(albums)):
                print(f"❌ 序号超出范围 (共 {len(albums)} 张)")
                sys.exit(1)
            album_mid = albums[idx]["mid"]

        album_name, songs = api.get_album_songs(album_mid)
        if not songs:
            print("❌ 拿不到这张专辑的曲目")
            sys.exit(1)
        print(f"\n💿 专辑《{album_name}》共 {len(songs)} 首")
        results, saved = [], []
        for i, song in enumerate(songs, 1):
            print(f"\n[{i}/{len(songs)}]", end="")
            results.append(download_song(api, song, args.quality, layout, saved))
        print(f"\n💿 《{album_name}》：✅ 新下载 {results.count('ok')} 首 · ⏭️ 已存在 {results.count('skip')} 首 · ❌ 失败 {results.count('fail')} 首")
        if "fail" in results:
            print("  ⚠️ 专辑没下全，不写专辑增益（补下完再跑一次就会写）")
        elif "ok" in results:
            write_album_replaygain(saved)
        if args.spatial:
            spatial = download_spatial_album(api, songs)
            if spatial is None:
                print(f"\n🎧 《{album_name}》没有杜比全景声，不存全景声版")
            else:
                print(f"\n🎧 《{album_name}（{SPATIAL_VERSION}）》：✅ 新下载 {spatial.count('ok')} 首 · ⏭️ 已存在 {spatial.count('skip')} 首 · ❌ 失败 {spatial.count('fail')} 首")
                results += spatial
        if "fail" in results:
            sys.exit(2)

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
                download_song(api, song, args.quality, layout)

    else:
        # 交互模式
        while True:
            print("\n" + "=" * 50)
            print("  🎵 QQ 音乐下载器")
            print("=" * 50)
            print("  1. 搜索下载")
            print("  2. 下载歌单")
            print("  3. 切换音质 (当前: {})".format(quality_label(args.quality)))
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
                do_login()
            elif choice in ("5", "q", "quit"):
                print("👋 再见！")
                break


if __name__ == "__main__":
    main()
