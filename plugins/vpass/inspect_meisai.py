"""
VPASS 利用明細ページの DOM 構造を調査する。
明細行のセレクタを vpass.py の _parse_table に反映するために使う。

実行: uv run python -m plugins.vpass.inspect_meisai
.env が暗号化されていれば master password を訊かれる。
"""
import asyncio
from pathlib import Path

from src.inspect_helper import init_inspect
from playwright.async_api import async_playwright
from plugins.vpass.scraper import login, _navigate_to_meisai, _get_available_months, _select_month

_BROWSER_DATA_DIR = Path(".browser_data/vpass")


async def main():
    init_inspect()
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(_BROWSER_DATA_DIR),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        await login(page)
        await _navigate_to_meisai(page)

        months = await _get_available_months(page)
        print(f"利用可能月: {months[:5]}")

        # 最新月を選択
        if months:
            await _select_month(page, months[0])
            await page.wait_for_timeout(2000)

        await page.screenshot(path="/tmp/vpass_meisai_debug.png")

        # テーブル確認
        tables = await page.query_selector_all("table")
        print(f"\n<table> タグ数: {len(tables)}")
        for i, t in enumerate(tables[:3]):
            rows = await t.query_selector_all("tr")
            print(f"  table[{i}]: {len(rows)} 行")

        # div/li でリスト状の要素を探す
        print("\n=== 金額っぽいテキストを含む要素 ===")
        # ¥ や円 や数字+カンマ を含む要素
        elements = await page.evaluate("""() => {
            const results = [];
            const walker = document.createTreeWalker(
                document.body,
                NodeFilter.SHOW_TEXT,
                null
            );
            let node;
            while (node = walker.nextNode()) {
                const text = node.textContent.trim();
                if (/[0-9,]{3,}/.test(text) && (text.includes('¥') || text.includes('円') || /^[\\d,]+$/.test(text))) {
                    const el = node.parentElement;
                    const tag = el.tagName.toLowerCase();
                    const cls = el.className || '';
                    const id = el.id || '';
                    results.push({ tag, cls: cls.substring(0, 60), id: id.substring(0, 40), text: text.substring(0, 50) });
                }
            }
            return results.slice(0, 30);
        }""")
        for e in elements:
            print(f"  <{e['tag']} class={e['cls']!r} id={e['id']!r}> {e['text']!r}")

        # 日付っぽいテキスト
        print("\n=== 日付っぽいテキストを含む要素 ===")
        date_elements = await page.evaluate("""() => {
            const results = [];
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
            let node;
            while (node = walker.nextNode()) {
                const text = node.textContent.trim();
                if (/^\\d{4}[\\/-]\\d{1,2}[\\/-]\\d{1,2}$/.test(text) || /^\\d{1,2}[\\/-]\\d{1,2}$/.test(text) || /^\\d{1,2}月\\d{1,2}日/.test(text)) {
                    const el = node.parentElement;
                    const tag = el.tagName.toLowerCase();
                    const cls = el.className || '';
                    const id = el.id || '';
                    results.push({ tag, cls: cls.substring(0, 60), id: id.substring(0, 40), text: text.substring(0, 50) });
                }
            }
            return results.slice(0, 20);
        }""")
        for e in date_elements:
            print(f"  <{e['tag']} class={e['cls']!r} id={e['id']!r}> {e['text']!r}")

        # ページの明細エリアの HTML を出力
        print("\n=== main/article/section の概要 ===")
        containers = await page.query_selector_all("main, article, section, [id*='meisai'], [id*='list'], [class*='meisai'], [class*='list'], [class*='usage'], [class*='transaction']")
        for c in containers[:5]:
            tag = await c.evaluate("el => el.tagName.toLowerCase()")
            cls = await c.get_attribute("class") or ""
            id_ = await c.get_attribute("id") or ""
            child_count = await c.evaluate("el => el.children.length")
            text_preview = (await c.inner_text())[:100].replace("\n", " ")
            print(f"  <{tag} id={id_!r} class={cls[:50]!r}> children={child_count} text={text_preview!r}")

        # HTMLを保存
        html = await page.content()
        with open("/tmp/vpass_meisai.html", "w") as f:
            f.write(html)
        print("\n\nHTML保存: /tmp/vpass_meisai.html")
        print("スクリーンショット: /tmp/vpass_meisai_debug.png")

        input("\nEnter で終了...")
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
