#!/usr/bin/env python3
"""Exercise the standalone cath ECG reviewer with generated synthetic data only."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "artifacts" / "browser" / "cath-review"
LEADS = ["I", "II", "III", "aVR", "aVL", "aVF", "V1", "V2", "V3", "V4", "V5", "V6"]


def fixture() -> dict:
    # Reverse the input columns to verify that display mapping uses lead names.
    return {"schema_version": "cath-ecg-review-v1",
            "case_id": "cr-1234567890abcdef1234567890abcdef", "age_years": 54,
            "sex": "male", "sample_rate_hz": 500, "lead_names": LEADS[::-1],
            "signals_mv": [[round((i % 500) / 5000 + j * .02, 6)
                            for j in reversed(range(12))] for i in range(5000)],
            "representation": "raw", "provenance": {"waveform_sha256": "a" * 64}}


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    checks: list[str] = []
    errors: list[str] = []
    external_requests: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda err: errors.append(str(err)))
        context.on("request", lambda request: external_requests.append(request.url)
                   if request.url.startswith(("http://", "https://")) else None)
        page.goto((ROOT / "web" / "cath-review" / "index.html").as_uri())
        expect(page.locator("#source-kind")).to_have_text("尚未載入")
        expect(page.locator("#export-annotation")).to_be_disabled()
        expect(page.locator("#lead-grid canvas")).to_have_count(12)
        page.locator("#load-demo").click()
        expect(page.locator("#source-kind")).to_have_text("合成練習 · 非真實病例")
        expect(page.locator("#case-meta")).to_contain_text("5000 個取樣點")
        expect(page.locator("#amplitude-status")).to_have_value("")
        assert page.locator("#findings select").evaluate_all("els => els.every(e => e.value === 'unknown')")
        checks.append("offline load, explicitly synthetic demo, all 12 leads, no prefilled interpretation")

        def import_case(value: dict, filename: str = "synthetic-blinded-fixture.json") -> None:
            page.locator("#import-file").set_input_files({
                "name": filename, "mimeType": "application/json",
                "buffer": json.dumps(value).encode(),
            })

        data = fixture()
        import_case(data, "PRIVATE_PATIENT_NAME_OMI_POSITIVE.json")
        expect(page.locator("#case-id")).to_have_text(data["case_id"])
        expect(page.locator("#import-error")).to_be_hidden()
        assert "PRIVATE_PATIENT_NAME" not in page.locator("body").inner_text()
        assert page.locator("#import-file").input_value() == ""
        expect(page.locator("#case-meta")).to_contain_text("男性")
        canonical = page.evaluate("value => CathReviewCore.canonicalJSON(value)", data)
        expected_hash = hashlib.sha256(canonical.encode()).hexdigest()
        expect(page.locator("#fingerprint")).to_contain_text(expected_hash)
        checks.append("strict blind import, source filename hidden, canonical content fingerprint verified")

        canvas = page.locator('canvas[data-lead="II"]')
        canvas.focus()
        canvas.press("Enter")  # First sample, whose lead II voltage is .02 mV.
        canvas.press("ArrowRight")
        expect(page.locator("#cursor-readout")).to_contain_text("0.0202 mV")
        expect(page.locator("#cursor-readout")).to_contain_text("Δt=0.002 秒，ΔV=0.0002 mV")
        page.locator("#window-seconds").select_option("5")
        page.locator("#start-time").evaluate("el => {el.value = '2'; el.dispatchEvent(new Event('input', {bubbles:true}));}")
        expect(page.locator("#start-value")).to_have_text("2.000 秒")
        page.locator("#amplitude-range").select_option("1")
        page.locator("#clear-cursor").click()
        expect(page.locator("#cursor-readout")).to_contain_text("移動游標")
        checks.append("lead-name mapping, exact sample cursor, manual delta, time and amplitude controls")

        # Every invalid import must preserve the active case, never render labels or file names.
        mutations = []
        bad = copy.deepcopy(data); bad["OMI"] = 1; mutations.append(bad)
        bad = copy.deepcopy(data); bad["provenance"]["diagnosis"] = "<img src=x onerror=alert(1)>"; mutations.append(bad)
        bad = copy.deepcopy(data); bad["schema_version"] = "future-v2"; mutations.append(bad)
        bad = copy.deepcopy(data); bad["lead_names"][0] = "II"; mutations.append(bad)
        bad = copy.deepcopy(data); bad["signals_mv"][2][1] = None; mutations.append(bad)
        bad = copy.deepcopy(data); bad["signals_mv"][0].append(2); mutations.append(bad)
        bad = copy.deepcopy(data); bad["signals_mv"][0][0] = 101; mutations.append(bad)
        bad = copy.deepcopy(data); bad["sample_rate_hz"] = 250; mutations.append(bad)
        bad = copy.deepcopy(data); bad["case_id"] = "Patient_123_ECG_456"; mutations.append(bad)
        bad = copy.deepcopy(data); bad["provenance"]["waveform_sha256"] = "not-a-hash"; mutations.append(bad)
        for bad in mutations:
            import_case(bad)
            expect(page.locator("#import-error")).to_be_visible()
            expect(page.locator("#case-id")).to_have_text(data["case_id"])
            assert page.locator("body img").count() == 0
        page.locator("#import-file").set_input_files({"name": "invalid.json", "mimeType": "application/json", "buffer": b"{broken"})
        expect(page.locator("#import-error")).to_contain_text("不是有效 JSON")
        checks.append("reject unknown fields, hidden outcomes, wrong schema, malformed arrays, units/rate, identifiers and JSON")

        import_case(data)
        expect(page.locator("#import-error")).to_be_hidden()
        page.locator("#reviewer-id").fill("reader_01")
        page.locator("#export-annotation").click()
        assert page.locator("#amplitude-status").evaluate("el => !el.checkValidity()")
        page.locator("#amplitude-status").select_option("indeterminate")
        page.locator("#finding-bundle_branch_block").select_option("yes")
        page.locator("#finding-ventricular_pacing").select_option("no")
        page.locator("#notes").fill("Synthetic interaction check only; no medical interpretation.")
        with page.expect_download() as download_info:
            page.locator("#export-annotation").click()
        path = OUTPUT / "synthetic-import-annotation.json"
        download_info.value.save_as(path)
        annotation = json.loads(path.read_text())
        assert annotation["schema_version"] == "cath-ecg-annotation-v1"
        assert annotation["case_sha256"] == expected_hash
        assert annotation["case_id"] == data["case_id"]
        assert annotation["waveform_sha256"] == "a" * 64
        assert annotation["index_ecg_ste_amplitude_status"] == "indeterminate"
        assert annotation["newness"] == "unknown"
        assert annotation["findings"]["bundle_branch_block"] is True
        assert annotation["findings"]["ventricular_pacing"] is False
        assert annotation["findings"]["posterior_pattern"] is None
        assert annotation["synthetic"] is False  # Import provenance is unknown; fixture is generated by this test.
        assert set(annotation) == {"schema_version", "tool_version", "case_id", "case_sha256",
            "case_hash_algorithm", "waveform_sha256", "representation", "synthetic", "created_at_utc",
            "reviewer_id", "index_ecg_ste_amplitude_status", "newness", "findings", "notes"}
        checks.append("required explicit interpretation, annotation download with fingerprint, ternary flags and no waveforms")

        median = copy.deepcopy(data)
        median["representation"] = "median"
        median["case_id"] = "cr-abcdef1234567890abcdef1234567890"
        median["signals_mv"] = median["signals_mv"][:600]
        median["age_years"] = None; median["sex"] = "unknown"
        median["signals_mv"][10][0] = 4
        import_case(median)
        expect(page.locator("#case-id")).to_have_text(median["case_id"])
        expect(page.locator("#representation-note")).to_contain_text("不能當作連續十秒節律")
        expect(page.locator("#case-meta")).to_contain_text("年齡未知 · 性別未知")
        expect(page.locator("#reviewer-id")).to_have_value("")
        expect(page.locator("#amplitude-status")).to_have_value("")
        expect(page.locator("#finding-bundle_branch_block")).to_have_value("unknown")
        expect(page.locator("#clipping-note")).to_be_visible()
        page.locator("#amplitude-range").select_option("5")
        expect(page.locator("#clipping-note")).to_be_hidden()
        checks.append("median/unknown demographics marked, case switch clears annotations, clipping disclosed without altering data")

        page.locator("#load-demo").click()
        expect(page.locator("#source-kind")).to_contain_text("合成練習")
        page.locator("#reviewer-id").fill("reader_demo")
        page.locator("#amplitude-status").select_option("indeterminate")
        with page.expect_download() as demo_download:
            page.locator("#export-annotation").click()
        demo_path = OUTPUT / "synthetic-demo-annotation.json"
        demo_download.value.save_as(demo_path)
        assert json.loads(demo_path.read_text())["synthetic"] is True
        page.screenshot(path=str(OUTPUT / "desktop-synthetic.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.locator("#case-heading")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(OUTPUT / "mobile-synthetic.png"), full_page=True)
        checks.append("synthetic annotation flag, desktop/mobile layout and no horizontal overflow")

        assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
        assert external_requests == [], external_requests
        assert errors == [], errors
        checks.append("no network requests, browser errors or persistent storage")
        context.close(); browser.close()
    result = {"status": "passed", "scope": "Generated synthetic fixtures only; no clinical waveform or interpretation validation.", "checks": checks}
    (OUTPUT / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
