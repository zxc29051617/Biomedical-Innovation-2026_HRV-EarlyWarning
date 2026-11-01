#!/usr/bin/env python3
"""Exercise the offline demo in Chromium using synthetic data only."""
from __future__ import annotations

import json
import base64
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from playwright.sync_api import expect, sync_playwright

from serve_demo import DemoHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "artifacts" / "browser"


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    checks: list[str] = []
    errors: list[str] = []
    requests: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request.url)
                if request.url.startswith(("http://", "https://")) else None)
        page.goto((ROOT / "web" / "index.html").as_uri())
        expect(page.locator("#case-list button")).to_have_count(3)
        expect(page.locator("#score-kind")).to_have_text("合成示意分數")
        expect(page.locator("#risk-chart svg")).to_be_visible()
        page.screenshot(path=str(OUTPUT / "initial-load.png"), full_page=True)
        assert not page.locator("#show-event").is_checked()
        checks.append("offline load / synthetic labeling / event hidden by default")

        def seek(index: int) -> None:
            page.locator("#timeline").evaluate(
                "(el, i) => {el.value = String(i); el.dispatchEvent(new Event('input', {bubbles: true}));}", index)

        page.locator("#rewind").click()
        expect(page.locator("#timeline")).to_have_value("0")
        page.locator("#step").click()
        expect(page.locator("#timeline")).to_have_value("1")
        page.locator("#playback-speed").select_option("400")
        page.locator("#play-pause").click()
        expect(page.locator("#play-pause")).to_have_attribute("aria-pressed", "true")
        # Locator assertions poll without requiring CSP unsafe-eval.
        expect(page.locator("#timeline")).not_to_have_value("1")
        page.locator("#play-pause").click()
        expect(page.locator("#play-pause")).to_have_attribute("aria-pressed", "false")
        checks.append("rewind / step / accelerated playback / pause")

        page.locator("#case-list button").nth(2).click()
        seek(4)  # Synthetic hour 10 is explicitly rejected.
        expect(page.locator("#risk-value")).to_have_text("—")
        expect(page.locator("#rmssd")).to_have_text("—")
        page.locator("#model-select").select_option("vitals_all")
        expect(page.locator("#risk-value")).not_to_have_text("—")
        page.locator("#model-select").select_option("paired_history_rr")
        checks.append("quality rejection suppresses paired models; vitals baseline remains available")

        page.locator("#case-list button").nth(1).click()
        seek(17)  # Hour 23, before the synthetic event at hour 24.
        page.locator("#show-event").check()
        expect(page.locator("#outcome-note")).to_be_hidden()
        seek(18)
        expect(page.locator("#outcome-note")).to_be_visible()
        expect(page.locator("#risk-value")).to_have_text("—")
        page.locator("#show-event").uncheck()
        expect(page.locator("#outcome-note")).to_be_hidden()
        seek(int(page.locator("#timeline").get_attribute("max")))
        expect(page.locator("#risk-value")).to_have_text("—")
        expect(page.locator("#mean-rr")).to_have_text("—")
        checks.append("no future event disclosure / no event or terminal scores / no terminal carry-forward")

        # Future mutations must not affect any currently displayed chart or score.
        page.locator("#case-list button").nth(0).click()
        seek(3)
        before = page.locator("#risk-chart").inner_html()
        assert page.evaluate("HRVAppCore.visiblePoints(HRV_DEMO.cases[0].points, 3).every(p => p.hour <= 9)")
        synthetic = page.evaluate("JSON.parse(JSON.stringify(HRV_DEMO))")
        synthetic["source_label"] = "Future-only synthetic mutation probe"
        for point in synthetic["cases"][0]["points"][4:]:
            point["scores"]["paired_history_rr"] = 0.9
        page.locator("#import-file").set_input_files({
            "name": "future-change.json", "mimeType": "application/json",
            "buffer": json.dumps(synthetic).encode(),
        })
        expect(page.locator("#source-description")).to_contain_text("Future-only synthetic mutation probe")
        expect(page.locator("#import-error")).to_be_hidden()
        seek(3)
        assert page.locator("#risk-chart").inner_html() == before
        checks.append("future values cannot alter current chart or its scaling")

        # Test a hostile label and forged quality/scores without importing any real case.
        synthetic["cases"][0]["label"] = '<img src=x onerror="window.injected=true">'
        synthetic["mode"] = "local_research"
        page.locator("#import-file").set_input_files({
            "name": "synthetic-schema-test.json", "mimeType": "application/json",
            "buffer": json.dumps(synthetic).encode(),
        })
        expect(page.locator("#score-kind")).to_have_text("研究模型分數")
        expect(page.locator("#case-title")).to_have_text(synthetic["cases"][0]["label"])
        assert page.locator("#case-title img").count() == 0
        assert not page.evaluate("Boolean(window.injected)")
        bad = json.loads(json.dumps(synthetic))
        bad["cases"][2]["points"][4]["scores"]["paired_history_rr"] = 0.2
        page.locator("#import-file").set_input_files({
            "name": "invalid-quality.json", "mimeType": "application/json",
            "buffer": json.dumps(bad).encode(),
        })
        expect(page.locator("#import-error")).to_be_visible()
        expect(page.locator("#case-title")).to_have_text(synthetic["cases"][0]["label"])
        checks.append("local JSON import / DOM escaping / invalid quality rejection preserves current dataset")

        page.locator("#restore-demo").click()
        expect(page.locator("#score-kind")).to_have_text("合成示意分數")
        seek(3)
        with page.expect_download() as download_info:
            page.locator("#export-summary").click()
        download = download_info.value
        download.save_as(OUTPUT / "synthetic-observation.json")
        exported = json.loads((OUTPUT / "synthetic-observation.json").read_text())
        assert exported
        checks.append("observation export / restored synthetic data")

        page.locator('[data-view="evidence"]').first.click()
        expect(page.locator("#view-evidence")).to_be_visible()
        expect(page.locator("#view-evidence")).to_contain_text("0.02419")
        expect(page.locator("#view-evidence")).to_contain_text("630")
        page.screenshot(path=str(OUTPUT / "evidence-desktop.png"), full_page=True)
        page.locator('[data-view="replay"]').first.click()
        seek(14)
        page.screenshot(path=str(OUTPUT / "replay-desktop.png"), full_page=True)
        # Small synthetic-only previews also allow review through text-only CI clients.
        preview = page.screenshot(type="jpeg", quality=60, full_page=True)
        print("DEMO_DESKTOP_JPEG=" + base64.b64encode(preview).decode(), flush=True)
        checks.append("aggregate research evidence and desktop render")

        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(OUTPUT / "replay-mobile.png"), full_page=True)
        preview = page.screenshot(type="jpeg", quality=60, full_page=True)
        print("DEMO_MOBILE_JPEG=" + base64.b64encode(preview).decode(), flush=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "Mobile horizontal overflow"
        page.locator('[data-view="guide"]').first.click()
        expect(page.locator("#view-guide")).to_be_visible()
        page.get_by_role("button", name="開始病例重播").click()
        expect(page.locator("#view-replay")).to_be_visible()
        checks.append("mobile layout and navigation")

        assert not errors, errors
        assert not requests, requests
        assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
        checks.append("no JavaScript errors, external requests, or persisted browser data")
        # Exercise the actual optional loopback server in the normal CI environment.
        server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            local_page = context.new_page()
            local_page.goto(base_url)
            expect(local_page.locator("#case-list button")).to_have_count(3)
            expect(local_page.locator("#risk-chart svg")).to_be_visible()
            for path, headers, expected_status in [
                ("/artifacts/replay/replay.json", {}, 404),
                ("/.git/config", {}, 404),
                ("/", {"Host": "example.com"}, 403),
            ]:
                try:
                    with urlopen(Request(base_url + path, headers=headers), timeout=5):
                        raise AssertionError("Private path or untrusted Host was served")
                except HTTPError as error:
                    assert error.code == expected_status
            local_page.close()
        finally:
            server.shutdown()
            worker.join(timeout=5)
            server.server_close()
        checks.append("actual loopback HTTP load / private file isolation / Host check")
        browser.close()
    result = {"status": "passed", "browser": "Chromium", "transport": "file://", "checks": checks}
    (OUTPUT / "checks.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
