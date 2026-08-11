# -*- coding: utf-8 -*-
"""
B站直播弹幕悬浮窗 v4 —— 打游戏时听直播也能看弹幕
=================================================
- 实时接收直播间弹幕 (WSS, 2026-08 协议验证可用)
- pygame 60fps 位图渲染 + Windows 分层窗口, 像素级透明
- 弹幕缓冲 + 轨道放行, 容量自动适配滚动区域
- 鼠标悬停显示边界: 拖动窗口 / 右下角缩放区域
- 配置持久化: 直播间列表(主播名+ID)、速度/字号/密度、滚动区域位置大小,
  下次启动自动沿用
- 控制台: 主播名下拉切换直播间 / 添加删除直播间 / 显示隐藏 / 速度字号密度

依赖: pip install pygame websocket-client brotli
"""
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import queue
import random
import struct
import threading
import time
import urllib.parse
import urllib.request
import zlib
import sys
import tkinter as tk
from tkinter import ttk

import pygame
import pygame.freetype
import websocket
import brotli

# ============ 配置 ============
FONT_PATH = r'C:\Windows\Fonts\msyhbd.ttc'
EMOJI_FONT_PATH = r'C:\Windows\Fonts\seguiemj.ttf'   # Windows emoji 字体
if getattr(sys, 'frozen', False):
    # PyInstaller 打包: 配置文件放在 exe 所在目录 (__file__ 指向临时解压目录)
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
DEFAULT_WINDOW_H = 240
MARGIN_TOP = 8
DEFAULT_SPEED = 150
DEFAULT_FONT_SIZE = 16
DEFAULT_DENSITY = 3
HEARTBEAT_INTERVAL = 30
BORDER_COLOR = (120, 180, 255, 90)
MIN_W, MIN_H = 400, 120

# ============ Windows API ============
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SW_HIDE, SW_SHOW = 0, 5
ULW_ALPHA = 0x00000002
AC_SRC_ALPHA = 0x01


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_ulong), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_ushort),
                ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_ulong),
                ("biSizeImage", ctypes.c_ulong), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_ulong),
                ("biClrImportant", ctypes.c_ulong)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER)]


class LayeredWindow:
    """把 pygame surface 实时推送到 Windows 分层窗口 (per-pixel alpha)"""

    def __init__(self, hwnd, w, h):
        self.hwnd, self.w, self.h = hwnd, w, h
        self.hdc_window = user32.GetDC(hwnd)
        self.hdc_mem = gdi32.CreateCompatibleDC(self.hdc_window)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        self.bmi = bmi
        self.bits = ctypes.c_void_p()
        self.hbmp = gdi32.CreateDIBSection(self.hdc_mem, ctypes.byref(bmi),
                                           0, ctypes.byref(self.bits), None, 0)
        self.old_bmp = gdi32.SelectObject(self.hdc_mem, self.hbmp)
        self.blend = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
        self.pt_src = POINT(0, 0)
        self.size = SIZE(w, h)

    def update(self, surface):
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
        pt_dst = POINT(rect.left, rect.top)
        data = pygame.image.tobytes(surface, 'BGRA', False)
        ctypes.memmove(self.bits, data, len(data))
        user32.UpdateLayeredWindow(self.hwnd, self.hdc_window,
                                   ctypes.byref(pt_dst), ctypes.byref(self.size),
                                   self.hdc_mem, ctypes.byref(self.pt_src),
                                   0, ctypes.byref(self.blend), ULW_ALPHA)

    def close(self):
        gdi32.SelectObject(self.hdc_mem, self.old_bmp)
        gdi32.DeleteObject(self.hbmp)
        gdi32.DeleteDC(self.hdc_mem)
        user32.ReleaseDC(self.hwnd, self.hdc_window)


# ============ 配置持久化 ============
class Config:
    _lock = threading.Lock()

    def __init__(self):
        self.rooms = []          # [{"name": 主播名, "id": 房间ID}]
        self.current_room = ''
        self.speed = DEFAULT_SPEED
        self.font_size = DEFAULT_FONT_SIZE
        self.density = DEFAULT_DENSITY
        self.visible = True
        self.window = None       # {"x","y","w","h"} 滚动区域

    @classmethod
    def load(cls):
        cfg = cls()
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                d = json.load(f)
            cfg.rooms = d.get('rooms', [])
            cfg.current_room = str(d.get('current_room', ''))
            cfg.speed = int(d.get('speed', DEFAULT_SPEED))
            cfg.font_size = int(d.get('font_size', DEFAULT_FONT_SIZE))
            cfg.density = int(d.get('density', DEFAULT_DENSITY))
            cfg.visible = bool(d.get('visible', True))
            cfg.window = d.get('window')
        except Exception:
            pass
        return cfg

    def save(self):
        d = {
            'rooms': self.rooms,
            'current_room': self.current_room,
            'speed': self.speed,
            'font_size': self.font_size,
            'density': self.density,
            'visible': self.visible,
            'window': self.window,
        }
        with self._lock:
            try:
                with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                    json.dump(d, f, ensure_ascii=False, indent=2)
            except Exception:
                pass


# ============ B站弹幕协议 (2026-08 验证可用) ============
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def make_cookie():
    import uuid
    b3 = str(uuid.uuid4()).upper() + "9247infoc"
    return f"buvid3={b3}; b_nut={int(time.time())}; buvid_fp={uuid.uuid4().hex}"


def http_get(url, params=None, cookie=None, tries=4):
    for i in range(tries):
        try:
            if params:
                url = url + '?' + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={
                'User-Agent': UA, 'Referer': 'https://live.bilibili.com/',
                'Cookie': cookie or make_cookie()})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5)


def get_mixin_key(orig):
    return ''.join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def enc_wbi(params, img_key, sub_key):
    mixin_key = get_mixin_key(img_key + sub_key)
    params = dict(params)
    params['wts'] = round(time.time())
    params = dict(sorted(params.items()))
    params = {k: ''.join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
    params['w_rid'] = hashlib.md5((urllib.parse.urlencode(params) + mixin_key).encode()).hexdigest()
    return params


def room_to_real(room_id, cookie):
    d = http_get(f'https://api.live.bilibili.com/room/v1/Room/room_init?id={room_id}',
                 cookie=cookie)
    if d.get('code') != 0:
        raise RuntimeError(f"房间号解析失败: {d.get('message')}")
    return d['data']['room_id']


def get_room_anchor_name(room_id, cookie):
    """获取主播名称: room_init 拿主播uid -> x/web-interface/card 拿昵称"""
    try:
        d = http_get(f'https://api.live.bilibili.com/room/v1/Room/room_init?id={room_id}',
                     cookie=cookie)
        uid = d['data']['uid']
        r = http_get('https://api.bilibili.com/x/web-interface/card',
                     {'mid': uid}, cookie=cookie)
        return r['data']['card']['name']
    except Exception:
        return None


def get_danmu_info(real_room_id, cookie):
    nav = http_get('https://api.bilibili.com/x/web-interface/nav', cookie=cookie)
    img_key = nav['data']['wbi_img']['img_url'].rsplit('/', 1)[1].split('.')[0]
    sub_key = nav['data']['wbi_img']['sub_url'].rsplit('/', 1)[1].split('.')[0]
    info = http_get('https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo',
                    enc_wbi({'id': real_room_id, 'type': 0}, img_key, sub_key),
                    cookie=cookie)
    if info.get('code') != 0:
        raise RuntimeError(f"弹幕服务器获取失败: {info.get('message')}")
    return info['data']


class DanmakuClient(threading.Thread):
    """WSS 弹幕接收线程, 断线自动重连"""

    def __init__(self, room_id, cookie, out_queue, status):
        super().__init__(daemon=True)
        self.room_id = room_id
        self.cookie = cookie
        self.out_queue = out_queue
        self.status = status
        self._stop = False

    def stop(self):
        self._stop = True

    @staticmethod
    def pack(op, body=b'', ver=0):
        return struct.pack('>IHHII', 16 + len(body), 16, ver, op, 1) + body

    def run(self):
        while not self._stop:
            try:
                self._connect_once()
            except Exception as e:
                self.status['conn'] = f"连接异常: {str(e)[:40]}"
                if not self._stop:
                    time.sleep(5)

    def _connect_once(self):
        info = get_danmu_info(self.room_id, self.cookie)
        host = info['host_list'][0]['host']
        port = info['host_list'][0]['wss_port']
        ws = websocket.create_connection(f"wss://{host}:{port}/sub",
                                         header={'User-Agent': UA}, timeout=15)
        auth = json.dumps({
            "uid": 0, "roomid": self.room_id, "protover": 3,
            "buvid": self.cookie.split('buvid3=')[1].split(';')[0],
            "platform": "web", "type": 2, "key": info['token'],
        }).encode()
        ws.send(self.pack(7, auth, ver=1), opcode=2)
        self.status['conn'] = "已连接"
        ws.settimeout(1.0)
        last_heartbeat = time.time()
        while not self._stop:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                pass
            except Exception:
                break
            else:
                self._parse(raw)
            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                try:
                    ws.send(self.pack(2, b'{}', ver=1), opcode=2)
                except Exception:
                    break
                last_heartbeat = now
        try:
            ws.close()
        except Exception:
            pass
        if not self._stop:
            self.status['conn'] = "重连中..."

    def _parse(self, raw):
        while len(raw) >= 16:
            plen, _hlen, ver, op, _seq = struct.unpack('>IHHII', raw[:16])
            body = raw[16:plen]
            raw = raw[plen:]
            if op != 5:
                continue
            try:
                if ver == 3:
                    self._parse(brotli.decompress(body))
                elif ver == 2:
                    self._parse(zlib.decompress(body))
                else:
                    self._emit(json.loads(body))
            except Exception:
                pass

    def _emit(self, j):
        cmd = j.get('cmd', '')
        if cmd.startswith('DANMU_MSG'):
            try:
                info = j['info']
                text = info[1]
                color = int(info[0][3]) or 0xFFFFFF
                mode = info[0][1]
                if mode not in (1, 4):
                    return
                self.out_queue.put_nowait((text, color))
            except Exception:
                pass


# ============ pygame 渲染线程 ============
class RenderThread(threading.Thread):
    def __init__(self, danmaku_queue, cmd_queue, status, cfg):
        super().__init__(daemon=True)
        self.danmaku_queue = danmaku_queue
        self.cmd_queue = cmd_queue
        self.status = status
        self.cfg = cfg

        self.speed = cfg.speed
        self.font_size = cfg.font_size
        self.density = cfg.density
        self.visible = cfg.visible
        self.client = None
        self.danmaku = []
        self.lanes = []
        self.font_cache = {}
        self._text_cache = {}
        self._font_render_lock = threading.Lock()

        self.hwnd = None
        self.win_w = 0
        self.win_h = DEFAULT_WINDOW_H
        self.win_x = 0
        self.win_y = 0
        self.mouse_in = False
        self.show_border = False
        self.dragging = False
        self.resizing = False
        self._drag_off = (0, 0)
        self._sdl_set_top = None
        self._stop_event = threading.Event()

        # 视频模式状态
        self.mode = 'live'                 # 'live' 直播 / 'video' 视频
        self.video = None                  # VideoDanmaku 实例
        self.video_idx = 0                 # 已消费到第几条
        self.video_last_t = 0.0            # 上次进度(跳变检测)
        self.video_loaded = False
        self._auto_playing = False         # 无扩展时软件自动推进时间轴
        self._auto_last_wall = 0.0

    def stop(self):
        self._stop_event.set()
        if self.client:
            self.client.stop()

    def _get_font(self, size):
        f = self.font_cache.get(size)
        if f is None:
            f = pygame.freetype.Font(FONT_PATH, size)
            self.font_cache[size] = f
        return f

    def _get_emoji_font(self, size):
        f = self.font_cache.get(('emoji', size))
        if f is None:
            try:
                f = pygame.freetype.Font(EMOJI_FONT_PATH, size)
            except Exception:
                f = self._get_font(size)
            self.font_cache[('emoji', size)] = f
        return f

    @staticmethod
    def _is_emoji(c):
        o = ord(c)
        return (0x1F000 <= o <= 0x1FAFF or   # 象形文字/补充
                0x2600 <= o <= 0x27BF or     # 杂项符号/装饰符号
                0x2B00 <= o <= 0x2BFF or     # 箭头等
                0x2190 <= o <= 0x21FF or     # 箭头
                0xFE00 <= o <= 0xFE0F or     # 变体选择符
                0x1F1E6 <= o <= 0x1F1FF or   # 区域指示符(国旗)
                o == 0x200D or               # ZWJ
                0x20E3 <= o <= 0x20E3)       # 组合键帽

    def _split_emoji(self, text):
        """把文本按 emoji 分段: [(片段, 是否emoji), ...]"""
        segs = []
        cur = ''
        cur_emoji = False
        first = True
        for c in text:
            e = self._is_emoji(c)
            if first:
                cur, cur_emoji, first = c, e, False
            elif e == cur_emoji:
                cur += c
            else:
                segs.append((cur, cur_emoji))
                cur, cur_emoji = c, e
        if cur:
            segs.append((cur, cur_emoji))
        return segs

    def _render_segments(self, text, color, size):
        """渲染文本(含 emoji 字体回退)为无描边 surface"""
        segs = self._split_emoji(text)
        surfaces = []
        for seg, is_emoji in segs:
            f = self._get_emoji_font(size) if is_emoji else self._get_font(size)
            s, _ = f.render(seg, fgcolor=color)
            surfaces.append(s)
        if len(surfaces) == 1:
            return surfaces[0]
        total_w = sum(s.get_width() for s in surfaces)
        max_h = max(s.get_height() for s in surfaces)
        out = pygame.Surface((total_w, max_h), pygame.SRCALPHA)
        x = 0
        for s in surfaces:
            out.blit(s, (x, (max_h - s.get_height()) // 2))
            x += s.get_width()
        return out

    def _render_text(self, text, color, size):
        """渲染带黑色描边的弹幕文字 (emoji 回退 + LRU 缓存)"""
        key = (text, color, size)
        surf = self._text_cache.get(key)
        if surf is not None:
            return surf
        with self._font_render_lock:
            main = self._render_segments(text, color, size)
            w, h = main.get_width() + 5, main.get_height() + 5
            out = pygame.Surface((w, h), pygame.SRCALPHA)
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2),
                           (-1, -1), (1, 1), (-1, 1), (1, -1)):
                black = self._render_segments(text, (0, 0, 0), size)
                out.blit(black, (dx + 2, dy + 2))
            out.blit(main, (2, 2))
        if len(self._text_cache) > 400:
            for k in list(self._text_cache)[:100]:
                del self._text_cache[k]
        self._text_cache[key] = out
        return out

    def _set_room(self, room_str):
        room_str = room_str.strip()
        if not room_str.isdigit():
            self.status['conn'] = "房间号无效"
            return
        if self.client:
            self.client.stop()
        self.danmaku.clear()
        self.lanes = [None] * len(self.lanes)
        while not self.danmaku_queue.empty():
            try:
                self.danmaku_queue.get_nowait()
            except queue.Empty:
                break
        self.status['room'] = room_str
        self.status['conn'] = "连接中..."
        try:
            cookie = make_cookie()
            real = room_to_real(int(room_str), cookie)
            self.client = DanmakuClient(real, cookie, self.danmaku_queue, self.status)
            self.client.start()
            # 抓主播名 (用于列表展示, 失败则用房间号)
            name = get_room_anchor_name(real, cookie)
            self.status['room_name'] = name or room_str
            # 配置列表里的名字还是房间号时, 自动补上主播名
            if name:
                for r in self.cfg.rooms:
                    if r['id'] == str(real) and r['name'] == str(real):
                        r['name'] = name
                self.cfg.save()
        except Exception as e:
            self.status['conn'] = f"房间解析失败: {str(e)[:40]}"
            self.status['room_name'] = room_str

    def _win_rect(self):
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
        return rect

    def _get_cursor(self):
        pt = ctypes.wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def _handle_mouse(self):
        cx, cy = self._get_cursor()
        rect = self._win_rect()
        inside = rect.left <= cx <= rect.right and rect.top <= cy <= rect.bottom
        lbtn = bool(user32.GetAsyncKeyState(0x01) & 0x8000)

        if inside and not self.mouse_in:
            self.mouse_in = True
            self.show_border = True
        elif not inside and self.mouse_in and not self.dragging and not self.resizing:
            self.mouse_in = False
            self.show_border = False
        # 每帧强制穿透状态 (SDL 可能主动恢复样式)
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        desired_transparent = not (self.mouse_in or self.dragging or self.resizing)
        if bool(style & WS_EX_TRANSPARENT) != desired_transparent:
            if desired_transparent:
                user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style | WS_EX_TRANSPARENT)
            else:
                user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style & ~WS_EX_TRANSPARENT)

        was_dragging, was_resizing = self.dragging, self.resizing
        if lbtn and inside and not self.dragging and not self.resizing:
            if cx >= rect.right - 26 and cy >= rect.bottom - 26:
                self.resizing = True
            else:
                self.dragging = True
            self._drag_off = (cx - rect.left, cy - rect.top)

        if self.dragging and lbtn:
            user32.SetWindowPos(self.hwnd, 0,
                                cx - self._drag_off[0], cy - self._drag_off[1],
                                0, 0, SWP_NOSIZE | SWP_NOACTIVATE)
        elif self.resizing and lbtn:
            new_w = max(MIN_W, min(cx - rect.left, user32.GetSystemMetrics(0)))
            new_h = max(MIN_H, min(cy - rect.top, user32.GetSystemMetrics(1)))
            if int(new_w) != self.win_w or int(new_h) != self.win_h:
                user32.SetWindowPos(self.hwnd, 0, rect.left, rect.top,
                                    int(new_w), int(new_h), SWP_NOACTIVATE)
                self._rebuild_canvas(int(new_w), int(new_h))

        if not lbtn:
            # 松手: 拖动/缩放结束, 保存滚动区域配置
            if was_dragging or was_resizing:
                r = self._win_rect()
                self.cfg.window = {'x': r.left, 'y': r.top,
                                   'w': r.right - r.left, 'h': r.bottom - r.top}
                self.cfg.save()
            self.dragging = False
            self.resizing = False

        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                self._stop_event.set()

    def _rebuild_canvas(self, new_w, new_h):
        self.lw.close()
        self.win_w, self.win_h = new_w, new_h
        self.canvas = pygame.Surface((new_w, new_h), pygame.SRCALPHA)
        self.lw = LayeredWindow(self.hwnd, new_w, new_h)
        self._apply_layout()
        self.danmaku.clear()
        self.lanes = [None] * self.lane_count

    def _apply_layout(self):
        self.line_height = self.font_size + 6
        self.lane_count = max(1, (self.win_h - MARGIN_TOP * 2) // self.line_height)
        self.lanes = [None] * self.lane_count

    def _find_lane(self):
        overlap = max(50, 200 - (self.density - 1) * 30)
        lane = None
        best_tail = None
        for i, last in enumerate(self.lanes):
            if last is None:
                return i
            tail = last['x'] + last['surf'].get_width()
            if tail < self.win_w + overlap and (best_tail is None or tail < best_tail):
                best_tail = tail
                lane = i
        return lane

    def run(self):
        try:
            self._run_inner()
        except Exception as e:
            import traceback
            self.status['conn'] = f"渲染线程异常: {type(e).__name__}: {e}"
            traceback.print_exc()
            self._stop_event.set()

    def _run_inner(self):
        pygame.init()
        pygame.freetype.init()
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)

        # 滚动区域: 优先用上次保存的配置
        if self.cfg.window:
            w = max(MIN_W, min(self.cfg.window['w'], sw))
            h = max(MIN_H, min(self.cfg.window['h'], sh))
            x = self.cfg.window['x']
            y = self.cfg.window['y']
        else:
            w, h = sw, DEFAULT_WINDOW_H
            x, y = 0, sh - DEFAULT_WINDOW_H - 24
        self.win_w, self.win_h = w, h
        self.win_x, self.win_y = x, y

        pygame.display.set_mode((w, h), pygame.NOFRAME)
        self.hwnd = pygame.display.get_wm_info()['window']
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE,
                              style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        user32.SetWindowPos(self.hwnd, 0, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER)

        # 置顶: SDL 原生 API (外部 SetWindowPos 会被 SDL 拒绝)
        try:
            import os as _os
            from pygame._sdl2 import Window as _SDLWindow
            _sdl2 = ctypes.CDLL(_os.path.join(_os.path.dirname(pygame.__file__), 'SDL2.dll'))
            _get_from_id = _sdl2.SDL_GetWindowFromID
            _get_from_id.argtypes = [ctypes.c_uint32]
            _get_from_id.restype = ctypes.c_void_p
            _set_aot = _sdl2.SDL_SetWindowAlwaysOnTop
            _set_aot.argtypes = [ctypes.c_void_p, ctypes.c_int]
            _sdl_win = _SDLWindow.from_display_module()
            self._sdl_win_ptr = _get_from_id(_sdl_win.id)
            self._sdl_set_top = lambda: _set_aot(self._sdl_win_ptr, 1)
            self._sdl_set_top()
        except Exception as e:
            print(f"[置顶] SDL API 初始化失败: {e}", flush=True)

        self.canvas = pygame.Surface((w, h), pygame.SRCALPHA)
        self.lw = LayeredWindow(self.hwnd, w, h)
        clock = pygame.time.Clock()

        self._apply_layout()
        if self.cfg.current_room:
            self._set_room(self.cfg.current_room)
        elif len(sys.argv) > 1:
            self._set_room(sys.argv[1])
        if not self.visible:
            user32.ShowWindow(self.hwnd, SW_HIDE)

        fps_counter = time.time()
        fps_frames = 0
        rate_counter = time.time()
        rate_n = 0
        topmost_timer = time.time()

        while not self._stop_event.is_set():
            self._handle_mouse()

            try:
                while True:
                    cmd = self.cmd_queue.get_nowait()
                    self._handle_cmd(cmd)
            except queue.Empty:
                pass

            dt = clock.tick(60) / 1000.0
            if dt <= 0:
                dt = 1 / 60.0

            if self.visible:
                for dm in self.danmaku:
                    dm['x'] -= dm['spd'] * dt
                keep = []
                for dm in self.danmaku:
                    if dm['x'] + dm['surf'].get_width() < -60:
                        if self.lanes[dm['lane']] is dm:
                            self.lanes[dm['lane']] = None
                    else:
                        keep.append(dm)
                self.danmaku = keep

                if self.danmaku_queue.qsize() > 500:
                    for _ in range(self.danmaku_queue.qsize() - 300):
                        try:
                            self.danmaku_queue.get_nowait()
                        except queue.Empty:
                            break

                # 视频模式: 按播放进度取弹幕送入队列
                if self.mode == 'video' and self.video is not None and self.video_loaded:
                    vt = self.status.get('vt')
                    # 扩展是否活跃 (3秒内上报过 = 扩展接管进度)
                    ext_alive = (time.time() - self.status.get('ext_last', 0)) < 3
                    # 自动推进: 无扩展时软件自己按真实时间走时间轴
                    if (self.status.get('auto_play') and vt and vt.get('bvid') == self.video.bvid
                            and not ext_alive):
                        if not self._auto_playing:
                            self._auto_playing = True
                            self._auto_last_wall = time.time()
                        now = time.time()
                        vt['t'] += now - self._auto_last_wall
                        vt['playing'] = True
                        self._auto_last_wall = now
                    elif not self.status.get('auto_play'):
                        self._auto_playing = False
                    if vt and vt.get('bvid') == self.video.bvid:
                        t = float(vt.get('t', 0))
                        playing = bool(vt.get('playing', True))
                        # 进度跳变(回退/快进): 清屏重定位
                        if abs(t - self.video_last_t) > 5:
                            self.danmaku.clear()
                            self.lanes = [None] * self.lane_count
                            self.video_idx = self.video.find_index(t - 0.5)
                        self.video_last_t = t
                        if playing:
                            dms = self.video.danmaku
                            # 弹幕在视频 t 秒时从右缘出现: 送入窗口 [t-0.5, t+3]
                            while (self.video_idx < len(dms) and
                                   dms[self.video_idx][0] <= t + 3):
                                dm_t, color, text = dms[self.video_idx]
                                self.video_idx += 1
                                if dm_t >= t - 0.5:
                                    try:
                                        self.danmaku_queue.put_nowait((text, color))
                                    except queue.Full:
                                        pass
                        self.status['vprogress'] = t
                    elif not vt:
                        self.status['vprogress'] = -1   # 等待扩展上报

                spawned = 0
                while not self.danmaku_queue.empty() and spawned < 8:
                    lane = self._find_lane()
                    if lane is None:
                        break
                    try:
                        text, color = self.danmaku_queue.get_nowait()
                    except queue.Empty:
                        break
                    rate_n += 1
                    fill = (255, 255, 255) if color == 0xFFFFFF else (
                        (color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)
                    try:
                        surf = self._render_text(text, fill, self.font_size)
                    except Exception:
                        continue
                    dm = {'surf': surf, 'x': float(self.win_w),
                          'spd': self.speed * random.uniform(0.9, 1.1), 'lane': lane}
                    self.danmaku.append(dm)
                    self.lanes[lane] = dm
                    spawned += 1

                self.canvas.fill((0, 0, 0, 0))
                for dm in self.danmaku:
                    self.canvas.blit(dm['surf'], (int(dm['x']), MARGIN_TOP + dm['lane'] * self.line_height))
                if self.show_border and self.mouse_in:
                    ww, hh = self.win_w, self.win_h
                    pygame.draw.rect(self.canvas, BORDER_COLOR, (1, 1, ww - 2, hh - 2), 2)
                    pygame.draw.rect(self.canvas, BORDER_COLOR, (ww - 24, hh - 24, 22, 22), 2)
                    pygame.draw.line(self.canvas, BORDER_COLOR, (ww - 16, hh - 8), (ww - 8, hh - 16), 2)
                self.lw.update(self.canvas)
                fps_frames += 1

            now = time.time()
            if now - topmost_timer > 0.5:
                if self._sdl_set_top:
                    try:
                        self._sdl_set_top()
                    except Exception:
                        pass
                else:
                    user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                topmost_timer = now
            if now - fps_counter >= 1:
                self.status['fps'] = fps_frames
                fps_frames = 0
                fps_counter = now
            if now - rate_counter >= 1:
                self.status['rate'] = rate_n
                rate_n = 0
                rate_counter = now

        if self.client:
            self.client.stop()
        self.lw.close()
        pygame.quit()

    def _handle_cmd(self, cmd):
        kind = cmd[0]
        if kind == 'room':
            self._set_room(cmd[1])
        elif kind == 'mode':
            new_mode = cmd[1]
            if new_mode == self.mode:
                return
            self.mode = new_mode
            self.danmaku.clear()
            self.lanes = [None] * self.lane_count
            while not self.danmaku_queue.empty():
                try:
                    self.danmaku_queue.get_nowait()
                except queue.Empty:
                    break
            if new_mode == 'video':
                if self.client:
                    self.client.stop()
                    self.client = None
                self.status['conn'] = '视频模式 (等待扩展上报进度)'
                self.video_idx = 0
                self.video_last_t = -100
            else:
                room = self.status.get('room', '')
                if room:
                    self._set_room(room)
                else:
                    self.status['conn'] = '未连接'
        elif kind == 'load_video':
            def _do_load():
                try:
                    vd = VideoDanmaku()
                    n = vd.load(cmd[1])
                    self.video = vd
                    self.video_loaded = True
                    self.video_idx = 0
                    self.video_last_t = -100
                    self.status['vduration'] = vd.duration
                    self.status['vbvid'] = vd.bvid
                    # 统计弹幕密集段 (每10分钟一桶, 提示前2名)
                    import collections
                    buckets = collections.Counter(int(t // 600) for t, _, _ in vd.danmaku)
                    top = sorted(buckets.items(), key=lambda x: -x[1])[:2]
                    dense = "、".join(f"{b * 10}~{(b + 1) * 10}分钟" for b, _ in sorted(top))
                    self.status['vdense'] = f"弹幕密集: {dense}"
                    self.status['vload'] = (f"已加载: {vd.title} | "
                                            f"时长{vd.duration // 60}分 | 弹幕{n}条 | "
                                            f"弹幕密集: {dense}")
                    self.status['conn'] = '视频已加载, 播放中自动同步'
                except Exception as e:
                    self.status['vload'] = f"加载失败: {str(e)[:50]}"
            threading.Thread(target=_do_load, daemon=True).start()
        elif kind == 'toggle_visible':
            self.visible = not self.visible
            user32.ShowWindow(self.hwnd, SW_SHOW if self.visible else SW_HIDE)
        elif kind == 'speed':
            new_spd = float(cmd[1])
            if self.speed > 0:
                ratio = new_spd / self.speed
                for dm in self.danmaku:
                    dm['spd'] *= ratio
            self.speed = new_spd
        elif kind == 'fontsize':
            self.font_size = int(cmd[1])
            self._apply_layout()
            self.danmaku.clear()
            self.lanes = [None] * self.lane_count
        elif kind == 'density':
            self.density = int(cmd[1])
        elif kind == 'quit':
            self._stop_event.set()


# ============ 视频弹幕 (拉取模式) ============
class VideoDanmaku:
    """视频弹幕数据: BV号 -> cid -> XML弹幕库, 按时间索引"""

    def __init__(self):
        self.bvid = ''
        self.title = ''
        self.duration = 0
        self.danmaku = []          # [(time_sec, color, text)] 按时间升序
        self._cookie = ''

    def load(self, bvid):
        """加载视频弹幕 (同步, 会阻塞几秒)"""
        import re
        self.bvid = bvid.strip()
        self._cookie = make_cookie()
        d = http_get('https://api.bilibili.com/x/web-interface/view',
                     {'bvid': self.bvid}, cookie=self._cookie)
        if d.get('code') != 0:
            raise RuntimeError(f"视频信息获取失败: {d.get('message')}")
        data = d['data']
        self.title = data['title']
        self.duration = int(data['duration'])
        cid = data['cid']

        req = urllib.request.Request(f'https://comment.bilibili.com/{cid}.xml', headers={
            'User-Agent': UA,
            'Referer': f'https://www.bilibili.com/video/{self.bvid}',
            'Accept-Encoding': 'gzip, deflate'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            enc = resp.headers.get('Content-Encoding', '')
        if enc == 'deflate':
            xml = zlib.decompress(raw, -zlib.MAX_WBITS).decode('utf-8', errors='replace')
        elif enc == 'gzip':
            import gzip
            xml = gzip.decompress(raw).decode('utf-8', errors='replace')
        else:
            xml = raw.decode('utf-8', errors='replace')

        dms = []
        for p, text in re.findall(r'<d p="([^"]+)">(.*?)</d>', xml, re.S):
            fields = p.split(',')
            try:
                t = float(fields[0])
                mode = int(fields[1])
                color = int(fields[3])
            except (ValueError, IndexError):
                continue
            if mode not in (1, 4):   # 只保留滚动弹幕
                continue
            dms.append((t, color, text))
        dms.sort(key=lambda x: x[0])
        self.danmaku = dms
        return len(dms)

    def find_index(self, t):
        """二分查找: 第一个时间 >= t 的弹幕索引"""
        lo, hi = 0, len(self.danmaku)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.danmaku[mid][0] < t:
                lo = mid + 1
            else:
                hi = mid
        return lo


class ProgressServer(threading.Thread):
    """本地 HTTP 服务: 接收 Chrome 扩展上报的视频播放进度"""

    def __init__(self, status, port=8765):
        super().__init__(daemon=True)
        self.status = status
        self.port = port
        self._httpd = None

    def run(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        status_ref = self.status

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    n = int(self.headers.get('Content-Length', 0))
                    body = json.loads(self.rfile.read(n).decode('utf-8'))
                    status_ref['vt'] = {
                        'bvid': body.get('bvid', ''),
                        't': float(body.get('t', 0)),
                        'playing': bool(body.get('playing', True)),
                        'duration': float(body.get('duration', 0)),
                    }
                    status_ref['ext_last'] = time.time()   # 扩展最后活跃时间
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'ok')
                except Exception:
                    self.send_response(400)
                    self.end_headers()

            def log_message(self, *a):
                pass

        try:
            self._httpd = HTTPServer(('127.0.0.1', self.port), Handler)
            self._httpd.serve_forever()
        except OSError as e:
            print(f"[进度服务] 端口 {self.port} 被占用: {e}", flush=True)

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()


# ============ 控制台 (tkinter) ============
class ConsoleApp:
    def __init__(self, cmd_queue, status, cfg):
        self.cmd_queue = cmd_queue
        self.status = status
        self.cfg = cfg
        self.root = tk.Tk()
        self.root.title("B站弹幕悬浮窗 控制台")
        self.root.attributes('-topmost', True)
        self.root.resizable(False, False)
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self.root.after(500, self._refresh_status)

    def _make_slider(self, frm, label, lo, hi, res, var, kind):
        """三行式配置条:
        第一行: 名称
        第二行: 当前值 (滑块正上方居中, 与滑块一体)
        第三行: [最小值] [滑块] [最大值] (轨道与数值水平对齐)
        点击轨道直接跳到点击位置"""
        row1 = ttk.Frame(frm)
        row1.pack(fill=tk.X, padx=10, pady=(6, 0))
        ttk.Label(row1, text=label).pack(side=tk.LEFT)

        row2 = ttk.Frame(frm)
        row2.pack(fill=tk.X, padx=10)
        row2.columnconfigure(1, weight=1)
        ttk.Label(row2, width=4).grid(row=0, column=0)
        val_label = ttk.Label(row2, text=str(int(round(float(var.get())))),
                              foreground='#2d8cf0')
        val_label.grid(row=0, column=1)
        ttk.Label(row2, width=4).grid(row=0, column=2)

        row3 = ttk.Frame(frm)
        row3.pack(fill=tk.X, padx=10, pady=(0, 2))
        row3.columnconfigure(1, weight=1)
        ttk.Label(row3, text=str(lo), width=4).grid(row=0, column=0)
        scale = tk.Scale(row3, from_=lo, to=hi, resolution=res, orient=tk.HORIZONTAL,
                         showvalue=False, variable=var,
                         command=lambda v: self._on_slider(kind, int(round(float(v)))))
        scale.grid(row=0, column=1, sticky='ew', padx=4)

        def on_click(e):
            w = scale.winfo_width()
            if w <= 1:
                return
            val = lo + (e.x / w) * (hi - lo)
            val = max(lo, min(hi, round(val / res) * res))
            scale.set(val)

        scale.bind('<Button-1>', on_click, add='+')
        ttk.Label(row3, text=str(hi), width=4).grid(row=0, column=2)

        # 当前值联动 (拖动/点击/外部设置都实时更新)
        def update_val(*_):
            val_label.config(text=str(int(round(float(var.get())))))

        var.trace_add('write', update_val)

    def _build_ui(self):
        pad = {'padx': 10, 'pady': 4}
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill=tk.BOTH)

        # 模式切换
        row = ttk.Frame(frm)
        row.pack(fill=tk.X, **pad)
        ttk.Label(row, text="模式:").pack(side=tk.LEFT)
        self.mode_var = tk.StringVar(value='live')
        ttk.Radiobutton(row, text="直播", value='live', variable=self.mode_var,
                        command=self._on_mode_change).pack(side=tk.LEFT, padx=4)
        ttk.Radiobutton(row, text="视频", value='video', variable=self.mode_var,
                        command=self._on_mode_change).pack(side=tk.LEFT, padx=4)

        # 直播模式控件
        self.live_frame = ttk.Frame(frm)
        self.live_frame.pack(fill=tk.X)
        row = ttk.Frame(self.live_frame)
        row.pack(fill=tk.X, **pad)
        ttk.Label(row, text="直播间:").pack(side=tk.LEFT)
        self.room_var = tk.StringVar()
        self.room_combo = ttk.Combobox(row, textvariable=self.room_var,
                                       state='readonly', width=24)
        self.room_combo.pack(side=tk.LEFT, padx=6)
        self.room_combo.bind('<<ComboboxSelected>>', self._on_room_selected)
        row = ttk.Frame(self.live_frame)
        row.pack(fill=tk.X, **pad)
        ttk.Label(row, text="添加(房间号):").pack(side=tk.LEFT)
        self.add_var = tk.StringVar()
        self.add_entry = ttk.Entry(row, textvariable=self.add_var, width=14)
        self.add_entry.pack(side=tk.LEFT, padx=6)
        self.add_entry.bind('<Return>', lambda e: self._add_room())
        ttk.Button(row, text="添加", command=self._add_room).pack(side=tk.LEFT)
        ttk.Button(row, text="删除当前", command=self._del_room).pack(side=tk.LEFT, padx=4)

        # 视频模式控件
        self.video_frame = ttk.Frame(frm)
        row = ttk.Frame(self.video_frame)
        row.pack(fill=tk.X, **pad)
        ttk.Label(row, text="视频BV:").pack(side=tk.LEFT)
        self.bv_var = tk.StringVar()
        self.bv_entry = ttk.Entry(row, textvariable=self.bv_var, width=18)
        self.bv_entry.pack(side=tk.LEFT, padx=6)
        self.bv_entry.bind('<Return>', lambda e: self._load_video())
        ttk.Button(row, text="加载", command=self._load_video).pack(side=tk.LEFT)
        self.vload_var = tk.StringVar(value="输入BV号加载视频弹幕")
        ttk.Label(self.video_frame, textvariable=self.vload_var,
                  foreground='#888888').pack(anchor=tk.W, **pad)

        # 手动进度条 (无扩展时拖动跟随; 有扩展时自动刷新)
        row = ttk.Frame(self.video_frame)
        row.pack(fill=tk.X, **pad)
        ttk.Label(row, text="进度:").pack(side=tk.LEFT)
        self.vt_var = tk.DoubleVar(value=0)
        self.vt_scale = tk.Scale(row, from_=0, to=1000, resolution=1,
                                 orient=tk.HORIZONTAL, showvalue=False,
                                 variable=self.vt_var, length=200,
                                 command=self._on_vt_manual)
        self.vt_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.vt_label = ttk.Label(row, text="0:00 / 0:00", width=14)
        self.vt_label.pack(side=tk.RIGHT)
        self._vt_dragging = False
        self.vt_scale.bind('<ButtonPress-1>', lambda e: setattr(self, '_vt_dragging', True))
        self.vt_scale.bind('<ButtonRelease-1>', lambda e: setattr(self, '_vt_dragging', False))
        self.auto_var = tk.StringVar(value="▶ 自动播放")
        ttk.Button(row, textvariable=self.auto_var, command=self._toggle_auto_play,
                   width=10).pack(side=tk.LEFT, padx=4)
        ttk.Label(self.video_frame,
                  text="提示: 装 Chrome 扩展后自动精确跟随; 不装可用进度条+自动播放",
                  foreground='#888888').pack(anchor=tk.W, **pad)

        self.status_var = tk.StringVar(value="启动中...")
        ttk.Label(frm, textvariable=self.status_var, foreground='#2d8cf0').pack(anchor=tk.W, **pad)

        self.vis_var = tk.StringVar(value="隐藏弹幕")
        ttk.Button(frm, textvariable=self.vis_var, command=self._toggle_visible).pack(fill=tk.X, **pad)

        self.speed_var = tk.DoubleVar(value=self.cfg.speed)
        self._make_slider(frm, "速度", 60, 320, 10, self.speed_var, 'speed')
        self.size_var = tk.DoubleVar(value=self.cfg.font_size)
        self._make_slider(frm, "字号", 12, 28, 1, self.size_var, 'fontsize')
        self.density_var = tk.DoubleVar(value=self.cfg.density)
        self._make_slider(frm, "密度", 1, 6, 1, self.density_var, 'density')

        ttk.Button(frm, text="退出程序", command=self._quit).pack(fill=tk.X, **pad)

        self._sync_room_list()

    def _on_mode_change(self):
        mode = self.mode_var.get()
        if mode == 'video':
            self.live_frame.pack_forget()
            self.video_frame.pack(fill=tk.X)
        else:
            self.video_frame.pack_forget()
            self.live_frame.pack(fill=tk.X)
        self.cmd_queue.put(('mode', mode))

    def _load_video(self):
        bv = self.bv_var.get().strip()
        if not bv:
            return
        self.vload_var.set("加载中...")
        self.cmd_queue.put(('load_video', bv))

    def _on_vt_manual(self, val):
        """手动进度条拖动: 直接把进度写入状态 (无需扩展), 同时停止自动推进"""
        bvid = self.status.get('vbvid', '')
        dur = int(self.status.get('vduration', 0)) or 1
        if not bvid:
            return
        t = float(val) / 1000.0 * dur
        self.status['vt'] = {'bvid': bvid, 't': t, 'playing': True, 'duration': dur}
        self.status['auto_play'] = False
        if self.auto_var.get().startswith('⏸'):
            self.auto_var.set("▶ 自动播放")

    def _toggle_auto_play(self):
        """无扩展时的自动推进: 从当前进度按真实时间走"""
        bvid = self.status.get('vbvid', '')
        if not bvid:
            return
        if self.status.get('auto_play'):
            self.status['auto_play'] = False
            self.auto_var.set("▶ 自动播放")
        else:
            # 从当前进度继续 (没有进度则从 0 开始)
            vt = self.status.get('vt')
            if not vt or vt.get('bvid') != bvid:
                self.status['vt'] = {'bvid': bvid, 't': 0.0, 'playing': True,
                                     'duration': int(self.status.get('vduration', 0)) or 1}
            self.status['auto_play'] = True
            self.auto_var.set("⏸ 暂停推进")

    # ---- 直播间管理 ----
    @staticmethod
    def _display(r):
        """列表显示: 主播名 (房间ID)"""
        return f"{r['name']} ({r['id']})"

    def _sync_room_list(self):
        names = [self._display(r) for r in self.cfg.rooms]
        self.room_combo['values'] = names
        cur = None
        for r in self.cfg.rooms:
            if r['id'] == self.cfg.current_room:
                cur = self._display(r)
                break
        if cur is None and names:
            cur = names[0]
        if cur is not None:
            self.room_var.set(cur)

    def _on_room_selected(self, _e):
        sel = self.room_var.get()
        for r in self.cfg.rooms:
            if self._display(r) == sel:
                self.cfg.current_room = r['id']
                self.cfg.save()
                self.cmd_queue.put(('room', r['id']))
                break

    def _add_room(self):
        rid = self.add_var.get().strip()
        if not rid.isdigit():
            self.status_var.set("房间号无效")
            return
        # 抓主播名
        try:
            cookie = make_cookie()
            real = room_to_real(int(rid), cookie)
            name = get_room_anchor_name(real, cookie) or rid
        except Exception as e:
            self.status_var.set(f"添加失败: {str(e)[:30]}")
            return
        for r in self.cfg.rooms:
            if r['id'] == str(real) or r['name'] == name:
                self.status_var.set(f"已存在: {name}")
                return
        self.cfg.rooms.append({'name': name, 'id': str(real)})
        self.cfg.current_room = str(real)
        self.cfg.save()
        self._sync_room_list()
        self.cmd_queue.put(('room', str(real)))
        self.add_var.set('')
        self.status_var.set(f"已添加并切换到: {name}")

    def _del_room(self):
        sel = self.room_var.get()
        for i, r in enumerate(self.cfg.rooms):
            if self._display(r) == sel:
                del self.cfg.rooms[i]
                if self.cfg.current_room == r['id']:
                    if self.cfg.rooms:
                        self.cfg.current_room = self.cfg.rooms[0]['id']
                        self.cmd_queue.put(('room', self.cfg.current_room))
                    else:
                        self.cfg.current_room = ''
                self.cfg.save()
                self._sync_room_list()
                self.status_var.set(f"已删除: {name}")
                return

    # ---- 其他控件 ----
    def _on_slider(self, kind, value):
        self.cmd_queue.put((kind, value))
        setattr(self.cfg, {'speed': 'speed', 'fontsize': 'font_size', 'density': 'density'}[kind], value)
        self.cfg.save()

    def _toggle_visible(self):
        self.vis_var.set("显示弹幕" if self.vis_var.get() == "隐藏弹幕" else "隐藏弹幕")
        self.cfg.visible = (self.vis_var.get() == "隐藏弹幕")
        self.cfg.save()
        self.cmd_queue.put(('toggle_visible',))

    def _refresh_status(self):
        s = self.status
        mode = self.mode_var.get()
        if mode == 'video':
            vload = s.get('vload', '')
            vp = s.get('vprogress', -1)
            vt = s.get('vt')
            dur = int((vt or {}).get('duration', 0)) or int(s.get('vduration', 0)) or 1
            ext = s.get('ext_last')
            ext_str = f"扩展:{'已连接' if ext and time.time() - ext < 5 else '未连接'}"
            # 自动模式下进度条跟随 (用户拖动时跳过)
            if vp is not None and vp >= 0 and not getattr(self, '_vt_dragging', False):
                self.vt_var.set(min(1000, max(0, int(vp / dur * 1000))))
                self.vt_label.config(
                    text=f"{int(vp)//60}:{int(vp)%60:02d} / {dur//60}:{dur%60:02d}")
            if vp is not None and vp >= 0 and vt:
                playing = '播放中' if vt.get('playing') else '已暂停'
                self.status_var.set(
                    f"{vload} | {int(vp)//60}:{int(vp)%60:02d}/{dur//60}:{dur%60:02d} "
                    f"{playing} {ext_str}")
            elif vload:
                self.status_var.set(f"{vload} | 等待浏览器上报进度 {ext_str}")
            else:
                self.status_var.set(f"视频模式 | {s.get('conn', '-')}")
            # 自动播放按钮与状态同步 (扩展上报会关闭自动推进)
            if s.get('auto_play') and not self.auto_var.get().startswith('⏸'):
                self.auto_var.set("⏸ 暂停推进")
            elif not s.get('auto_play') and self.auto_var.get().startswith('⏸'):
                self.auto_var.set("▶ 自动播放")
        else:
            room = s.get('room', '-')
            name = s.get('room_name', '')
            conn = s.get('conn', '-')
            fps = s.get('fps', 0)
            rate = s.get('rate', 0)
            shown = f"{name} ({room})" if name and name != room else room
            self.status_var.set(f"当前: {shown} | {conn} | {fps}fps | {rate}条/秒")
        self.root.after(500, self._refresh_status)

    def _quit(self):
        self.cmd_queue.put(('quit',))
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, False, "Local\\BiliDanmakuOverlay")
    if kernel32.GetLastError() == 183:
        print("已有弹幕悬浮窗在运行, 请先退出旧实例再启动")
        sys.exit(1)

    cfg = Config.load()
    # 启动参数优先: 指定了房间则作为当前房间
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        cfg.current_room = sys.argv[1]
        if not any(r['id'] == sys.argv[1] for r in cfg.rooms):
            try:
                cookie = make_cookie()
                real = room_to_real(int(sys.argv[1]), cookie)
                name = get_room_anchor_name(real, cookie) or sys.argv[1]
                cfg.rooms.append({'name': name, 'id': str(real)})
            except Exception:
                pass
        cfg.save()

    status = {'room': cfg.current_room, 'room_name': '', 'conn': '启动中', 'fps': 0, 'rate': 0}
    danmaku_queue = queue.Queue(maxsize=2000)
    cmd_queue = queue.Queue()

    # 本地进度服务 (接收 Chrome 扩展上报的视频播放进度)
    progress_server = ProgressServer(status)
    progress_server.start()

    render = RenderThread(danmaku_queue, cmd_queue, status, cfg)
    render.start()

    console = ConsoleApp(cmd_queue, status, cfg)
    try:
        console.run()
    finally:
        render.stop()
        progress_server.stop()
        render.join(timeout=3)


if __name__ == '__main__':
    main()
