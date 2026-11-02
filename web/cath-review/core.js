"use strict";
/* No patient files, clinical labels, external libraries or network calls. */
(function (global) {
  const LEADS = Object.freeze(["I", "II", "III", "aVR", "aVL", "aVF", "V1", "V2", "V3", "V4", "V5", "V6"]);
  const ROOT_KEYS = ["schema_version", "case_id", "age_years", "sex", "sample_rate_hz", "lead_names", "signals_mv", "representation", "provenance"];
  const STATUSES = ["applicable_positive", "applicable_negative", "alternative_criteria", "indeterminate"];
  const NEWNESS = ["new", "presumed_new", "old", "unknown"];
  const FINDINGS = ["bundle_branch_block", "ventricular_pacing", "left_ventricular_hypertrophy", "posterior_pattern", "other_occlusion_pattern"];
  function fail(message) { throw new Error(message); }
  function exactKeys(value, keys) {
    return value !== null && typeof value === "object" && !Array.isArray(value) &&
      Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
  }
  function validateCase(value) {
    if (!exactKeys(value, ROOT_KEYS)) fail("病例欄位不符：僅接受固定盲性 schema，不接受額外欄位、診斷或分數。");
    if (value.schema_version !== "cath-ecg-review-v1") fail("病例 schema 版本不支援。");
    if (typeof value.case_id !== "string" || !/^cr-[0-9a-f]{32}$/.test(value.case_id)) fail("病例代碼必須是 cr- 加 32 位小寫十六進位盲性代碼。");
    if (value.age_years !== null && (!Number.isInteger(value.age_years) || value.age_years < 0 || value.age_years > 120)) fail("年齡必須是 0–120 的整數或 null。");
    if (!["male", "female", "unknown"].includes(value.sex)) fail("性別欄位不符。");
    if (value.sample_rate_hz !== 500) fail("本版僅接受已確認 500 Hz 的 ECG。");
    if (!["raw", "median"].includes(value.representation)) fail("波形形式必須為 raw 或 median。");
    if (!Array.isArray(value.lead_names) || value.lead_names.length !== 12 ||
        new Set(value.lead_names).size !== 12 || !value.lead_names.every(x => LEADS.includes(x))) fail("必須提供不重複且完整的十二導程名稱。");
    if (!Array.isArray(value.signals_mv) || value.signals_mv.length < 2 || value.signals_mv.length > 5000 ||
        !value.signals_mv.every(row => Array.isArray(row) && row.length === 12 && row.every(x => typeof x === "number" && Number.isFinite(x) && Math.abs(x) <= 100))) fail("波形須為 2–5000 × 12 的有限 mV 數值，且絕對值不超過 100 mV。");
    if (!exactKeys(value.provenance, ["waveform_sha256"]) || typeof value.provenance.waveform_sha256 !== "string" ||
        !/^[0-9a-f]{64}$/.test(value.provenance.waveform_sha256)) fail("來源 waveform_sha256 必須是 64 位小寫十六進位。");
    return value;
  }
  function canonicalJSON(value) {
    if (Array.isArray(value)) return "[" + value.map(canonicalJSON).join(",") + "]";
    if (value !== null && typeof value === "object") return "{" + Object.keys(value).sort().map(k => JSON.stringify(k) + ":" + canonicalJSON(value[k])).join(",") + "}";
    return JSON.stringify(value);
  }
  async function fingerprint(value) {
    if (!global.crypto || !global.crypto.subtle) fail("瀏覽器無法計算 SHA-256，請使用支援的現代瀏覽器。");
    const bytes = new TextEncoder().encode(canonicalJSON(value));
    const digest = await global.crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), x => x.toString(16).padStart(2, "0")).join("");
  }
  function validateReview(review) {
    if (!/^[A-Za-z0-9_-]{1,40}$/.test(review.reviewer_id)) fail("請填入 1–40 字的判讀者代碼，只用英數、底線或減號。");
    if (!STATUSES.includes(review.index_ecg_ste_amplitude_status)) fail("請先完成一般 ST 上升幅度規則的判讀。");
    if (!NEWNESS.includes(review.newness)) fail("新發狀態不符。");
    if (!exactKeys(review.findings, FINDINGS) || !Object.values(review.findings).every(x => x === null || typeof x === "boolean")) fail("型態旗標必須為是／否／未判定。");
    if (typeof review.notes !== "string" || review.notes.length > 2000) fail("備註不可超過 2,000 字。");
    return review;
  }
  function makeSynthetic() {
    const fs = 500, n = 5000;
    const g = (x, centre, width) => Math.exp(-0.5 * ((x - centre) / width) ** 2);
    const scales = [0.65, 1, 0.35, -0.8, 0.15, 0.7, -0.4, 0.55, 1.2, 1.1, 0.85, 0.65];
    const signals = Array.from({length: n}, (_, i) => {
      const phase = (i / fs) % 0.85;
      const beat = 0.13*g(phase,0.12,0.025)-0.15*g(phase,0.245,0.01)+g(phase,0.27,0.009)-0.25*g(phase,0.3,0.012)+0.26*g(phase,0.5,0.055);
      return scales.map((scale, j) => Math.round((beat*scale+0.012*Math.sin(i/fs*2*Math.PI*0.2+j))*1000000)/1000000);
    });
    return {schema_version:"cath-ecg-review-v1",case_id:"cr-00000000000000000000000000000001",age_years:55,sex:"female",sample_rate_hz:500,lead_names:[...LEADS],signals_mv:signals,representation:"raw",provenance:{waveform_sha256:"0".repeat(64)}};
  }
  const api = Object.freeze({LEADS, FINDINGS, validateCase, validateReview, canonicalJSON, fingerprint, makeSynthetic});
  global.CathReviewCore = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
