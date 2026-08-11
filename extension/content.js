// B站弹幕悬浮窗 进度同步扩展
// 读取 B 站视频播放器的播放进度, 定时上报给本地弹幕悬浮窗 (127.0.0.1:8765)
(function () {
  'use strict';

  const ENDPOINT = 'http://127.0.0.1:8765/progress';
  const INTERVAL_MS = 500;
  let lastT = -1;
  let lastPlaying = null;

  function getBvid() {
    const m = location.pathname.match(/\/video\/(BV[0-9A-Za-z]+)/);
    return m ? m[1] : '';
  }

  function report(t, playing, duration) {
    fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bvid: getBvid(), t: t, playing: playing, duration: duration })
    }).catch(function () { /* 悬浮窗未启动时静默失败 */ });
  }

  function sync() {
    const v = document.querySelector('video');
    if (!v || !v.duration) return;
    const bvid = getBvid();
    if (!bvid) return;

    const t = v.currentTime || 0;
    const playing = !v.paused && !v.ended;
    const duration = v.duration || 0;

    // 节流: 播放中进度变化 <0.3s 不上报; 暂停/恢复状态变化必须上报
    if (playing === lastPlaying && playing && Math.abs(t - lastT) < 0.3) return;
    lastT = t;
    lastPlaying = playing;
    report(t, playing, duration);
  }

  // 监听播放器事件, 变化立即上报 (不依赖轮询延迟)
  document.addEventListener('play', function () { setTimeout(sync, 50); }, true);
  document.addEventListener('pause', function () { setTimeout(sync, 50); }, true);
  document.addEventListener('seeked', function () { setTimeout(sync, 50); }, true);
  document.addEventListener('ended', function () { setTimeout(sync, 50); }, true);

  setInterval(sync, INTERVAL_MS);
})();
