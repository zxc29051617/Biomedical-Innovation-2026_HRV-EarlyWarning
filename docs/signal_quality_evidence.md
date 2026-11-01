# 五分鐘 ECG 品質與候選 RR 特徵：證據及工程規則

核對日期：2026-11-01；程式版本 `rr_quality_v1`。實作見 [signal_quality.py](../src/hrv_early_warning/signal_quality.py)，測試見 [test_signal_quality.py](../tests/test_signal_quality.py)。本文件包含真實資料的一致性抽查，但不代表已完成心律標註或偵測準確率驗證，也不是臨床可用性認證。

這一版輸出「通過工程篩選的 RR 間期特徵」。**通過不代表竇性心律、排除 AF／pacing、確認所有心搏正常，亦不等於可解釋為自律神經 HRV。** 所有結果均包含 `rhythm_status=not_assessed`、`nn_confirmed=false` 及未判定 AF／pacing 的欄位。因此採 `mean_rr_ms`、`sdrr_ms`、`rmssd_rr_ms` 命名，不將 `sdrr_ms` 改稱已確認的 SDNN。

## 原始來源與支持範圍

| 規則或判斷 | 分類 | 來源與適用界線 |
| --- | --- | --- |
| 短時 HRV 常以五分鐘標準化；SDNN／RMSSD 的定義涉及 NN 間期 | `source-supported` | [1996 ESC/NASPE 原始標準，ESC 官方 PDF](https://www.escardio.org/static-file/Escardio/Guidelines/Scientific-Statements/guidelines-Heart-Rate-Variability-FT-1996.pdf)，[原始書目／DOI](https://pubmed.ncbi.nlm.nih.gov/8598068/)。五分鐘長度本身不能證明心律、平穩性或訊號適用性。原始 PDF／AHA 全文直接存取此次受 403 限制，已核對官方文件的可檢索文字及指標表，未聲稱完整通讀全文。 |
| XQRS／GQRS 可接收 ECG 及取樣頻率，回傳 QRS sample index；GQRS 可接收 physical signal | `source-supported` | 已讀 [WFDB 官方 processing 文件](https://wfdb.readthedocs.io/en/latest/processing.html)並核對本機 4.3.0 API。官方頁當時顯示 4.3.1。GQRS 官方明載未持續開發／支援且存在沿襲實作問題；只用作交叉檢查，不作標註真值。 |
| 多偵測器一致性可作訊號品質線索 | `source-supported`：方法概念；`project-defined`：本實作 | [Li、Mark、Clifford 2008 原始研究](https://pmc.ncbi.nlm.nih.gov/articles/PMC2259026/)，DOI `10.1088/0967-3334/29/1/002`。本程式的 XQRS＋GQRS 組合、匹配式及門檻不是該論文已驗證的整套方法；兩個偵測器可能同時犯錯。 |
| 品質篩選、心律排除與 RR 清理應分開；剔除資料後保留原始鄰接關係 | `source-supported` | 已讀 [Vest 等 2018 原始全文](https://pmc.ncbi.nlm.nih.gov/articles/PMC6442742/)，DOI `10.1088/1361-6579/aae021`。該研究包含獨立心律分析及可設定的 SQI／RR 規則；本專案沒有重現其 AF、PVC 或 VF 分類器。 |
| 用固定 RR 範圍、局部中位數與剔除比例控制品質 | `project-defined`，有方法先例 | 同一 [Vest 原始研究](https://pmc.ncbi.nlm.nih.gov/articles/PMC6442742/)提供相關方法；本版門檻、邊緣處理及鄰接規則以以下程式規格為準，不借用原論文效能數字。 |
| 真正 NN、異位搏動、AF、pacing、臨床生理原因 | `unobservable`（此模組） | 單靠有限振幅、RR 規則和雙偵測一致性不能確認。需要獨立標註／心律方法；缺少判讀不能編成正常。 |

## 凍結的候選工程配置

以下均為 **project-defined**，不是醫療標準或已在本地 ICU ECG 驗證過的門檻。選定時只看文件與合成訊號，沒有使用患者結局或真實 ECG 調參。根研究規格的版本與凍結時間由整合流程記錄；若之後修改，須保存原版本、理由及只用開發資料的核對結果。

| 階段 | `QualityConfig` 預設與處理 |
| --- | --- |
| 輸入 | 單一連續 ECG 通道、physical mV；`epoch_seconds=300`，必須剛好 `round(300*fs)` 點。僅容許目標點數取整帶來的半點時間誤差，不補短窗、不截長窗。`minimum_fs_hz=100`；這是納入門檻，不是宣稱 100／125 Hz 達到所有 HRV 精度要求。 |
| 缺值 | 所有點皆須 finite；任一 NaN／inf 即拒絕。不上下插值。呼叫端另外負責檔案 gap、通道與日期連續性。 |
| 平坦 | 相鄰差 ≤`1e-7 mV` 的連續樣本達 `1 s` 即拒絕。最長 run 包含首點，輸出實際秒數。 |
| 振幅 | 每個不重疊 `10 s` block 的 peak-to-peak 須 ≥`0.05 mV`；全窗範圍須 ≤`10 mV`。過低／過高皆只作工程拒答原因，可能排除真實低振幅或生理狀態。 |
| 疑似截頂 | 在全窗最大／最小值附近 `1e-8 mV` 內連續停留 ≥`0.04 s` 即拒絕，原因為 `suspected_clipping`。沒有 ADC rails 資訊，故不叫「確認 clipping」；也可能漏掉其他局部飽和。 |
| QRS | XQRS 作主偵測，GQRS 作交叉檢查；以 WFDB 4.3.0 預設參數、無另加跨窗濾波或手動 peak 修正。每個偵測器至少 `100` beats。 |
| 一致性 | 按時間排序作一對一匹配，差距 ≤`0.10 s`；每個偵測最多配一次。全窗 `2*配對數/(XQRS數+GQRS數)` ≥`0.90`。每個不重疊 `10 s` block 的已配對偵測數／所有偵測數 ≥`0.80`；空 block 為 0。跨 block 邊界配對按各自偵測位置計數。不是相對人工標註的 F1 accuracy。 |
| 首尾覆蓋 | XQRS 第一點距窗開始、最後一點距窗結束均 ≤`2.5 s`。不將只有中間有心搏的片段視作完整五分鐘。 |
| RR 篩選 | 由原始相鄰 XQRS 差值得 RR；其兩端均須有 GQRS 配對。範圍含端點 `0.30–2.0 s`，且與包含自身的 `11` 點局部中位數差距 ≤`20%`；邊界用 nearest extension。範圍相當於約 30–200 bpm，是工程納入限制，不能說之外必然是假訊號。 |
| RR 保留 | 有效 RR 個數占原始 RR ≥`95%`，有效 RR 時長總和占 300 s ≥`90%`；保留比例與各種剔除數全部輸出。RR 原因可能重疊，計數不可直接相加當唯一剔除數。 |

局部 RR 篩選會影響觀察到的變異度，可能剔除真實生理變化。因此不能因接受後的序列較規則，就聲稱心律正常。低品質、超界心率、心律未確認三件事在研究解釋上須分開。

## 特徵與時間界線

`assess_ecg_epoch(signal_mv, fs, config=None)` 回傳 `EpochAssessment(accepted, reasons, metrics, features)`。任何拒絕都令 `features={}`；拒絕原因及已算得品質指標仍保留。無原始波形、患者 ID 或逐搏時間輸出。

接受的窗只產出：

- `mean_rr_ms`：保留 RR 的平均值。
- `sdrr_ms`：保留 RR 的樣本標準差，`ddof=1`。
- `rmssd_rr_ms`：**原本相鄰且兩者都有效**的 RR 差值平方平均後開根號；剔除間期形成的缺口不橋接、不補值。
- `heart_rate_from_mean_rr_bpm`：`60000 / mean_rr_ms`；不是逐搏瞬時心率的算術平均。

本版不計算 LF/HF、DFA、MSE 或其他非線性指標，也不作病理機制解釋。程式內偵測／局部中位數可使用整個已結束的 epoch，因此研究呼叫端必須保證整段落在 `[t-6h,t)` 內；不得在 t 時計算跨到 t 之後的五分鐘窗。API 本身不接收日期，無法替呼叫端驗證這一點。

`dataclasses.asdict(QualityConfig())` 可完整記錄本層門檻。執行 fingerprint 還須納入 `ALGORITHM_VERSION`、程式版本與 wfdb／numpy／scipy 版本，因為偵測器預設值由依賴套件提供。缺少依賴會拋出例外，不得被算為患者 ECG 品質差。有限的偵測器執行錯誤只保存 exception class，仍應另報工程失敗率。

## 已完成驗證與限制

使用 `lab410` 的 Python 3.13.17、numpy 2.2.6、scipy 1.15.3、wfdb 4.3.0；26 項合成及邊界測試涵蓋 300 s 長度、NaN／inf、平坦、振幅、截頂、全窗／局部一致性、RR 限制、原始鄰接、缺少依賴、偵測錯誤，以及真正 WFDB 對合成正／反極性 ECG 與白雜訊的行為。合成固定 75 bpm、125 Hz ECG 得到平均 RR 800 ms；這只驗證程式基本行為，不能外推真實 ICU 偵測準確度。

相同合成 300 s／125 Hz 資料的單次本機計時：XQRS 約 `0.090 s`、GQRS 約 `0.176 s`；完整模組 warm call 約 `0.265 s`，首次含 lazy import 約 `0.74 s`。簡單 bandpass＋`scipy.signal.find_peaks` 約 `0.005 s`，但其資料適用性未驗證，故未採為替代。這些為單例速度量測，未包含來源檔讀取、平行排程及真實雜訊變異，不是全資料執行時間保證。

## 真實訓練片段抽查：重現成功但定位精度仍有缺口

2026-11-01，使用 [spotcheck_signals.py](../scripts/spotcheck_signals.py)，只從既有快取的訓練患者依工程決策分層，固定 seed=20261101，以雜湊排序選取三個 accepted 及三個已解碼的 rejected 片段，每個片段來自不同次 ICU。選取不依臨床終點，首次選取清單保存後不隨新增快取改變。這是小型、非代表性的一致性抽查，三個 accepted 是抽樣設計，不是估計的接受率；也不能用六例外推問題盛行率或偵測敏感度／特異度。

六例全部為 125 Hz、300 秒、37,500 點，輸出單位 mV；重新讀取來源解碼後，**6/6 的決策、拒絕理由、特徵及 metrics 與快取完全相同**。三個拒絕片段分別包含缺值、疑似截頂，以及有效 RR 個數／時間不足。原始波形檢視圖、患者對照清單及詳細報告只存於忽略的 `reports/local/spotcheck/`，不隨原始碼發布。

接受片段 01 與 03 的檢視圖顯示，XQRS 的搏動定位點（fiducial）在不同搏動間可落於 QRS 前方低點或較高峰，仍符合與 GQRS 的 100 ms 配對容許值。以下對同一組一對一配對搏動計算偏移；RMSSD 診斷僅使用兩個偵測器中皆維持原始相鄰的三搏組，不跨越缺失配對，也沒有套用模型的 RR 篩選或產生替代特徵。

| 私有抽查序號（均 accepted） | 配對搏動數 | XQRS−GQRS 時點偏移第 5／50／95 百分位（ms） | XQRS 診斷 RMSSD（ms） | GQRS 診斷 RMSSD（ms） |
|---|---:|---|---:|---:|
| 01 | 550 | −16／32／40 | 62.22 | 5.75 |
| 02 | 516 | 32／40／40 | 7.98 | 7.54 |
| 03 | 439 | −16／32／40 | 49.61 | 18.19 |

這三例的 XQRS 診斷 RMSSD 與快取 `rmssd_rr_ms` 一致。兩個偵測器的逐次 RR 差值之差，可由配對時點偏移的二階差分精確重建；固定偏移會抵消，逐搏變動的偏移則會進入變異度。**GQRS 並非標註真值，表中較低數值不能當作正確生理 HRV。** 此觀察顯示高配對率不保證足以計算 RR 變異的定位一致性，不能把 accepted 解讀為已證實可靠的生理 HRV。

此二階差分恆等式只是代數一致性核對，不能將兩個 RMSSD 數值直接相減，解釋成已分離的生理量與誤差量。[WFDB 4.3.0 XQRS 原始碼](https://github.com/MIT-LCP/wfdb-python/blob/v4.3.0/wfdb/processing/qrs.py)在處理後訊號的局部峰值判定 QRS，沒有再自動對齊原始波形的 R 波頂點。125 Hz 每格為 8 ms，驗證時須特別區分找對搏動與定位足夠精確。

本輪 `rr_quality_v1` 保留為凍結的工程基準，不依這六例改門檻或覆寫快取；即使模型後續有分數增益，也仍須排除偵測時點與品質差異的影響。後續 v2 須在訓練資料與有獨立搏動標註的資料上驗證一致定位點、相對標註的時間誤差及 RR／RMSSD 誤差，並另外處理心律與正常搏動適用性。[WFDB 官方 `correct_peaks` 文件](https://wfdb.readthedocs.io/en/latest/processing.html#wfdb.processing.correct_peaks)提供依搜尋範圍及方向調整到局部峰值的方法，可列為候選；它不是本專案已驗證的解法，亦不能取代心律標註。

v2 若變更偵測位置、規則或依賴套件，須保存 v1、記錄新版本與驗證依據，使用新的快取目錄，並在相同患者切分下重新計算。全量覆蓋、拒絕原因與有效 RR 時間仍需完整報告；保留測試維持封存，不用於選定位方法或門檻。

候選定位方法另須檢查正／反向及雙相波形、寬 QRS、雜訊、重複峰值、索引順序、位移上限和片段邊界。本地 WFDB 4.3.0 的 `correct_peaks` 邊界探測中，30 點陣列在 index 2 放單一脈衝，`search_radius=5`、`smooth_window_size=3`、`peak_dir='up'` 會回傳 −4；脈衝放在 index 29 則回傳 24。[相同版本的 peaks 原始碼](https://github.com/MIT-LCP/wfdb-python/blob/v4.3.0/wfdb/processing/peaks.py)亦須配合檢視。這是候選方法的合成邊界問題，本輪 v1 未使用該函式。後續不得以預測時刻之後的訊號補邊界，亦不得把通過這些工程測試當成正常心律標註。
