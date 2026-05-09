"""MUFG 入出金明細ページの日付フィールド DOM 調査。

実行: uv run python -m plugins.mufg.inspect_date
.env が暗号化されていれば master password を訊かれる。
"""
import asyncio

from src.inspect_helper import init_inspect
from playwright.async_api import async_playwright
from plugins.mufg.scraper import login

async def main():
    init_inspect()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await login(page)

        await page.click("a:has-text('入出金明細')")
        await page.wait_for_load_state("networkidle")
        await page.screenshot(path="/tmp/mufg_meisai.png")

        # 期間指定を選択して日付フィールドを表示
        await page.select_option("#sl-period", "5")
        await page.wait_for_timeout(500)
        await page.screenshot(path="/tmp/mufg_period.png")

        print("=== 期間指定後の input ===")
        for el in await page.query_selector_all("input[type='text'], input:not([type])"):
            box = await el.bounding_box()
            if box and box['width'] > 0:
                placeholder = await el.get_attribute("placeholder") or ""
                id_ = await el.get_attribute("id") or ""
                cls = await el.get_attribute("class") or ""
                print(f"  id={id_!r} placeholder={placeholder!r} class={cls!r} box={box}")

        # 条件変更ボタン
        btns = await page.query_selector_all("button")
        for btn in btns:
            text = (await btn.inner_text()).strip()
            if text:
                print(f"  button: {text!r}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
