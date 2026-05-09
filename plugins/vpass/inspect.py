"""
VPASS のページ構造を調査するデバッグスクリプト。
セレクタが合わない場合はこれで確認して vpass.py を修正する。

実行: uv run python -m plugins.vpass.inspect
.env が暗号化されていれば master password を訊かれる。
"""
import asyncio

from src.inspect_helper import init_inspect
from playwright.async_api import async_playwright
from plugins.vpass.scraper import login


async def main():
    init_inspect()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await login(page)

        await page.screenshot(path="/tmp/vpass_top.png")
        print(f"ログイン後URL: {page.url}")
        print("スクリーンショット: /tmp/vpass_top.png")

        # 利用明細リンクを探す
        links = await page.query_selector_all("a")
        print("\n=== リンク一覧（利用/明細/照会 を含むもの） ===")
        for link in links:
            text = (await link.inner_text()).strip()
            href = await link.get_attribute("href") or ""
            if any(kw in text + href for kw in ["利用", "明細", "照会", "meisai", "usage"]):
                print(f"  {text!r} → {href}")

        # セレクトボックスを探す
        selects = await page.query_selector_all("select")
        print(f"\n=== セレクトボックス ({len(selects)} 個) ===")
        for sel in selects:
            name = await sel.get_attribute("name") or ""
            id_ = await sel.get_attribute("id") or ""
            options = await sel.evaluate("el => Array.from(el.options).map(o => `${o.value}: ${o.text}`)")
            print(f"  name={name!r} id={id_!r}")
            for opt in options[:10]:
                print(f"    {opt}")

        input("\nEnter で終了...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
