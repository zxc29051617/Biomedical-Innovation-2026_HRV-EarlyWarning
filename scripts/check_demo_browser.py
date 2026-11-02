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

        expect(page.locator("#monitor-title")).to_be_visible()
        expect(page.locator(".monitor-gate")).to_contain_text("RR 增強未啟用")
        expect(page.locator(".monitor-freshness")).to_have_text("資料新鮮度：未提供")
        # Pure audit logic must not interpolate gaps, approve RR or use future data.
        assert page.evaluate("""() => {
            const core = HRVAppCore;
            const item = JSON.parse(JSON.stringify(HRV_DEMO.cases[0]));
            const before = JSON.stringify(core.monitoringTrace(item, 3, 'synthetic', false));
            item.points.slice(4).forEach(p => {p.vitals.heart_rate_bpm = 200; p.scores.vitals_all = 0.9;});
            item.event_hour = 24;
            if (JSON.stringify(core.monitoringTrace(item, 3, 'synthetic', false)) !== before) return false;
            const gap = JSON.parse(JSON.stringify(HRV_DEMO.cases[0]));
            gap.points.splice(1, 1);
            const gaps = core.monitoringTrace(gap, 1, 'synthetic', false);
            if (gaps.rows.length !== 2 || gaps.coverage.provided_eligible_updates !== 2 || gaps.unfilled_time_gaps.length !== 1) return false;
            const missing = JSON.parse(JSON.stringify(HRV_DEMO.cases[0]));
            Object.keys(missing.points[0].vitals).forEach(key => {missing.points[0].vitals[key] = null;});
            const sparse = core.monitoringTrace(missing, 0, 'synthetic', false).rows[0];
            if (sparse.path !== 'baseline_with_all_vitals_missing' || sparse.visible_vital_count !== 0 || !sparse.baseline_score_available) return false;
            missing.points[0].scores.vitals_all = null;
            if (core.monitoringTrace(missing, 0, 'synthetic', false).rows[0].path !== 'baseline_unavailable') return false;
            const ended = core.monitoringTrace(HRV_DEMO.cases[1], 18, 'synthetic', true).rows[18];
            if (ended.path !== 'stopped' || ended.baseline_score !== null || ended.rr_research_available || ended.visible_vital_count !== null) return false;
            const terminal = core.monitoringTrace(HRV_DEMO.cases[0], HRV_DEMO.cases[0].points.length, 'synthetic', true).rows.at(-1);
            if (terminal.path !== 'stopped' || terminal.eligible_update) return false;
            const local = core.monitoringTrace(HRV_DEMO.cases[0], 2, 'local_research', true);
            return local.view === 'source_replay' && local.rows.every(r => r.rr_view === 'source_replay' && r.rr_enhancement_enabled === false)
                && local.evidence_gate.incremental_value === 'not_supported_by_development_comparison';
        }""")
        checks.append("monitoring audit preserves gaps, missing-input warnings, evidence gate, time cutoff and stop states")

        seek(3)
        risk_before = page.locator("#risk-chart").inner_html()
        score_before = page.locator("#risk-value").inner_text()
        notices_before = page.locator("#notification-count").inner_text()
        baseline_before = page.locator("#monitor-baseline-score").inner_text()
        source_before = page.evaluate("JSON.stringify(HRV_DEMO)")
        page.locator("#simulate-ecg").check()
        expect(page.locator("#monitor-view")).to_have_text("ECG 中斷演練")
        expect(page.locator("#monitor-rr-state")).to_have_text("演練：ECG 中斷")
        expect(page.locator("#monitor-baseline-score")).to_have_text(baseline_before)
        assert page.locator("#risk-chart").inner_html() == risk_before
        assert page.locator("#risk-value").inner_text() == score_before
        assert page.locator("#notification-count").inner_text() == notices_before
        assert page.evaluate("JSON.stringify(HRV_DEMO)") == source_before
        with page.expect_download() as monitor_download_info:
            page.locator("#export-monitoring").click()
        monitor_download_info.value.save_as(OUTPUT / "synthetic-monitoring-trace.json")
        trace = json.loads((OUTPUT / "synthetic-monitoring-trace.json").read_text())
        assert trace["view"] == "simulated_ecg_interruption"
        assert trace["cutoff_relative_hour"] == 9
        assert len(trace["rows"]) == 4
        assert all(row["relative_hour"] <= 9 and not row["rr_research_available"] for row in trace["rows"])
        assert trace["coverage"]["provided_eligible_updates"] == 4
        assert trace["coverage"]["denominator"] == "provided_eligible_update_points_only"
        assert "event_hour" not in trace and "followup_end_hour" not in trace
        assert not trace["evidence_gate"]["rr_enhancement_enabled"]
        page.locator("#simulate-ecg").uncheck()
        expect(page.locator("#monitor-rr-state")).to_have_text("工程片段可研究檢視")
        checks.append("synthetic ECG interruption changes panel only; trace download contains cutoff and simulation provenance")

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

        page.locator("#simulate-ecg").check()
        page.locator("#case-list button").nth(2).click()
        expect(page.locator("#simulate-ecg")).not_to_be_checked()
        seek(4)  # Synthetic hour 10 is explicitly rejected.
        expect(page.locator("#risk-value")).to_have_text("—")
        expect(page.locator("#rmssd")).to_have_text("—")
        page.locator("#model-select").select_option("vitals_all")
        expect(page.locator("#risk-value")).not_to_have_text("—")
        page.locator("#model-select").select_option("paired_history_rr")
        expect(page.locator("#monitor-rr-state")).to_have_text("RR 暫停研究檢視")
        expect(page.locator("#monitor-baseline-score")).not_to_have_text("—")
        seek(11)  # Hour 17 has two observed vital inputs, not four fresh readings.
        expect(page.locator("#monitor-input-count")).to_have_text("2 / 4 項有數值")
        expect(page.locator("#monitor-input-detail")).to_contain_text("未提供：心率、平均動脈壓")
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
        page.locator("#simulate-ecg").check()
        page.locator("#import-file").set_input_files({
            "name": "synthetic-schema-test.json", "mimeType": "application/json",
            "buffer": json.dumps(synthetic).encode(),
        })
        expect(page.locator("#score-kind")).to_have_text("研究模型分數")
        expect(page.locator("#case-title")).to_have_text(synthetic["cases"][0]["label"])
        expect(page.locator("#simulate-ecg")).to_be_disabled()
        expect(page.locator("#simulate-ecg")).not_to_be_checked()
        expect(page.locator("#monitor-view")).to_have_text("原始重播視圖")
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
        checks.append("case changes reset rehearsal; research import disables rehearsal and preserves separate source view")

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
        expect(page.locator("#controls-evidence")).to_contain_text("0.03046")
        expect(page.locator("#controls-evidence")).to_contain_text("−0.00289")
        expect(page.locator("#controls-evidence")).to_contain_text("[−0.01540, 0.01764]")
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
