"""
メルカリ取引履歴ページの DOM 構造を調査するデバッグスクリプト。

実行: uv run python -m plugins.mercari.inspect
.env が暗号化されていれば master password を訊かれる。
"""
import asyncio

from src.inspect_helper import init_inspect
from playwright.async_api import async_playwright
from plugins.mercari.scraper import _launch_context, _needs_login, login, PURCHASES_URL, SALES_URL


async def main():
    init_inspect()
    async with async_playwright() as p:
        ctx = await _launch_context(p, headless=False)
        page = await ctx.new_page()

        await page.goto(PURCHASES_URL, wait_until="load", timeout=60_000)
        await asyncio.sleep(2)
        if _needs_login(page.url):
            print("ログインが必要です...")
            await login(page)
            await page.goto(PURCHASES_URL, wait_until="load", timeout=60_000)
            await asyncio.sleep(2)

        await page.screenshot(path="/tmp/mercari_purchases.png")
        print(f"購入履歴URL: {page.url}")

        # 購入履歴の要素を調査
        result = await page.evaluate("""() => {
            const results = [];
            document.querySelectorAll('li, article, [class*="item"], [class*="transaction"]').forEach(el => {
                const t = (el.innerText || '').trim();
                if (t.length > 10 && t.length < 200) {
                    results.push({tag: el.tagName, cls: el.className.substring(0, 80), text: t.substring(0, 100)});
                }
            });
            return results.slice(0, 20);
        }""")
        print("\n=== 購入履歴要素候補 ===")
        for r in result:
            print(f"  <{r['tag']}> {r['cls']!r}")
            print(f"    → {r['text']!r}")

        with open("/tmp/mercari_purchases.html", "w") as f:
            f.write(await page.content())
        print("\nHTML保存: /tmp/mercari_purchases.html")

        # 売上履歴ページも確認
        await page.goto(SALES_URL, wait_until="load", timeout=60_000)
        await page.screenshot(path="/tmp/mercari_sales.png")
        print(f"\n売上履歴URL: {page.url}")
        with open("/tmp/mercari_sales.html", "w") as f:
            f.write(await page.content())
        print("HTML保存: /tmp/mercari_sales.html")

        input("\nEnter で終了...")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
