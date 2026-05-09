"""テンプレート共通の差し込みを行う極小ヘルパ。

server は Jinja2 を使わず Path(...).read_text() を直接 HTMLResponse に
返している。共通スニペット (例: 金額マスクウィジェット) を 5 ページに
copy/paste すると保守コストが上がるため、placeholder マーカーを置換する
シンプルな仕組みを用意する。
"""
from __future__ import annotations

import os
from pathlib import Path

_TEMPLATES = Path(__file__).parent / "templates"
_MASK_WIDGET = (_TEMPLATES / "_mask_widget.html").read_text()

_DEMO_MODE = os.environ.get("DEMO_MODE", "").lower() in ("1", "true", "yes")

# デモモードでは🔒ボタン（ログアウト）を非表示にする
_LOCK_BTN_HIDE = (
    "<style>#mf2-lock-btn{display:none!important}</style>"
    if _DEMO_MODE else ""
)

# デモモード時に全ページ下部に表示するバナー (カウントダウン付き)
_DEMO_BANNER = """
<div id="demo-footer-bar" style="
  position:fixed;bottom:0;left:0;right:0;z-index:9998;
  background:#1a1a2e;color:#aac4ff;
  padding:6px 16px;font-size:12px;
  display:flex;align-items:center;gap:12px;
  border-top:2px solid #3a3a6e;
">
  <span>👀 <b>デモモード (読み取り専用)</b></span>
  <span style="color:#888">—</span>
  <span id="demo-reset-msg" style="color:#ccc">
    レシートなどの入力データは <b>6時間ごと</b> にリセットされます
  </span>
</div>
<style>
  /* バナー高さ分だけページ下部に余白を確保 */
  body { padding-bottom: 36px !important; }
</style>
<script>
(async () => {
  try {
    const r = await fetch('/api/security/status');
    if (!r.ok) return;
    const d = await r.json();
    const nextAt = d.demo_next_reset_at;
    if (!nextAt) return;
    const target = new Date(nextAt);
    const el = document.getElementById('demo-reset-msg');
    if (!el) return;
    function fmt() {
      const diff = Math.max(0, target - Date.now());
      const h = Math.floor(diff / 3600000);
      const m = Math.floor((diff % 3600000) / 60000);
      const s = Math.floor((diff % 60000) / 1000);
      const hh = String(h).padStart(2, '0');
      const mm = String(m).padStart(2, '0');
      const ss = String(s).padStart(2, '0');
      el.innerHTML = `入力データは <b>${hh}:${mm}:${ss}</b> 後にリセットされます`;
    }
    fmt();
    setInterval(fmt, 1000);
  } catch (_) {}
})();
</script>
"""

# 青色申告・インボイス管理ページ専用: 全ボタン・入力を CSS で無効化
# body.demo-readonly に pointer-events:none を当てることで
# 動的レンダリングされた要素も自動的にカバーする。
_DEMO_READONLY_CSS = """
<style>
body.demo-readonly button:not(.mf2-fixed-btn) {
  pointer-events: none !important;
  opacity: 0.4 !important;
  cursor: not-allowed !important;
}
body.demo-readonly input:not([type=hidden]):not([type=radio]):not([type=range]),
body.demo-readonly textarea,
body.demo-readonly select {
  pointer-events: none !important;
  opacity: 0.5 !important;
  background: #f5f5f5 !important;
}
body.demo-readonly a[onclick],
body.demo-readonly [role=button] {
  pointer-events: none !important;
  opacity: 0.4 !important;
}
</style>
<script>
document.addEventListener('DOMContentLoaded', () => {
  document.body.classList.add('demo-readonly');
});
</script>
"""

# 読み取り専用にするページ
_DEMO_READONLY_PAGES = {"aoiro.html", "tax.html"}


def render(name: str) -> str:
    """テンプレを読み <!--MF2_MASK_WIDGET--> を共通スニペットに差し替えて返す。"""
    html = (_TEMPLATES / name).read_text()
    html = html.replace("<!--MF2_MASK_WIDGET-->", _MASK_WIDGET + _LOCK_BTN_HIDE)
    if _DEMO_MODE:
        extra = _DEMO_READONLY_CSS if name in _DEMO_READONLY_PAGES else ""
        html = html.replace("</body>", extra + _DEMO_BANNER + "\n</body>")
    return html
