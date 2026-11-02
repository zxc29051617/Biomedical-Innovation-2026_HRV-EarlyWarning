# 十二導程 ECG 波形稽核與盲性複核工具

2026-11-02。承接 [OMI 研究草案](omi_study_protocol.md)，先檢查來源波形是否能按正確導程與電壓尺度讀取，再準備同張 ECG 的盲性判讀。本文的工程檢查不等於臨床訊號品質、ST 幅度判定或 OMI 模型效能。

## 固定資料與稽核範圍

`scripts/audit_cath_waveforms.py` 固定使用 [Figshare v1](https://doi.org/10.6084/m9.figshare.29925314.v1) 三份檔案及已核對的大小／MD5，串流下載並另記 SHA256；不接受任意下載網址。原始與 median ZIP 合計約1.42 GB，另需解碼工作空間。原始資料與逐案內容只能留在忽略目錄。

只解碼明確的 `CSV/train.csv` 所指紀錄。整個來源 ZIP 雖也包含官方 test 的檔案，**不讀取 test 波形作品質或模型分析**。本輪保留全部訓練列，不按 OMI、來源 STEMI、時間或訊號看起來是否漂亮挑選；患者／事件選圖與建模切分尚未執行。

CSV 至 header 優先使用精確路徑；若路徑不同，只容許唯一 basename 配對，零個或多個匹配即記失敗。報告兩種配對方式數量。原始與 median 分開計數，不能將 median 的短長度當成缺少十秒原始 ECG。

## 解碼與校正

使用官方 [WFDB rdheader／rdrecord](https://wfdb.readthedocs.io/en/latest/wfdb.html) 讀取物理電壓；依 header `sig_name` 確認完整十二導程，重排為 `I, II, III, aVR, aVL, aVF, V1…V6`。不假設檔案第4欄一定是哪個導程。明確核對500 Hz、ADC gain／baseline 與單位；µV／uV 僅按明確比例轉 mV。

不補零、不補遺失訊號、不重採樣，也不任意平移 ST 基線。非有限值、尺度不明、缺導程、危險 header 引用或無法解碼均列失敗碼。raw 是否5000 samples另報；median依自身長度／500 Hz報時間。十秒波形只能當十二導程 ECG，不能稱五分鐘 HRV。

解壓縮限已驗證的 header 及其同資料夾 `.dat`，使用暫存目錄、大小上限及路徑檢查；header comments不傳到模型、報告或判讀包。[WFDB header格式](https://physionet.org/physiotools/wag/header-5.htm) 的可選日期／時間只計存在與否，不輸出值，也不由此推論到院或造影事件。

彙總包含實際解碼紀錄／患者數、來源 OMI 覆蓋、長度、單位／格式、精確恆定導程、全局振幅範圍及失敗碼。這些是結構與工程統計，不能直接作「可可靠判讀」的品質通過人數。

對標準導程順序、校正後mV波形、形狀與取樣率計算固定 SHA256；另數重複波形、跨 Patient_id 的相同波形，以及同一患者同波形的 OMI 0／1差異。指紋只辨認完全相同的解碼內容，不能辨認近似重複或證明同次事件。只有彙總數會公開，不公開個別波形指紋或病人對照。[標籤語義審查](cath_label_semantics.md)說明目前115位差異患者尚不能解釋的原因。

可在有網路及依賴的環境執行：

```sh
PYTHONPATH=src python scripts/audit_cath_waveforms.py \
  --output-dir artifacts/cath_feasibility/waveforms_new_run
```

已有官方檔案時可用 `--csv-archive`、`--raw-archive`、`--median-archive` 指定；仍須通過相同完整校驗。也可 `--representation raw` 或 `median` 單獨稽核。輸出拒絕覆寫，失敗不會被當成功結果。CI 只上傳彙總 JSON，不上傳來源 ZIP、逐案 ECG 或本機 MIMIC 資料。

## 離線複核工具

用瀏覽器開啟 [web/cath-review/index.html](../web/cath-review/index.html)，或執行 `python scripts/package_cath_review.py` 後解壓縮 `artifacts/releases/Cath-ECG-Review.zip`。先按「載入合成練習」即可操作；這是程式生成的練習波形，不是真實患者或已判定 OMI 病例。

介面顯示十二導程、時間／mV格線、校正標記、時間與振幅縮放、原始取樣點游標及手動參考點的電壓／時間差。**螢幕上的毫米不是實體 ECG 紙張毫米。** 不自動定位 J 點、不產生 ST 分類或疾病分數；手動游標功能也尚未經臨床量測驗證。

判讀者填寫代碼、四類一般 ST 幅度狀態、新舊變化與 BBB／pacing／LVH／後壁／其他型態旗標，匯出 `cath-ecg-annotation-v1`。預設未判，未知旗標不當陰性。匯出包含病例指紋以供對應，沒有波形；不同判讀者需獨立操作相同盲包，不能共享對方答案作獨立判讀。臨床判讀手冊及裁決人員仍待確定。

病例切換會清除判讀，離開前須匯出。資料只留頁面記憶體，不上傳、不用 localStorage／IndexedDB 自動保存。工具以欄位白名單拒絕額外診斷、模型分數、來源 ID 等欄位；這不等於能保證任何外部檔案已完成去識別化，資料管理者仍需按以下合約準備。

## 盲包資料合約

`cath-ecg-review-v1` 只接受 `schema_version`、隨機 `cr-<32 hex>` 的 `case_id`、`age_years`、`sex`、`sample_rate_hz`、`lead_names`、`signals_mv`、`representation`、`provenance`。其中 `signals_mv` 為 sample-major N×12，500 Hz、2–5000 samples；`provenance` 只有解碼後 `waveform_sha256`。年齡可為null，性別可unknown，不憑0／1自行猜來源編碼。

來源患者、ECG檔名、相對造影時間與所有結局不得進盲包。`build_blinded_review_case()` 重算波形指紋再建立白名單；來源對照必須留在資料管理者的忽略目錄。前端另以遞迴排序鍵、JS JSON.stringify 的序列化定義計算 `case_sha256`，不能直接假設 Python 不同數字序列化會得到相同雜湊。

## 驗證狀態

合成測試檢查官方 WFDB 往返、導程重排、單位、缺失值、ZIP控制及盲包白名單；瀏覽器測試另檢查匯入／拒絕額外欄位、游標、判讀匯出、手機布局與無網路請求。缺依賴而跳過的測試不得稱已通過。實際公開波形稽核結果與 CI 狀態於工作完成後另存彙總並更新本節；目前不以程式完成代替資料成功。
