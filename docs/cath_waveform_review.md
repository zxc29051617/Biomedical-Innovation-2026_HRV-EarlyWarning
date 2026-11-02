# 十二導程 ECG 波形稽核與盲性複核工具

2026-11-02。承接 [OMI 研究草案](omi_study_protocol.md)，先檢查來源波形是否能按正確導程與電壓尺度讀取，再準備同張 ECG 的盲性判讀。本文的工程檢查不等於臨床訊號品質、ST 幅度判定或 OMI 模型效能。

## 固定資料與稽核範圍

`scripts/audit_cath_waveforms.py` 固定使用 [Figshare v1](https://doi.org/10.6084/m9.figshare.29925314.v1) 三份檔案及已核對的大小／MD5，串流下載並另記 SHA256；不接受任意下載網址。原始與 median ZIP 合計約1.42 GB，另需解碼工作空間。原始資料與逐案內容只能留在忽略目錄。

只解碼明確的 `CSV/train.csv` 所指紀錄。整個來源 ZIP 雖也包含官方 test 的檔案，**不讀取 test 波形作品質或模型分析**。本輪保留全部訓練列，不按 OMI、來源 STEMI、時間或訊號看起來是否漂亮挑選；患者／事件選圖與建模切分尚未執行。

CSV 至 header 優先使用精確路徑；若路徑不同，只容許唯一 basename 配對，零個或多個匹配即記失敗。CSV實際指向`.dat`，僅移除明確的`.dat`／`.hea`後綴取得record stem，不任意刪其他副檔名。報告兩種配對方式數量。**實測 median ZIP只有`.med`，沒有WFDB header，目前尚未支援其解碼**；介面支援已校正的median JSON，不代表官方`.med`已轉換成功。

## 解碼與校正

使用官方 [WFDB rdheader／rdrecord](https://wfdb.readthedocs.io/en/latest/wfdb.html) 讀取物理電壓；依 header `sig_name` 確認完整十二導程，重排為 `I, II, III, aVR, aVL, aVF, V1…V6`。不假設檔案第4欄一定是哪個導程。明確核對500 Hz、ADC gain／baseline 與單位；µV／uV 僅按明確比例轉 mV。

不補零、不補遺失訊號、不重採樣，也不任意平移 ST 基線。非有限值、尺度不明、缺導程、危險 header 引用或無法解碼均列失敗碼。raw 是否5000 samples另報；median依自身長度／500 Hz報時間。十秒波形只能當十二導程 ECG，不能稱五分鐘 HRV。

解壓縮限已驗證的 header 及其同資料夾 `.dat`，使用暫存目錄、大小上限及路徑檢查；header comments不傳到模型、報告或判讀包。[WFDB header格式](https://physionet.org/physiotools/wag/header-5.htm) 的可選日期／時間只計存在與否，不輸出值，也不由此推論到院或造影事件。

彙總包含實際解碼紀錄／患者數、來源 OMI 覆蓋、長度、單位／格式、精確恆定導程、全局振幅範圍及失敗碼。這些是結構與工程統計，不能直接作「可可靠判讀」的品質通過人數。

對標準導程順序、校正後mV波形、形狀與取樣率計算固定 SHA256；另數重複波形、跨 Patient_id 的相同波形，以及同一患者同波形的 OMI 0／1差異。指紋只辨認完全相同的解碼內容，不能辨認近似重複或證明同次事件。只有彙總數會公開，不公開個別波形指紋或病人對照。[標籤語義審查](cath_label_semantics.md)說明目前115位差異患者尚不能解釋的原因。

可在有網路及依賴的環境執行：

```sh
PYTHONPATH=src python scripts/audit_cath_waveforms.py \
  --output-dir artifacts/cath_feasibility/waveforms_new_run --representation raw
```

已有官方檔案時可用 `--csv-archive`、`--raw-archive` 指定；仍須通過相同完整校驗。`--representation median`／`both`可重現格式盤點，但目前不會產生官方median解碼成功結果；尚缺`.med`的明確尺度與讀取規格。輸出拒絕覆寫，失敗不會被當成功結果。CI 只上傳彙總 JSON，不上傳來源 ZIP、逐案 ECG 或本機 MIMIC 資料。

## 離線複核工具

用瀏覽器開啟 [web/cath-review/index.html](../web/cath-review/index.html)，或執行 `python scripts/package_cath_review.py` 後解壓縮 `artifacts/releases/Cath-ECG-Review.zip`。先按「載入合成練習」即可操作；這是程式生成的練習波形，不是真實患者或已判定 OMI 病例。

介面顯示十二導程、時間／mV格線、校正標記、時間與振幅縮放、原始取樣點游標及手動參考點的電壓／時間差。**螢幕上的毫米不是實體 ECG 紙張毫米。** 不自動定位 J 點、不產生 ST 分類或疾病分數；手動游標功能也尚未經臨床量測驗證。

判讀者填寫代碼、四類一般 ST 幅度狀態、新舊變化與 BBB／pacing／LVH／後壁／其他型態旗標，匯出 `cath-ecg-annotation-v1`。預設未判，未知旗標不當陰性。匯出包含病例指紋以供對應，沒有波形；不同判讀者需獨立操作相同盲包，不能共享對方答案作獨立判讀。臨床判讀手冊及裁決人員仍待確定。

病例切換會清除判讀，離開前須匯出。資料只留頁面記憶體，不上傳、不用 localStorage／IndexedDB 自動保存。工具以欄位白名單拒絕額外診斷、模型分數、來源 ID 等欄位；這不等於能保證任何外部檔案已完成去識別化，資料管理者仍需按以下合約準備。

## 盲包資料合約

`cath-ecg-review-v1` 只接受 `schema_version`、隨機 `cr-<32 hex>` 的 `case_id`、`age_years`、`sex`、`sample_rate_hz`、`lead_names`、`signals_mv`、`representation`、`provenance`。其中 `signals_mv` 為 sample-major N×12，500 Hz、2–5000 samples；`provenance` 只有解碼後 `waveform_sha256`。年齡可為null，性別可unknown，不憑0／1自行猜來源編碼。

年齡若提供，接受0–130的有限數值；此為檔案輸入範圍，不代表正式收案資格。複核介面另限制單點絕對值不超過100mV，匯出器同步拒絕超出者；這是顯示工具的輸入界限，不是臨床品質門檻。完整波形稽核仍保留解碼後的實際振幅統計，不以此限值偷偷移除研究病例。

來源患者、ECG檔名、相對造影時間與所有結局不得進盲包。`build_blinded_review_case()` 重算波形指紋再建立白名單；來源對照必須留在資料管理者的忽略目錄。前端另以遞迴排序鍵、JS JSON.stringify 的序列化定義計算 `case_sha256`，不能直接假設 Python 不同數字序列化會得到相同雜湊。

單例匯出指令如下；`--record` 必須由資料管理者提供明確來源紀錄參照，此工具不自動選例，也不能把手選示例當隨機或主要研究樣本：

```sh
python scripts/export_cath_review_case.py \
  --archive artifacts/cath_feasibility/waveforms_new_run/ECG_row_data.zip \
  --record SOURCE_RECORD_REFERENCE --representation raw \
  --output-dir artifacts/cath_feasibility/review_case_new
```

可另外提供 `--age-years` 與 `--sex male|female|unknown`；未提供時保留未知。匯出器完整校驗官方 ZIP，產生 `case.json` 及 `source_mapping.local.json`，後者包含私密來源對照。**只交 `case.json` 給兩位獨立判讀者，來源對照留在資料管理端。** 目錄0700、檔案0600，禁止覆寫；輸出不讀取或推導疾病標籤。第一次到院、就醫事件及實際檢查時間維持未知。官方`.med`目前不能匯出，實際病例僅支援raw；合成median瀏覽器測試不能替代此解碼。

## 驗證狀態

程式 `c38b8cfa3371c1f15577c57e91a73bcfd155a11b` 在[完整 CI](https://github.com/zxc29051617/Biomedical-Innovation-2026_HRV-EarlyWarning/actions/runs/41432927580)實際通過497項Python測試，包括官方WFDB往返與盲包匯出；新版瀏覽器9組合成檢查與原HRV展示檢查通過。測試涵蓋導程重排、單位、缺失值、ZIP控制、盲包白名單、游標／匯出、手機布局及無網路請求。這些不代表已有醫師判讀或臨床量測驗證。

### 實際公開波形結果

[公開資料 CI](https://github.com/zxc29051617/Biomedical-Innovation-2026_HRV-EarlyWarning/actions/runs/41432927603)已完成校驗三份固定來源；[完整彙總](results/cath_waveform_summary.json)保留每種representation的分母、格式與失敗碼。

| 原始 raw 訓練 ECG | 結果 |
|---|---:|
| 嘗試讀取 | 17,960份 |
| 成功解碼 | 17,955份／17,013位患者 |
| 成功解碼的來源 OMI 陽性 | 1,151份／1,139位患者 |
| 成功解碼且來源 OMI=1、STEMI=0 | 424份；尚非ST幅度陰性子群 |
| 非有限／缺失取樣值 | 3份 |
| WFDB解碼錯誤 | 2份 |
| 成功解碼但至少一導程完全恆定 | 50份 |
| 成功解碼中的完全重複波形 | 0組 |

成功解碼者均為十二導程、500Hz、5000 samples（10秒），header明列mV、WFDB format16，沒有提供base date／time。全局電壓範圍約−32.766至+32.766mV；這些極值及平直導程仍須品質檢查，不能宣稱17,955份全可可靠判讀。缺少日期／時間也無法補出到院第一張。

原始archive有19,955對`.dat`／`.hea`；訓練CSV指向`.dat`，17,955個成功配對皆經唯一basename解析。首輪只處理stem／`.hea`而全部配對失敗；補上已知`.dat`後綴後取得上述結果，沒有放寬臨床或訊號品質門檻。成功解碼的17,955個波形指紋各不相同，但不能排除近似重複，也不能解釋115位患者的標籤時序差異。

Median archive實際為19,955個`.med`檔、0個`.hea`，訓練CSV也指向`.med`。報告中的median `record_reference_missing`表示目前WFDB reader不支援此格式，**不是已證明全部median波形壞掉**。[原文](https://pmc.ncbi.nlm.nih.gov/articles/PMC13347058/)只說由商用MedEx擷取median，[作者程式](https://github.com/catcatgirl/validation-code/tree/tag)未提供`.med` parser；目前未找到dtype、byte order、reshape及電壓尺度的完整規格，不猜二進位格式。匯出器對此回報明確的unsupported錯誤。主要候選輸入仍為原始十二導程；與採median的文獻模型比較時須揭露表示方式不同，不能稱已重現原模型。

本機網路無法連上來源站，因此上述真實解碼在CI完成，來源ZIP與患者盲包沒有傳回本機或對外發布。本機已產生僅含工具與合成練習的`Cath-ECG-Review.zip`，SHA256為`f77f2809a1e7578459cc94c545755e54c670307b083297f48542adaa329de7f6`。正式病例盲包及臨床判讀尚未產生；具資料連線的環境可依上方CLI準備。
