/* Authored synthetic scenarios for interface demonstration only. No patient data,
   no fitted model, and no efficacy claims are represented by these values. */
(function (root) {
  "use strict";
  var thresholds = {vitals_all: 0.03288167697, paired_vitals: 0.025036, paired_current_rr: 0.026654, paired_history_rr: 0.028315, paired_history_trend_rr: 0.029411};
  var models = Object.keys(thresholds);
  function round(value, digits) { return Number(value.toFixed(digits)); }
  function makeCase(kind, label, description, eventHour, count) {
    var points = [];
    for (var i = 0; i < count; i += 1) {
      var hour = 6 + i;
      var change = kind === "changing" ? Math.max(0, Math.min(1, (hour - 11) / 10)) : 0;
      var accepted = kind !== "interrupted" || ![10, 11, 16, 17, 18, 25].includes(hour);
      var missingVital = kind === "interrupted" && [17, 18].includes(hour);
      var heartRate = 77 + Math.sin(i * 0.65) * 3 + change * 33 + (kind === "interrupted" ? 8 : 0);
      var rr = 60000 / heartRate;
      var baseScore = 0.008 + 0.002 * Math.sin(i * 0.52) + change * 0.047;
      if (kind === "interrupted") baseScore += 0.009 + 0.008 * Math.sin(i * 0.34);
      var scores = {};
      models.forEach(function (model, modelIndex) {
        var value = baseScore * [1.06, 1, 1.09, 1.16, 1.12][modelIndex] + (modelIndex > 1 ? 0.0015 * Math.sin(i * 0.43 + modelIndex) : 0);
        scores[model] = (model !== "vitals_all" && !accepted) || (eventHour !== null && hour >= eventHour) ? null : round(Math.max(0, value), 6);
      });
      points.push({
        hour: hour,
        vitals: {heart_rate_bpm: missingVital ? null : round(heartRate, 1), map_mmhg: missingVital ? null : round(83 + Math.cos(i * 0.48) * 3 - change * 20, 1), respiratory_rate_bpm: round(16 + Math.sin(i * 0.42) + change * 8, 1), spo2_pct: round(98 - change * 3 + Math.sin(i * 0.35) * 0.5, 1)},
        rr: {mean_rr_ms: accepted ? round(rr, 1) : null, sdrr_ms: accepted ? round(31 + Math.sin(i * 0.6) * 3 - change * 12, 2) : null, rmssd_rr_ms: accepted ? round(22 + Math.cos(i * 0.5) * 3 - change * 10, 2) : null, accepted: accepted, reason_codes: accepted ? [] : (hour % 2 === 0 ? ["nonfinite_samples"] : ["low_block_beat_agreement", "insufficient_valid_rr_duration"]), rhythm_status: "not_assessed"},
        scores: scores
      });
    }
    return {id: "demo-" + kind, label: label, description: description, event_hour: eventHour, followup_end_hour: 6 + count, thresholds: Object.assign({}, thresholds), points: points};
  }
  root.HRV_DEMO = {
    schema_version: "hrv-replay-v1",
    mode: "synthetic",
    source_label: "三個人工設計的合成情境；所有分數均為示意，未使用真實患者資料。",
    cases: [
      makeCase("stable", "穩定觀察", "觀察規律更新的生命徵象與完整取樣覆蓋。", null, 25),
      makeCase("changing", "循環變化", "觀察合成生理變化、門檻跨越，以及事件後停止出分。", 24, 22),
      makeCase("interrupted", "訊號間斷", "觀察缺值與品質拒絕時，RR 模型如何保持不出分。", null, 25)
    ]
  };
})(typeof window === "undefined" ? globalThis : window);
