"""
抖音扫码登录后台 worker：
- 生成二维码截图
- 轮询登录状态
- 将状态写入 session json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path


REQUIRED_KEYS = ("ttwid", "odin_tt", "passport_csrf_token")
SUGGESTED_KEYS = ("msToken", "sid_guard")


def _now_ts() -> int:
    return int(time.time())


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _extract_cookies(cookies: list[dict]) -> dict:
    result = {}
    for c in cookies:
        domain = c.get("domain", "")
        if "douyin.com" not in domain:
            continue
        name = c.get("name", "")
        val = c.get("value", "")
        if name and val:
            result[name] = val
    return result


def _is_login_success(ck: dict) -> bool:
    return all(ck.get(k) for k in REQUIRED_KEYS)


def _pick_cookies(ck: dict) -> dict:
    result = {}
    for k in REQUIRED_KEYS + SUGGESTED_KEYS:
        if ck.get(k):
            result[k] = ck[k]
    return result


async def _run(args: argparse.Namespace):
    from playwright.async_api import async_playwright

    session_file = Path(args.session_file)
    session_id = args.session_id
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    qr_path = str(data_dir / f"douyin_login_qr_{session_id}.png")
    debug_path = str(data_dir / f"douyin_login_debug_{session_id}.png")

    _write_json(
        session_file,
        {
            "session_id": session_id,
            "status": "starting",
            "message": "初始化浏览器中",
            "created_at": _now_ts(),
            "updated_at": _now_ts(),
        },
    )

    p = None
    browser = None
    context = None
    page = None
    try:
        p = await async_playwright().start()
        browser = await p.chromium.launch(headless=args.headless)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 1800},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        target_urls = [
            "https://www.douyin.com/",
            "https://www.douyin.com/user/self?from_tab_name=main&showTab=like",
            "https://www.douyin.com/passport?next=%2F",
        ]
        for idx, url in enumerate(target_urls):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(2 if idx == 0 else 1)
                if page.url:
                    break
            except Exception:
                continue

        for login_selector in [
            "[data-e2e='top-login-button']",
            "text=登录",
            "text=立即登录",
            "button:has-text('登录')",
            "[class*='login']",
        ]:
            try:
                btn = page.locator(login_selector).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=1500)
                    await asyncio.sleep(1.0)
                    break
            except Exception:
                pass

        for tab_selector in ["text=扫码登录", "text=二维码登录", "text=扫码"]:
            try:
                tab = page.locator(tab_selector).first
                if await tab.count() > 0:
                    await tab.click(timeout=1500)
                    await asyncio.sleep(0.8)
                    break
            except Exception:
                pass

        qr_selector_candidates = [
            "[class*='qrcode'] canvas",
            "[class*='qr'] canvas",
            "[class*='qrcode'] img",
            "[class*='scan'] img",
            "canvas",
        ]
        qr_captured = False
        for selector in qr_selector_candidates:
            try:
                loc = page.locator(selector).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.screenshot(path=qr_path)
                    qr_captured = True
                    break
            except Exception:
                pass

            for frame in page.frames:
                try:
                    floc = frame.locator(selector).first
                    if await floc.count() > 0 and await floc.is_visible():
                        await floc.screenshot(path=qr_path)
                        qr_captured = True
                        break
                except Exception:
                    continue
            if qr_captured:
                break

        try:
            await page.screenshot(path=debug_path, full_page=True)
        except Exception:
            debug_path = ""

        if not qr_captured:
            await page.screenshot(path=qr_path, full_page=True)

        _write_json(
            session_file,
            {
                "session_id": session_id,
                "status": "qrcode_ready",
                "message": "二维码已生成",
                "qr_path": qr_path,
                "debug_path": debug_path,
                "qr_mode": "element" if qr_captured else "full_page",
                "page_url": page.url,
                "page_title": await page.title(),
                "created_at": _now_ts(),
                "updated_at": _now_ts(),
            },
        )

        elapsed = 0
        while elapsed < args.timeout:
            cookies = await context.cookies()
            ck = _extract_cookies(cookies)
            if _is_login_success(ck):
                picked = _pick_cookies(ck)
                _write_json(
                    session_file,
                    {
                        "session_id": session_id,
                        "status": "success",
                        "message": "登录成功",
                        "cookies": picked,
                        "qr_path": qr_path,
                        "debug_path": debug_path,
                        "updated_at": _now_ts(),
                    },
                )
                return
            await asyncio.sleep(3)
            elapsed += 3

        _write_json(
            session_file,
            {
                "session_id": session_id,
                "status": "timeout",
                "message": "扫码超时",
                "qr_path": qr_path,
                "debug_path": debug_path,
                "updated_at": _now_ts(),
            },
        )

    except Exception as e:
        _write_json(
            session_file,
            {
                "session_id": session_id,
                "status": "error",
                "message": str(e),
                "qr_path": qr_path,
                "debug_path": debug_path,
                "updated_at": _now_ts(),
            },
        )
    finally:
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        try:
            if p:
                await p.stop()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--session-file", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--headless", action="store_true", default=True)
    args = parser.parse_args()
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

