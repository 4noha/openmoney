"""
Amazon 注文履歴ページの DOM 構造を調査するデバッグスクリプト。
セレクタが合わない場合はこれで確認して amazon.py を修正する。

実行: uv run python -m plugins.amazon.inspect
.env が暗号化されていれば master password を訊かれる。
"""
import asyncio

from src.inspect_helper import init_inspect
from playwright.async_api import async_playwright
from plugins.amazon.scraper import _launch_context, _needs_login, _on_orders_page, login, ORDER_HISTORY_URL


async def main():
    init_inspect()
    async with async_playwright() as p:
        ctx = await _launch_context(p, headless=False)
        page = await ctx.new_page()

        await page.goto(ORDER_HISTORY_URL, wait_until="load", timeout=60_000)
        if _needs_login(page.url) or not _on_orders_page(page.url):
            await login(page)

        await page.screenshot(path="/tmp/amazon_orders.png")
        print(f"URL: {page.url}")
        print("スクリーンショット: /tmp/amazon_orders.png")

        # 注文カードのクラスを調査
        cards = await page.query_selector_all(
            ".order.js-order-card, .a-box-group.js-order-card, [class*='order-card']"
        )
        print(f"\n注文カード数: {len(cards)}")
        if cards:
            # 最初のカードの HTML 構造を表示
            html = await cards[0].inner_html()
            print("\n=== 最初の注文カード HTML（先頭 2000 文字）===")
            print(html[:2000])

        # 注文日・金額・商品名のセレクタ候補を調査
        print("\n=== 日付っぽいテキスト ===")
        date_els = await page.evaluate("""() => {
            const results = [];
            document.querySelectorAll('span, div').forEach(el => {
                const t = el.innerText?.trim() || '';
                if (/\\d{4}年\\d{1,2}月\\d{1,2}日/.test(t) && t.length < 20) {
                    results.push({ cls: el.className.substring(0, 60), text: t });
                }
            });
            return results.slice(0, 10);
        }""")
        for e in date_els:
            print(f"  class={e['cls']!r} → {e['text']!r}")

        print("\n=== 金額っぽいテキスト ===")
        amount_els = await page.evaluate("""() => {
            const results = [];
            document.querySelectorAll('span, div').forEach(el => {
                const t = el.innerText?.trim() || '';
                if (/^[¥￥][\\d,]+$/.test(t) || /^[\\d,]+円$/.test(t)) {
                    results.push({ cls: el.className.substring(0, 60), text: t });
                }
            });
            return results.slice(0, 10);
        }""")
        for e in amount_els:
            print(f"  class={e['cls']!r} → {e['text']!r}")

        # HTML を保存
        with open("/tmp/amazon_orders.html", "w") as f:
            f.write(await page.content())
        print("\nHTML保存: /tmp/amazon_orders.html")

        input("\nEnter で終了...")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
