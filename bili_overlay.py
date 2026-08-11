# -*- coding: utf-8 -*-
"""
B站直播弹幕悬浮窗 —— 打游戏时听直播也能看弹幕
=================================================
- 实时接收直播间弹幕 (WSS, 已验证 2026-08 可用)
- 全透明背景, 只有弹幕文字在屏幕上滚动 (不影响游戏画面)
- 点击穿透 (不影响游戏操作), 游戏需无边框窗口化
- 弹幕颜色直接采集 B站弹幕数据自带的真实颜色, 白字黑边为默认样式
- 用法: python bili_overlay.py [房间号|短号]
  关闭: 点窗口右上角红点; 拖动: 按住左上角灰点拖动

依赖: pip install websocket-client brotli
"""
import json
import queue
import random
import struct
import threading
import time
import zlib
import sys
import urllib.parse
import urllib.request
import hashlib
import ctypes
import tkinter as tk

import websocket
import brotli

# ============ 配置 ============
FONT_FAMILY = "Microsoft YaHei UI"
FONT_SIZE = 16
MAX_ONSCREEN = 24           # 同时最多显示多少条弹幕
MAX_QUEUE = 200             # 弹幕队列上限(超出丢弃, 防止大直播间卡UI)
SCROLL_SPEED = 150          # 像素/秒
WINDOW_H = 240              # 窗口高度
MARGIN_TOP = 12             # 第一行弹幕距窗口顶部
LINE_HEIGHT = FONT_SIZE + 10
TRANSPARENT_BG = '#101014'  # 设为透明色的背景 (屏幕上不可见)

# ============ B站弹幕协议 (2026-08 验证可用) ============
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def make_cookie():
    """生成游客 cookie (buvid3), 无需登录"""
    import uuid
    b3 = str(uuid.uuid4()).upper() + "9247infoc"
    return f"buvid3={b3}; b_nut={int(time.time())}; buvid_fp={uuid.uuid4().hex}"


def http_get(url, params=None, cookie=None, tries=4):
    for i in range(tries):
        try:
            if params:
                url = url + '?' + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={
                'User-Agent': UA,
                'Referer': 'https://live.bilibili.com/',
                'Cookie': cookie or make_cookie(),
            })
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
    """短号 -> 真实房间ID (弹幕协议必须用真实ID)"""
    d = http_get(f'https://api.live.bilibili.com/room/v1/Room/room_init?id={room_id}',
                 cookie=cookie)
    if d.get('code') != 0:
        raise RuntimeError(f"房间号解析失败: {d.get('message')}")
    return d['data']['room_id']


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

    def __init__(self, room_id, cookie, out_queue):
        super().__init__(daemon=True)
        self.room_id = room_id
        self.cookie = cookie
        self.out_queue = out_queue
        self._stop = False

    def stop(self):
        self._stop = True

    def pack(self, op, body=b'', ver=0):
        return struct.pack('>IHHII', 16 + len(body), 16, ver, op, 1) + body

    def heartbeat_packet(self):
        """2026 新协议: 心跳 = op=2 + body='{}' (旧版 op=3 会被服务器直接踢下线)"""
        return self.pack(2, b'{}', ver=1)

    def run(self):
        while not self._stop:
            try:
                self._connect_once()
            except Exception as e:
                print(f"[弹幕] 连接异常: {e}, 5秒后重连", flush=True)
                time.sleep(5)

    def _connect_once(self):
        info = get_danmu_info(self.room_id, self.cookie)
        host = info['host_list'][0]['host']
        port = info['host_list'][0]['wss_port']
        ws = websocket.create_connection(f"wss://{host}:{port}/sub",
                                         header={'User-Agent': UA}, timeout=15)
        # 认证包: uid必须为0, roomid必须用真实房间ID, protover=3(brotli)
        auth = json.dumps({
            "uid": 0, "roomid": self.room_id, "protover": 3,
            "buvid": self.cookie.split('buvid3=')[1].split(';')[0],
            "platform": "web", "type": 2, "key": info['token'],
        }).encode()
        ws.send(self.pack(7, auth, ver=1), opcode=2)
        print(f"[弹幕] 已连接 {host} 房间{self.room_id}, 等待弹幕...", flush=True)

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
            if now - last_heartbeat >= 30:
                try:
                    ws.send(self.heartbeat_packet(), opcode=2)
                except Exception:
                    break
                last_heartbeat = now
        try:
            ws.close()
        except Exception:
            pass
        print("[弹幕] 连接断开, 准备重连", flush=True)

    def _parse(self, raw):
        while len(raw) >= 16:
            plen, _hlen, ver, op, _seq = struct.unpack('>IHHII', raw[:16])
            body = raw[16:plen]
            raw = raw[plen:]
            if op == 8:
                continue
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
                color = int(info[0][3]) or 0xFFFFFF  # B站弹幕真实颜色
                mode = info[0][1]
                if mode not in (1, 4):   # 跳过顶部/底部/高级弹幕
                    return
                self.out_queue.put((text, color))
            except Exception:
                pass


# ============ Overlay 窗口 ============
class OverlayApp:
    def __init__(self, room_id):
        self.queue = queue.Queue(maxsize=MAX_QUEUE)
        self.danmaku_items = []      # 每条: {items:[(canvas_id,dx,dy)], x, width, spd, lane}
        self._closed = False

        self.root = tk.Tk()
        self.root.title(f"B站弹幕悬浮窗 - 房间{room_id}")
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.configure(bg=TRANSPARENT_BG)
        # 像素级透明: 背景色整体变为透明, 只留文字
        self.root.attributes('-transparentcolor', TRANSPARENT_BG)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.win_w = sw
        self.root.geometry(f"{sw}x{WINDOW_H}+0+{sh - WINDOW_H - 24}")

        self.canvas = tk.Canvas(self.root, bg=TRANSPARENT_BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # 轨道数
        self.lane_count = max(1, (WINDOW_H - MARGIN_TOP * 2) // LINE_HEIGHT)
        self.lanes = [None] * self.lane_count   # 每条轨道最后一条弹幕

        # 关闭按钮(右上角红点, 实心色可点击)
        self.close_btn = self.canvas.create_oval(
            sw - 28, 6, sw - 12, 22, fill='#e5484d', outline='', tags='close')
        self.canvas.tag_bind('close', '<Button-1>', lambda e: self._close())
        self.canvas.tag_bind('close', '<Enter>',
                             lambda e: self.canvas.itemconfig(self.close_btn, fill='#ff6b6e'))
        self.canvas.tag_bind('close', '<Leave>',
                             lambda e: self.canvas.itemconfig(self.close_btn, fill='#e5484d'))

        # 拖动把手(左上角灰点): 按住拖动窗口, 松手恢复点击穿透
        self.handle = self.canvas.create_oval(6, 6, 22, 22, fill='#778899', outline='')
        self.canvas.tag_bind(self.handle, '<Button-1>', self._drag_start)
        self.canvas.bind('<B1-Motion>', self._drag_move)
        self.canvas.bind('<ButtonRelease-1>', self._drag_end)

        self._click_through(True)

        # 弹幕客户端线程
        self.client = DanmakuClient(room_id, make_cookie(), self.queue)
        self.client.start()

        self._last = time.time()
        self._tick()

    # ---- 窗口交互 ----
    def _click_through(self, enable):
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        if enable:
            style |= 0x00080000 | 0x00000020   # LAYERED | TRANSPARENT
        else:
            style &= ~0x00000020
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, style)

    def _drag_start(self, e):
        self._click_through(False)
        self._dx = e.x_root - self.root.winfo_x()
        self._dy = e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _drag_end(self, e):
        self._click_through(True)

    def _close(self):
        self._closed = True
        try:
            self.client.stop()
        except Exception:
            pass
        self.root.destroy()

    # ---- 弹幕渲染 ----
    def _create_danmaku(self, text, color, lane, x0):
        y = MARGIN_TOP + lane * LINE_HEIGHT
        fill = '#FFFFFF' if color == 0xFFFFFF else f'#{color:06x}'
        items = []
        # 先建4个黑色描边副本, 最后建主文字(在最上层) — 经典B站弹幕样式
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            it = self.canvas.create_text(x0 + dx, y + dy, text=text, anchor=tk.W,
                                         fill='#000000',
                                         font=(FONT_FAMILY, FONT_SIZE, 'bold'))
            items.append((it, dx, dy))
        main = self.canvas.create_text(x0, y, text=text, anchor=tk.W, fill=fill,
                                       font=(FONT_FAMILY, FONT_SIZE, 'bold'))
        items.append((main, 0, 0))
        bbox = self.canvas.bbox(main)
        width = (bbox[2] - bbox[0]) if bbox else len(text) * FONT_SIZE
        return {'items': items, 'x': x0, 'width': width,
                'spd': SCROLL_SPEED * random.uniform(0.85, 1.15), 'lane': lane}

    def _tick(self):
        now = time.time()
        dt = min(now - self._last, 0.1)
        self._last = now
        cw = self.canvas.winfo_width()
        if cw < 100:
            cw = self.win_w

        # 1. 移动所有弹幕(只动x, y固定在轨道上)
        for dm in self.danmaku_items:
            dm['x'] -= dm['spd'] * dt
            y = MARGIN_TOP + dm['lane'] * LINE_HEIGHT
            for it, dx, dy in dm['items']:
                self.canvas.coords(it, dm['x'] + dx, y + dy)

        # 2. 移除完全滚出左侧的, 释放轨道
        still = []
        for dm in self.danmaku_items:
            if dm['x'] + dm['width'] < -80:
                for it, _dx, _dy in dm['items']:
                    self.canvas.delete(it)
                if self.lanes[dm['lane']] is dm:
                    self.lanes[dm['lane']] = None
            else:
                still.append(dm)
        self.danmaku_items = still

        # 3. 消费新弹幕: 找有空位的轨道, 从右边缘进入
        while not self.queue.empty() and len(self.danmaku_items) < MAX_ONSCREEN:
            try:
                text, color = self.queue.get_nowait()
            except queue.Empty:
                break
            lane = None
            for i, last in enumerate(self.lanes):
                if last is None or (last['x'] + last['width'] < cw - 150):
                    lane = i
                    break
            if lane is None:
                break   # 轨道全满, 丢弃
            dm = self._create_danmaku(text, color, lane, cw)
            if dm:
                self.danmaku_items.append(dm)
                self.lanes[lane] = dm

        self.root.after(33, self._tick)

    def run(self):
        self.root.mainloop()


def main():
    room = sys.argv[1] if len(sys.argv) > 1 else input("直播间房间号: ").strip()
    cookie = make_cookie()
    real = room_to_real(room, cookie)
    print(f"[信息] 房间 {room} -> 真实ID {real}")
    app = OverlayApp(real)
    app.run()


if __name__ == '__main__':
    main()
