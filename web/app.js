/* Offline replay. No requests, persistence, inference, or clinical decisions. */
(function (root) {
  "use strict";
  var MODELS = ["vitals_all", "paired_vitals", "paired_current_rr", "paired_history_rr", "paired_history_trend_rr"];
  var LABELS = {vitals_all: "全風險集合常規基準", paired_vitals: "配對常規資訊", paired_current_rr: "常規資訊＋當下 RR", paired_history_rr: "常規資訊＋歷史 RR", paired_history_trend_rr: "常規資訊＋歷史 RR＋斜率"};
  var METRICS = [
    {key: "heart_rate_bpm", label: "心率", unit: "bpm", max: 400},
    {key: "map_mmhg", label: "平均動脈壓", unit: "mmHg", max: 400},
    {key: "respiratory_rate_bpm", label: "呼吸頻率", unit: "/min", max: 200},
    {key: "spo2_pct", label: "血氧飽和度", unit: "%", max: 100}
  ];
  var RR_KEYS = ["mean_rr_ms", "sdrr_ms", "rmssd_rr_ms"];
  var MAX_FILE_BYTES = 8 * 1024 * 1024;
  var EPSILON = 0.000001;
  function fail(where, message) { throw new Error(where + "：" + message); }
  function object(value, where) {
    if (!value || typeof value !== "object" || Array.isArray(value)) fail(where, "必須是物件");
    return value;
  }
  function string(value, where, max) {
    if (typeof value !== "string" || !value.trim() || value.length > max || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value)) fail(where, "字串空白、過長或含有不支援字元");
    return value;
  }
  function number(value, where, min, max, nullable) {
    if (nullable && value === null) return null;
    if (typeof value !== "number" || !Number.isFinite(value) || value < min || value > max) fail(where, "必須是 " + min + " 至 " + max + " 的有限數值" + (nullable ? " 或 null" : ""));
    return value;
  }
  function validateDataset(input) {
    object(input, "根物件");
    if (input.schema_version !== "hrv-replay-v1") fail("schema_version", "需要 hrv-replay-v1");
    if (!["synthetic", "local_research"].includes(input.mode)) fail("mode", "需要 synthetic 或 local_research");
    var source = string(input.source_label, "source_label", 500);
    if (!Array.isArray(input.cases) || input.cases.length < 1 || input.cases.length > 50) fail("cases", "需要 1 至 50 個病例");
    var ids = new Set();
    var total = 0;
    var cases = input.cases.map(function (original, ci) {
      var where = "病例 " + (ci + 1);
      object(original, where);
      var id = string(original.id, where + ".id", 120);
      if (ids.has(id)) fail(where + ".id", "不可重複");
      ids.add(id);
      var label = string(original.label, where + ".label", 100);
      var description = string(original.description, where + ".description", 800);
      var end = number(original.followup_end_hour, where + ".followup_end_hour", 0, 87600, false);
      var event = number(original.event_hour, where + ".event_hour", 0, end, true);
      object(original.thresholds, where + ".thresholds");
      var thresholds = {};
      MODELS.forEach(function (model) { thresholds[model] = number(original.thresholds[model], where + ".thresholds." + model, 0, 1, false); });
      if (!Array.isArray(original.points) || original.points.length < 1 || original.points.length > 4000) fail(where + ".points", "需要 1 至 4,000 個時點");
      total += original.points.length;
      if (total > 20000) fail("cases", "總時點不可超過 20,000");
      var previous = -1;
      var points = original.points.map(function (point, pi) {
        var at = where + " 時點 " + (pi + 1);
        object(point, at);
        var hour = number(point.hour, at + ".hour", 0, end, false);
        if (hour <= previous) fail(at + ".hour", "必須嚴格遞增、不可重複");
        previous = hour;
        object(point.vitals, at + ".vitals");
        var vitals = {};
        METRICS.forEach(function (metric) { vitals[metric.key] = number(point.vitals[metric.key], at + ".vitals." + metric.key, 0, metric.max, true); });
        object(point.rr, at + ".rr");
        if (typeof point.rr.accepted !== "boolean") fail(at + ".rr.accepted", "需要 boolean");
        if (point.rr.rhythm_status !== "not_assessed") fail(at + ".rr.rhythm_status", "目前僅支援 not_assessed");
        if (!Array.isArray(point.rr.reason_codes) || point.rr.reason_codes.length > 20) fail(at + ".rr.reason_codes", "需要至多 20 個原因代碼");
        var reasons = point.rr.reason_codes.map(function (reason) {
          if (typeof reason !== "string" || !/^[a-z][a-z0-9_]{0,79}$/.test(reason)) fail(at + ".rr.reason_codes", "原因代碼格式不支援");
          return reason;
        });
        var rr = {accepted: point.rr.accepted, rhythm_status: "not_assessed", reason_codes: reasons};
        if (rr.accepted && reasons.length) fail(at + ".rr", "工程通過不能同時附帶品質拒絕原因");
        RR_KEYS.forEach(function (key) {
          rr[key] = number(point.rr[key], at + ".rr." + key, key === "mean_rr_ms" ? 0.001 : 0, 10000, !rr.accepted);
          if (!rr.accepted && rr[key] !== null) fail(at + ".rr", "品質拒絕時 RR 特徵必須為 null");
        });
        if (Object.prototype.hasOwnProperty.call(point.rr, "history_valid_count")) {
          var count = number(point.rr.history_valid_count, at + ".rr.history_valid_count", 0, 6, true);
          if (count !== null && !Number.isInteger(count)) fail(at + ".rr.history_valid_count", "需要整數");
          rr.history_valid_count = count;
        }
        object(point.scores, at + ".scores");
        var scores = {};
        MODELS.forEach(function (model) {
          scores[model] = number(point.scores[model], at + ".scores." + model, 0, 1, true);
          if (!rr.accepted && model !== "vitals_all" && scores[model] !== null) fail(at + ".scores", "品質拒絕時配對模型必須不出分");
          if (event !== null && hour >= event && scores[model] !== null) fail(at + ".scores", "事件開始後必須停止出分");
          if (hour >= end && scores[model] !== null) fail(at + ".scores", "追蹤終點必須停止出分");
          if (input.mode === "local_research" && hour < 6 && scores[model] !== null) fail(at + ".scores", "研究時點在 ICU +6 小時以前不得出分");
        });
        return {hour: hour, vitals: vitals, rr: rr, scores: scores};
      });
      points.forEach(function (point, pi) {
        if (!Number.isInteger(point.rr.history_valid_count)) return;
        var accepted = 0, rejected = 0;
        for (var lag = 0; lag < 6; lag += 1) {
          var past = findPointAt(points, point.hour - lag, pi);
          if (past) { if (past.rr.accepted) accepted += 1; else rejected += 1; }
        }
        if (point.rr.history_valid_count < accepted || point.rr.history_valid_count > 6 - rejected) fail(where + " 時點 " + (pi + 1) + ".rr.history_valid_count", "與已提供的歷史品質片段數矛盾");
      });
      return {id: id, label: label, description: description, followup_end_hour: end, event_hour: event, thresholds: thresholds, points: points};
    });
    return {schema_version: "hrv-replay-v1", mode: input.mode, source_label: source, cases: cases};
  }
  function makeReplayPoints(item) {
    var points = item.points.slice();
    if (item.followup_end_hour > points[points.length - 1].hour + EPSILON) {
      var vitals = {}, rr = {accepted: false, rhythm_status: "not_assessed", reason_codes: ["risk_followup_ended"]}, scores = {};
      METRICS.forEach(function (m) { vitals[m.key] = null; });
      RR_KEYS.forEach(function (key) { rr[key] = null; });
      MODELS.forEach(function (key) { scores[key] = null; });
      points.push({hour: item.followup_end_hour, vitals: vitals, rr: rr, scores: scores, _terminal: true});
    }
    return points;
  }
  function visiblePoints(points, index) { return points.slice(0, index + 1); }
  function findPointAt(points, target, maxIndex) {
    var low = 0, high = maxIndex;
    while (low <= high) {
      var middle = Math.floor((low + high) / 2), point = points[middle];
      if (Math.abs(point.hour - target) < EPSILON) return point._terminal ? null : point;
      if (point.hour < target) low = middle + 1; else high = middle - 1;
    }
    return null;
  }
  function chartSegments(points, getter) {
    var segments = [], segment = [], previousHour = null;
    points.forEach(function (point) {
      var value = getter(point);
      if (value === null || typeof value !== "number" || !Number.isFinite(value)) {
        if (segment.length) segments.push(segment);
        segment = []; previousHour = null;
        return;
      }
      if (previousHour !== null && point.hour - previousHour > 1 + EPSILON) {
        if (segment.length) segments.push(segment);
        segment = [];
      }
      segment.push({hour: point.hour, value: value});
      previousHour = point.hour;
    });
    if (segment.length) segments.push(segment);
    return segments;
  }
  function historyCoverage(points, index) {
    var point = points[index];
    var slots = [];
    for (var ago = 5; ago >= 0; ago -= 1) {
      var target = point.hour - ago;
      var match = findPointAt(points, target, index);
      slots.push({hour: target, status: match ? (match.rr.accepted ? "accepted" : "rejected") : "unknown"});
    }
    var count = slots.filter(function (slot) { return slot.status === "accepted"; }).length;
    var fromFeature = Number.isInteger(point.rr.history_valid_count);
    if (fromFeature) count = point.rr.history_valid_count;
    return {count: point._terminal ? null : count, slots: slots, fromFeature: fromFeature};
  }
  function notificationEvents(points, model, threshold) {
    var times = [];
    points.forEach(function (point) {
      var value = point.scores[model];
      if (value !== null && value >= threshold && (!times.length || point.hour - times[times.length - 1] >= 6)) times.push(point.hour);
    });
    return times;
  }
  function arrivedEvent(item, hour, enabled) { return enabled && item.event_hour !== null && item.event_hour <= hour ? item.event_hour : null; }
  function monitoringTrace(item, index, mode, simulateEcg) {
    // This is a replay audit, not a new model or a calibrated routing policy.
    // Only provided points up to index are examined; unknown time gaps stay unknown.
    var simulated = mode === "synthetic" && simulateEcg === true;
    var points = visiblePoints(makeReplayPoints(item), index);
    var gaps = [], previous = null;
    var rows = points.map(function (point) {
      if (previous !== null && point.hour - previous > 1 + EPSILON) gaps.push({after_hour: previous, before_hour: point.hour});
      previous = point.hour;
      var ended = Boolean(point._terminal) || point.hour >= item.followup_end_hour || (item.event_hour !== null && point.hour >= item.event_hour);
      var eligible = !ended && point.hour >= 6;
      var present = eligible ? METRICS.filter(function (m) { return point.vitals[m.key] !== null; }).map(function (m) { return m.key; }) : [];
      var missing = eligible ? METRICS.filter(function (m) { return point.vitals[m.key] === null; }).map(function (m) { return m.key; }) : [];
      var score = eligible ? point.scores.vitals_all : null;
      var sourceRr = eligible && point.rr.accepted;
      var rrAvailable = sourceRr && !simulated;
      var reasons = ended ? ["risk_followup_ended"] : !eligible ? ["before_first_landmark"] : simulated ? ["simulated_ecg_interruption"] : sourceRr ? [] : point.rr.reason_codes.length ? point.rr.reason_codes.slice() : ["missing_rr_information"];
      var path = ended ? "stopped" : !eligible ? "before_first_landmark" : score === null ? "baseline_unavailable" : present.length === 0 ? "baseline_with_all_vitals_missing" : rrAvailable ? "baseline_and_rr_research" : "baseline_only";
      return {relative_hour: point.hour, eligible_update: eligible, path: path,
        baseline_model: "vitals_all", baseline_score: score, baseline_score_available: score !== null,
        visible_vital_count: eligible ? present.length : null, present_vital_keys: present, missing_vital_keys: missing,
        vital_freshness: "not_provided", source_rr_engineering_accepted: point.rr.accepted,
        rr_research_available: rrAvailable, rr_reason_codes: reasons,
        rr_view: eligible && simulated ? "simulated_interruption" : "source_replay",
        rr_enhancement_enabled: false};
    });
    var eligibleRows = rows.filter(function (row) { return row.eligible_update; });
    return {schema_version: "hrv-monitoring-trace-v1", mode: mode,
      view: simulated ? "simulated_ecg_interruption" : "source_replay",
      simulation_scope: simulated ? "all_visible_eligible_updates_in_this_panel_only" : null,
      cutoff_relative_hour: rows.length ? rows[rows.length - 1].relative_hour : null,
      evidence_gate: {rr_enhancement_enabled: false, timing_and_nn: "not_validated", incremental_value: "not_supported_by_development_comparison"},
      coverage: {denominator: "provided_eligible_update_points_only", provided_eligible_updates: eligibleRows.length,
        baseline_score_updates: eligibleRows.filter(function (row) { return row.baseline_score_available; }).length,
        rr_research_updates: eligibleRows.filter(function (row) { return row.rr_research_available; }).length,
        updates_with_all_vitals_missing: eligibleRows.filter(function (row) { return row.visible_vital_count === 0; }).length},
      unfilled_time_gaps: gaps, rows: rows};
  }
  root.HRVAppCore = {validateDataset: validateDataset, makeReplayPoints: makeReplayPoints, visiblePoints: visiblePoints, chartSegments: chartSegments, historyCoverage: historyCoverage, notificationEvents: notificationEvents, arrivedEvent: arrivedEvent, monitoringTrace: monitoringTrace, maxFileBytes: MAX_FILE_BYTES};
  if (typeof document === "undefined") return;

  var $ = function (id) { return document.getElementById(id); };
  var state = {dataset: validateDataset(root.HRV_DEMO), caseIndex: 0, index: 6, model: "paired_history_rr", timer: null, view: "replay", importVersion: 0, simulateEcg: false};
  var replayPoints = [];
  var vitalElements = {};
  var REASONS = {nonfinite_samples: "含有缺失或非有限訊號", low_block_beat_agreement: "分段搏動偵測一致性不足", insufficient_valid_rr_duration: "有效 RR 覆蓋時間不足", low_detector_agreement: "搏動偵測器一致性不足", low_beat_agreement: "搏動偵測一致性不足", excessive_rr_outliers: "異常 RR 比例過高", insufficient_beats: "可用搏動數不足", invalid_signal: "訊號無效", flat_signal: "訊號變化不足", high_clipping_fraction: "削波比例過高", missing_epoch: "未找到 ECG 片段", missing_features: "RR 特徵缺少", no_valid_epoch: "沒有有效 ECG 片段", risk_followup_ended: "已到達風險追蹤終點", no_matching_ecg_epoch: "未找到此時點的 ECG 片段", wrong_epoch_length: "片段長度不符", sampling_frequency_below_project_minimum: "取樣頻率低於研究規格", empty_signal: "訊號為空", flat_run: "存在過長平坦訊號", low_block_amplitude: "分段振幅不足", excessive_amplitude_range: "振幅範圍過大", suspected_clipping: "疑似訊號削波", detector_error: "搏動偵測失敗", too_few_beats: "可用搏動數不足", incomplete_beat_span: "搏動未覆蓋完整片段", too_many_screened_rr_intervals: "遭篩除 RR 比例過高", insufficient_adjacent_rr_intervals: "有效相鄰 RR 對不足", ecg_decode_unavailable: "ECG 無法解碼", other_quality_rejection: "其他工程品質拒絕", quality_rejected: "未通過工程品質篩選"};
  function node(tag, text, className) {
    var element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function text(id, value) { $(id).textContent = value; }
  function format(value, digits) { return value === null || value === undefined ? "—" : value.toLocaleString("zh-TW", {maximumFractionDigits: digits === undefined ? 1 : digits}); }
  function hour(value) { return "ICU +" + format(value, 2) + " 小時"; }
  function currentCase() { return state.dataset.cases[state.caseIndex]; }
  function scoreKind() { return state.dataset.mode === "synthetic" ? "合成示意分數" : "研究模型分數"; }
  function announce(message, isError) {
    text("app-message", message);
    $("app-message").className = "app-message" + (isError ? " error" : "");
    $("app-message").hidden = false;
  }
  function describeReasons(point) {
    if (point.rr.accepted) return "目前片段通過工程篩選；尚未確認節律與正常 NN。";
    if (!point.rr.reason_codes.length) return "未提供可接受的 RR 片段；不計算配對模型分數。";
    return point.rr.reason_codes.map(function (code) { return REASONS[code] || "品質原因：" + code; }).join("；") + "。";
  }
  function pause() {
    if (state.timer !== null) root.clearInterval(state.timer);
    state.timer = null;
    text("play-pause", "播放");
    $("play-pause").setAttribute("aria-pressed", "false");
  }
  function chooseCase(index) {
    pause();
    state.simulateEcg = false;
    state.caseIndex = index;
    replayPoints = makeReplayPoints(currentCase());
    state.index = Math.min(6, replayPoints.length - 1);
    $("show-event").checked = false;
    renderCaseList();
    render();
  }
  function renderCaseList() {
    var host = $("case-list"), preserveFocus = host.contains(document.activeElement); host.replaceChildren();
    state.dataset.cases.forEach(function (item, index) {
      var button = node("button", undefined, "case-button" + (index === state.caseIndex ? " active" : ""));
      button.type = "button";
      button.setAttribute("aria-pressed", String(index === state.caseIndex));
      button.append(node("span", (state.dataset.mode === "synthetic" ? "合成情境 " : "本地病例 ") + String(index + 1).padStart(2, "0"), "case-index"), node("strong", item.label), node("small", item.description));
      button.addEventListener("click", function () { chooseCase(index); });
      host.append(button);
      if (preserveFocus && index === state.caseIndex) button.focus();
    });
  }
  function buildVitals() {
    METRICS.forEach(function (metric) {
      var card = node("article", undefined, "card vital-card");
      var label = node("div", metric.label, "vital-label");
      var value = node("div", undefined, "vital-value");
      var numeric = node("span", "—");
      value.append(numeric, node("small", metric.unit));
      var chart = node("div", undefined, "vital-spark");
      card.append(label, value, chart);
      $("vital-grid").append(card);
      vitalElements[metric.key] = {value: numeric, chart: chart};
    });
  }
  var NS = "http://www.w3.org/2000/svg";
  function svgNode(tag, attributes, content) {
    var element = document.createElementNS(NS, tag);
    Object.keys(attributes || {}).forEach(function (key) { element.setAttribute(key, String(attributes[key])); });
    if (content !== undefined) element.textContent = content;
    return element;
  }
  function drawChart(host, points, series, options) {
    var spark = Boolean(options.spark);
    var width = Math.max(spark ? 150 : 270, host.clientWidth || (spark ? 220 : 760));
    var height = spark ? 57 : (options.rr ? 150 : 255);
    var margins = spark ? {left: 3, right: 3, top: 7, bottom: 5} : {left: 41, right: 14, top: 19, bottom: 27};
    var current = points[points.length - 1].hour;
    var start = points[0].hour;
    if (start === current) start = current - 1;
    var values = [];
    series.forEach(function (s) { points.forEach(function (p) { var value = s.get(p); if (value !== null && Number.isFinite(value)) values.push(value); }); });
    var hasObservedValues = values.length > 0;
    if (options.threshold !== undefined) values.push(options.threshold);
    var min = spark && values.length ? Math.min.apply(null, values) : 0;
    var max = values.length ? Math.max.apply(null, values) : (options.rr ? 40 : 0.05);
    if (spark) { var pad = Math.max(1, (max - min) * 0.2); min -= pad; max += pad; }
    else max = Math.max(options.rr ? 10 : 0.04, max * 1.2);
    var x = function (h) { return margins.left + (h - start) / (current - start) * (width - margins.left - margins.right); };
    var y = function (v) { return height - margins.bottom - (v - min) / (max - min) * (height - margins.top - margins.bottom); };
    var svg = svgNode("svg", {viewBox: "0 0 " + width + " " + height, role: "img", "aria-label": options.label});
    svg.append(svgNode("title", {}, options.label + "；僅含 " + hour(current) + " 以前資料，缺值留白。"));
    if (!spark) {
      for (var tick = 0; tick <= 3; tick += 1) {
        var val = min + (max - min) * tick / 3;
        svg.append(svgNode("line", {x1: margins.left, x2: width - margins.right, y1: y(val), y2: y(val), stroke: "#e8eef0", "stroke-width": 1}));
        svg.append(svgNode("text", {x: margins.left - 8, y: y(val) + 3, fill: "#7b8e97", "font-size": 9, "text-anchor": "end"}, options.rr ? format(val, 0) : format(val * 100, 1) + "%"));
      }
      var ticks = width < 420 ? 3 : 5;
      for (var xt = 0; xt <= ticks; xt += 1) {
        var time = start + (current - start) * xt / ticks;
        svg.append(svgNode("text", {x: x(time), y: height - 6, fill: "#7b8e97", "font-size": 9, "text-anchor": xt === 0 ? "start" : (xt === ticks ? "end" : "middle")}, "+" + format(time, 1) + "h"));
      }
      if (options.threshold !== undefined) svg.append(svgNode("line", {x1: margins.left, x2: width - margins.right, y1: y(options.threshold), y2: y(options.threshold), stroke: "#bf8a3b", "stroke-width": 1.2, "stroke-dasharray": "5 5"}));
    }
    series.forEach(function (s) {
      chartSegments(points, s.get).forEach(function (segment) {
        var d = segment.map(function (p, i) { return (i ? "L" : "M") + x(p.hour).toFixed(2) + " " + y(p.value).toFixed(2); }).join(" ");
        if (segment.length > 1) svg.append(svgNode("path", {d: d, fill: "none", stroke: s.color, "stroke-width": spark ? 1.8 : 2.2, "stroke-linecap": "round", "stroke-linejoin": "round"}));
        segment.forEach(function (p) {
          var mark = svgNode("circle", {cx: x(p.hour), cy: y(p.value), r: spark ? 1.6 : (p.hour === current ? 4 : 2.3), fill: s.color});
          mark.append(svgNode("title", {}, s.label + " · " + hour(p.hour) + " · " + format(p.value * (options.percent ? 100 : 1), 2) + (options.percent ? "%" : "")));
          svg.append(mark);
        });
      });
    });
    if (!hasObservedValues) svg.append(svgNode("text", {x: width / 2, y: height / 2, "text-anchor": "middle", fill: "#7b8e97", "font-size": spark ? 9 : 11}, "此時段無可用資料"));
    if (options.event !== null && options.event !== undefined && options.event >= start) {
      var eventX = x(options.event);
      svg.append(svgNode("line", {x1: eventX, x2: eventX, y1: margins.top, y2: height - margins.bottom, stroke: "#a74942", "stroke-dasharray": "3 4", "stroke-width": 1.5}));
      svg.append(svgNode("text", {x: eventX - 5, y: 11, fill: "#a74942", "font-size": 9, "text-anchor": "end"}, "回溯事件"));
    }
    host.replaceChildren(svg);
  }
  function renderCharts() {
    if (state.view !== "replay") return;
    var item = currentCase();
    var points = visiblePoints(replayPoints, state.index);
    var series = [];
    var paired = !["paired_vitals", "vitals_all"].includes(state.model);
    if (paired) series.push({label: LABELS.paired_vitals, color: "#9bafb7", get: function (p) { return p.scores.paired_vitals; }});
    series.push({label: LABELS[state.model], color: "#087e87", get: function (p) { return p.scores[state.model]; }});
    $("baseline-legend").hidden = !paired;
    drawChart($("risk-chart"), points, series, {label: scoreKind() + "時間軸", threshold: item.thresholds[state.model], percent: true, event: arrivedEvent(item, points[points.length - 1].hour, $("show-event").checked)});
    drawChart($("rr-chart"), points, [{label: "RMSSD of RR", color: "#087e87", get: function (p) { return p.rr.rmssd_rr_ms; }}, {label: "SDRR", color: "#5d7fc5", get: function (p) { return p.rr.sdrr_ms; }}], {rr: true, label: "RR 變異特徵（毫秒）"});
    METRICS.forEach(function (m) { drawChart(vitalElements[m.key].chart, points, [{label: m.label, color: "#369497", get: function (p) { return p.vitals[m.key]; }}], {spark: true, label: m.label + "（" + m.unit + "）"}); });
  }
  var MONITOR_PATHS = {stopped: "追蹤已結束", before_first_landmark: "尚未進入預警時點", baseline_unavailable: "缺少常規分數", baseline_with_all_vitals_missing: "四項生命徵象全缺失，仍有模型分數", baseline_and_rr_research: "常規基準＋RR 研究檢視", baseline_only: "常規基準；RR 暫停"};
  function monitorRrText(row) {
    if (!row.eligible_update) return "停止／尚未開始評估";
    if (row.rr_research_available) return "工程片段可研究檢視";
    if (row.rr_view === "simulated_interruption") return "演練：ECG 中斷";
    return "RR 暫停研究檢視";
  }
  function monitorReason(row) {
    if (!row.eligible_update) return row.path === "stopped" ? "已到達風險追蹤終點，不沿用先前分數。" : "尚未到達 ICU +6 小時的第一個預警時點。";
    if (row.rr_view === "simulated_interruption") return "本面板將 ECG 設為中斷；原始資料、圖表、模型分數與通知均未改動。";
    if (row.rr_research_available) return "工程通過只允許研究檢視；尚未確認正常 NN 與量測準確度。";
    return row.rr_reason_codes.map(function (code) { return REASONS[code] || (code === "missing_rr_information" ? "未提供可用 RR 資訊" : code); }).join("；") + "。";
  }
  function renderMonitoring() {
    var synthetic = state.dataset.mode === "synthetic";
    if (!synthetic) state.simulateEcg = false;
    $("simulate-ecg").disabled = !synthetic;
    $("simulate-ecg").checked = state.simulateEcg;
    var trace = monitoringTrace(currentCase(), state.index, state.dataset.mode, state.simulateEcg);
    var row = trace.rows[trace.rows.length - 1], count = trace.coverage;
    text("monitor-view", trace.view === "source_replay" ? "原始重播視圖" : "ECG 中斷演練");
    $("monitor-view").className = "pill " + (state.simulateEcg ? "amber" : "neutral");
    text("monitor-simulation-note", synthetic ? "演練會將本面板已到達的有效更新點視為 ECG 中斷；不改原始資料、其他圖表或通知。切換病例會重置。" : "本機研究病例停用演練。此面板只稽核匯入的既有分數與品質狀態。");
    text("monitor-baseline-score", row.baseline_score === null ? "—" : format(row.baseline_score * 100, 2) + "%");
    text("monitor-score-kind", scoreKind() + " · 固定常規基準");
    text("monitor-path", MONITOR_PATHS[row.path]);
    text("monitor-input-count", row.visible_vital_count === null ? "—" : row.visible_vital_count + " / 4 項有數值");
    text("monitor-input-detail", !row.eligible_update ? "此時點不評估輸入充分性。" : row.missing_vital_keys.length ? "未提供：" + METRICS.filter(function (m) { return row.missing_vital_keys.includes(m.key); }).map(function (m) { return m.label; }).join("、") + "。有分數仍可能依賴補值。" : "四項皆有數值；不代表剛完成測量，也不代表臨床可信度已驗證。");
    text("monitor-rr-state", monitorRrText(row)); text("monitor-rr-reason", monitorReason(row));
    text("monitor-coverage", "已提供的 " + count.provided_eligible_updates + " 個有效更新點：常規分數可檢視 " + count.baseline_score_updates + " 個，RR 可研究檢視 " + count.rr_research_updates + " 個。");
    text("monitor-gap-note", "僅計本病例已提供且已到達的更新點，不是患者時間覆蓋率。" + (trace.unfilled_time_gaps.length ? "另有 " + trace.unfilled_time_gaps.length + " 處時間缺口，未補值或補算。" : "未提供的時間不加入分母。"));
    var shown = trace.rows.slice(-24);
    text("monitor-trace-note", "顯示最近 " + shown.length + " / " + trace.rows.length + " 個已到達紀錄；匯出包含目前以前的完整軌跡。");
    $("monitor-trace-body").replaceChildren();
    shown.forEach(function (entry, index) {
      if (index > 0 && entry.relative_hour - shown[index - 1].relative_hour > 1 + EPSILON) {
        var gap = node("tr", undefined, "monitor-gap"), gapCell = node("td", "時間缺口 · 未提供更新，不補值");
        gapCell.colSpan = 4; gap.append(gapCell); $("monitor-trace-body").append(gap);
      }
      var tr = node("tr");
      tr.append(node("th", hour(entry.relative_hour)), node("td", MONITOR_PATHS[entry.path]), node("td", monitorRrText(entry)), node("td", entry.visible_vital_count === null ? "—" : entry.visible_vital_count + " / 4"));
      tr.firstChild.scope = "row"; $("monitor-trace-body").append(tr);
    });
  }
  function render() {
    var item = currentCase(), point = replayPoints[state.index], value = point.scores[state.model];
    var synthetic = state.dataset.mode === "synthetic";
    var terminal = point._terminal || point.hour >= item.followup_end_hour || (item.event_hour !== null && point.hour >= item.event_hour);
    var coverage = historyCoverage(replayPoints, state.index);
    var notices = notificationEvents(visiblePoints(replayPoints, state.index), state.model, item.thresholds[state.model]);
    text("case-title", item.label); text("case-description", item.description); text("source-description", state.dataset.source_label);
    text("source-mode", synthetic ? "合成展示" : "本機研究資料"); $("source-mode").className = "pill " + (synthetic ? "amber" : "teal");
    text("score-kind", scoreKind()); text("score-label", scoreKind());
    $("mode-notice").className = "mode-notice" + (synthetic ? "" : " local-mode");
    text("mode-explanation", synthetic ? "所有數值為人工合成情境，不是患者資料或模型預測成績。" : "分數由本機檔案提供，頁面不獨立驗證來源；尚未校準為個人風險或完成臨床驗證。");
    text("risk-value", value === null ? "—" : format(value * 100, 2)); $("risk-unit").hidden = value === null;
    var over = value !== null && value >= item.thresholds[state.model];
    text("risk-state", terminal ? "已退出預警風險集合" : value === null ? "資料不足 · 不出分" : over ? (synthetic ? "達示意通知門檻" : "達研究通知門檻") : (synthetic ? "未達示意通知門檻" : "未達研究通知門檻"));
    $("risk-state").className = "pill " + (terminal || value === null ? "neutral" : over ? "amber" : "teal");
    text("risk-context", terminal ? "追蹤已結束，不沿用最後一筆分數。" : state.model === "vitals_all" ? "常規基準可獨立出分；尚未驗證為臨床替代系統。" : value === null ? "缺少可評分片段，不能據此排除風險。" : "分數顯示模型輸出；不可直接解讀為已校準風險。");
    text("quality-title", point._terminal ? "追蹤已結束" : point.rr.accepted ? "工程篩選通過" : "片段未通過");
    text("quality-symbol", point.rr.accepted ? "✓" : "—"); $("quality-symbol").className = "quality-symbol" + (point.rr.accepted ? "" : " bad");
    text("quality-detail", describeReasons(point));
    text("coverage-count", format(coverage.count, 0));
    $("coverage-blocks").replaceChildren();
    coverage.slots.forEach(function (slot) {
      var block = node("span", slot.status === "accepted" ? "✓" : slot.status === "rejected" ? "×" : "?", "coverage-block " + slot.status);
      var description = hour(slot.hour) + "：" + (slot.status === "accepted" ? "工程通過" : slot.status === "rejected" ? "品質拒絕" : "未匯出此片段，品質未知");
      block.title = description; block.setAttribute("aria-label", description); $("coverage-blocks").append(block);
    });
    text("coverage-note", point._terminal ? "追蹤終點沒有新的 ECG 片段。" : coverage.fromFeature ? "總數來自原模型歷史特徵；? 表示該片段未匯出。" : "? 表示未提供；六個片段不等於連續六小時 ECG。");
    text("selected-model-legend", LABELS[state.model]);
    text("threshold-label", synthetic ? "示意通知門檻" : "匯入研究門檻");
    text("notification-label", synthetic ? "到目前為止的示意通知" : "到目前為止的研究通知");
    text("threshold-note", (synthetic ? "示意門檻 " : "匯入研究門檻 ") + format(item.thresholds[state.model] * 100, 2) + (synthetic ? "% · 非臨床界值" : "% · 正式 M2 匯出採訓練陰性第 99 百分位；非臨床界值"));
    text("current-time", hour(point.hour)); text("replay-position", (state.index + 1) + " / " + replayPoints.length + (point._terminal ? " · 追蹤終點" : ""));
    $("timeline").max = String(replayPoints.length - 1); $("timeline").value = String(state.index); $("timeline").setAttribute("aria-valuetext", hour(point.hour));
    $("step").disabled = state.index === replayPoints.length - 1; $("rewind").disabled = state.index === 0;
    METRICS.forEach(function (m) { vitalElements[m.key].value.textContent = format(point.vitals[m.key]); });
    text("mean-rr", format(point.rr.mean_rr_ms)); text("sdrr", format(point.rr.sdrr_ms)); text("rmssd", format(point.rr.rmssd_rr_ms));
    var event = arrivedEvent(item, point.hour, $("show-event").checked);
    $("outcome-note").hidden = event === null;
    text("outcome-note", event !== null ? "回溯事件：首次持續升壓劑輸注於 " + hour(event) + " 開始。事件資訊只用於回顧，不作為模型輸入。" : "");
    var observations = [terminal ? "此時點已退出未治療風險集合，所有模型停止出分。" : describeReasons(point), coverage.count === null ? "沒有新的歷史覆蓋評估。" : "最近六個取樣片段中有 " + coverage.count + " 個工程通過；覆蓋不足可能限制 RR 特徵解讀。", state.model === "vitals_all" ? "全風險集合基準使用的評估族群不同，不能直接與配對成績相減。" : "圖中的常規基準與 RR 模型使用相同配對時點；缺值保持空白。", synthetic ? "本病例與所有分數為合成示意，請到研究證據頁核對真實開發結果。" : "本頁僅重播既有分數，不重算 ECG、不訓練模型，也不提供用藥建議。"];
    $("observation-list").replaceChildren(); observations.forEach(function (line) { $("observation-list").append(node("li", line)); });
    text("notification-count", notices.length);
    renderMonitoring();
    renderCharts();
  }
  function next() {
    if (state.index < replayPoints.length - 1) state.index += 1;
    if (state.index >= replayPoints.length - 1) pause();
    render();
  }
  function play() {
    if (state.index >= replayPoints.length - 1) state.index = 0;
    state.timer = root.setInterval(next, Number($("playback-speed").value));
    text("play-pause", "暫停"); $("play-pause").setAttribute("aria-pressed", "true"); render();
  }
  function showView(view) {
    if (!["replay", "evidence", "guide"].includes(view)) view = "replay";
    if (view !== "replay") pause();
    state.view = view;
    ["replay", "evidence", "guide"].forEach(function (key) { $("view-" + key).hidden = key !== view; });
    document.querySelectorAll(".nav-button").forEach(function (button) {
      var active = button.dataset.view === view;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    });
    renderCharts();
  }
  function importFailure(error) {
    var message = "匯入失敗：" + error.message + "。仍顯示先前資料，尚未切換來源。";
    text("import-error", message); $("import-error").hidden = false; announce(message, true);
  }
  function importFile(file) {
    pause();
    var version = ++state.importVersion;
    if (!file) return;
    $("import-error").hidden = true;
    if (file.size > MAX_FILE_BYTES || file.size === 0) { importFailure(new Error("檔案必須介於 1 byte 與 8 MiB")); return; }
    var reader = new FileReader();
    reader.onerror = function () { if (version === state.importVersion) importFailure(new Error("無法讀取本機檔案")); };
    reader.onload = function () {
      if (version !== state.importVersion) return;
      try {
        var dataset = validateDataset(JSON.parse(reader.result));
        state.dataset = dataset;
        chooseCase(0); showView("replay");
        announce("已載入 " + dataset.cases.length + " 個" + (dataset.mode === "synthetic" ? "合成病例" : "本機研究病例") + "；內容僅留在目前頁面記憶體。", false);
      } catch (error) { importFailure(error); }
    };
    reader.readAsText(file, "UTF-8");
  }
  function exportSummary() {
    var item = currentCase(), point = replayPoints[state.index], coverage = historyCoverage(replayPoints, state.index);
    var summary = {schema_version: "hrv-observation-v1", mode: state.dataset.mode, source_label: state.dataset.source_label, case_label: item.label, relative_hour: point.hour, model: state.model, score_label: scoreKind(), score: point.scores[state.model], threshold: item.thresholds[state.model], vitals: point.vitals, rr: point.rr, history_valid_count: coverage.count, notification_hours_so_far: notificationEvents(visiblePoints(replayPoints, state.index), state.model, item.thresholds[state.model]), outcome_display_enabled: $("show-event").checked, arrived_event_hour: arrivedEvent(item, point.hour, $("show-event").checked), limitations: ["回溯研究原型，未完成保留測試與臨床驗證。", "RR 工程品質不等同正常 NN 或生理 HRV。", "分數尚未校準為個人風險；不能據此診斷或給藥。"]};
    if (state.dataset.mode === "synthetic") summary.limitations.unshift("本病例與分數全部為人工合成示意。");
    try {
      var url = URL.createObjectURL(new Blob([JSON.stringify(summary, null, 2)], {type: "application/json;charset=utf-8"}));
      var anchor = node("a"); anchor.href = url; anchor.download = "hrv-observation.json"; document.body.append(anchor); anchor.click(); anchor.remove();
      root.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      announce("目前時點摘要已交給瀏覽器下載；檔案名稱為 hrv-observation.json。", false);
    } catch (error) { announce("無法產生摘要下載：" + error.message, true); }
  }
  function exportMonitoring() {
    var trace = monitoringTrace(currentCase(), state.index, state.dataset.mode, state.simulateEcg);
    trace.source_label = state.dataset.source_label;
    trace.case_label = currentCase().label;
    trace.limitations = ["這是目前以前的來源重播／演練狀態，不是新模型預測。", "RR 增強固定未啟用；開發對照未支持增益，NN 與量測驗證未完成。", "可見數值不代表即時測量，資料新鮮度未提供；有模型分數不代表輸入充分。", "覆蓋只計已提供有效更新點，不計患者小時；時間缺口不補值。", "演練只影響本軌跡，未修改來源資料、圖表、分數或通知。"];
    try {
      var url = URL.createObjectURL(new Blob([JSON.stringify(trace, null, 2)], {type: "application/json;charset=utf-8"}));
      var anchor = node("a"); anchor.href = url; anchor.download = "hrv-monitoring-trace.json"; document.body.append(anchor); anchor.click(); anchor.remove();
      root.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      announce("已匯出目前以前的監測軌跡，包含來源與演練標記。真實研究病例的匯出請留在本機。", false);
    } catch (error) { announce("無法產生軌跡下載：" + error.message, true); }
  }
  $("play-pause").addEventListener("click", function () { if (state.timer === null) play(); else pause(); });
  $("step").addEventListener("click", function () { pause(); next(); });
  $("rewind").addEventListener("click", function () { pause(); state.index = 0; render(); });
  $("timeline").addEventListener("input", function () { pause(); state.index = Number(this.value); render(); });
  $("playback-speed").addEventListener("change", function () { if (state.timer !== null) { pause(); play(); } });
  $("model-select").addEventListener("change", function () { state.model = this.value; render(); });
  $("show-event").addEventListener("change", render);
  $("import-file").addEventListener("change", function () { importFile(this.files[0]); this.value = ""; });
  $("restore-demo").addEventListener("click", function () { state.importVersion += 1; state.dataset = validateDataset(root.HRV_DEMO); $("import-error").hidden = true; chooseCase(0); showView("replay"); announce("已回到三個合成情境。", false); });
  $("export-summary").addEventListener("click", exportSummary);
  $("simulate-ecg").addEventListener("change", function () { state.simulateEcg = state.dataset.mode === "synthetic" && this.checked; renderMonitoring(); });
  $("export-monitoring").addEventListener("click", exportMonitoring);
  document.querySelectorAll("[data-view]").forEach(function (button) { button.addEventListener("click", function () { showView(button.dataset.view); }); });
  document.querySelector(".brand").addEventListener("click", function () { showView("replay"); });
  root.addEventListener("hashchange", function () { showView(root.location.hash.slice(1)); });
  var resizeTimer;
  root.addEventListener("resize", function () { root.clearTimeout(resizeTimer); resizeTimer = root.setTimeout(renderCharts, 150); });
  document.addEventListener("visibilitychange", function () { if (document.hidden) pause(); });
  buildVitals(); chooseCase(0); showView(root.location.hash.slice(1) || "replay");
})(typeof window === "undefined" ? globalThis : window);
