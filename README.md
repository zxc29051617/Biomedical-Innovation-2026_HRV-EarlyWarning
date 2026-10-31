# HRV Early Warning

**重症患者循環風險預警：結合 HRV 趨勢與訊號品質評估**

這是由王獻霆與朋友自主組隊開發的專案。GitHub repo：[Biomedical-Innovation-2026_HRV-EarlyWarning](https://github.com/zxc29051617/Biomedical-Innovation-2026_HRV-EarlyWarning)。

本專案以既有 `fish_yang_MI` 的 ECG 與 HRV 研究經驗為基礎，建立獨立、可重跑的重症預警研究管線。第一版使用 MIMIC-III 成人 MetaVision ICU 資料，研究「未來 6 小時內首次開始持續使用升壓劑」的風險；敗血症是預先指定的重點分析子群。

核心研究問題：**在相同誤報負擔下，HRV 與個人過去的變化，能否比心率、血壓等常規資訊更早辨識事件？** 模型預測的是給藥紀錄中的事件，不直接判定敗血性休克或治療必要性。

目前完成資料盤點、成人 ICU 資格、首次持續升壓劑事件萃取、同次 ICU 波形配對，以及事件前 II 通道與資料檔存在性稽核。醫學與資料定義已整理成[附來源的證據審查](docs/clinical_evidence.md)。ECG 品質檢查、HRV 特徵、模型與展示介面仍待實作；目前沒有可宣稱的模型效能。

2026-11-01 的 protocol 0.2 稽核：23,327 次成人 ICU、4,862 個首次事件；426 個事件有合法預測前 II 通道 metadata，其中 372 個至少有 3 小時、220 個有完整 6 小時覆蓋。這些是檔案存在性，尚不是品質合格的 HRV 樣本。完整排除原因與早期事件限制見[資料盤點](docs/data_inventory.md)。

## 團隊

| 成員 | 就讀單位 | 年級 |
|---|---|---|
| 王獻霆 | 國立臺灣大學生醫電子與資訊學研究所 | 碩士班三年級 |
| 姚亦俠 | 國立臺灣大學醫學系 | 三年級 |
| 黃榆崴 | 國立臺灣大學醫學系 | 三年級 |
| 李奕辰 | 國立臺灣大學電機工程學系 | 四年級 |

指導教授：**歐陽彥正教授（Yen-Jen Oyang）**。公開研究背景為機器學習、生醫資訊與知識工程；詳見[團隊資料](docs/team.md)及[教授背景與資料來源](docs/advisor_background.md)。學校資訊列為個人成員背景，專案由團隊自主發起。

## 第一版目標

| 項目 | 決定 |
|---|---|
| 主要族群 | 成人 MetaVision ICU；同一次 ICU 住院可對齊 ECG |
| 觀察與更新 | 使用過去 6 小時資料，每小時更新 |
| 主要終點 | 接下來 6 小時首次開始持續至少 60 分鐘的指定升壓劑輸注 |
| 次要分析 | 2 小時預測窗；敗血症等事後描述子群；輸注持續時間敏感度分析 |
| 第一個模型 | 常規生命徵象基準與正則化簡單模型，先驗證 HRV 額外價值 |
| 展示 | 風險時間軸、觀察到的生理趨勢、資料品質、資料不足狀態 |
| 成功標準 | 可重跑、時間與患者切分正確、完整呈現效果與誤報；提升必須由保留測試支持 |

第一版先完成成人 ICU 管線，避免將出院診斷或事後才完成的感染證據當作當下已知資訊。後續若轉為「疑似感染患者專用」模型，必須重新定義當下可得的納入條件與驗證方案。不同疾病共用訊號處理框架，重新建立隊列、標籤與模型。

## 開始使用

資料盤點與測試只需 Python 3.11 以上的標準函式庫。從 repo 根目錄執行：

```bash
cp configs/paths.example.toml configs/paths.local.toml
```

將 `configs/paths.local.toml` 改成已有的臨床與波形掛載路徑。資料仍保留在外部掛載。

```bash
PYTHONPATH=src python -m hrv_early_warning protocol
PYTHONPATH=src python -m hrv_early_warning audit \
  --paths configs/paths.local.toml \
  --headers 10 \
  --output reports/local/inventory.json
PYTHONPATH=src python -m hrv_early_warning feasibility \
  --paths configs/paths.local.toml \
  --work-dir artifacts/feasibility \
  --workers 8 \
  --output reports/local/feasibility.json
PYTHONPATH=src python -m unittest discover -s tests -v
```

`audit` 完整掃描四張較小臨床表，其他大型事件表只讀欄位；可選擇抽查少量波形標頭。報告只有彙總資訊。患者 ID 出現在波形索引中，尚不代表同次住院、預測前時間窗或訊號品質合格。

`feasibility` 完整掃描一次 `INPUTEVENTS_MV`，只快取五種指定藥物；讀取成人 MetaVision 患者的 waveform master headers，再對合格事件的預測前視窗檢查 II 通道與非空資料檔。使用 `[t-6h,t)` 的實際時間交集，合併重疊覆蓋並排除 gap。它不解碼 ECG、不計算 HRV，也不代表完成模型訓練隊列。

患者層級快取與交接產物只放在忽略的 `artifacts/feasibility/`；`reports/local/feasibility.json` 為彙總。快取使用來源路徑、索引大小／修改時間及版本辨識，並非逐檔內容校驗；若來源 header 更新但索引不變，請指定新的 `--work-dir` 重新稽核。不要將這些患者產物加入 Git。

需要安裝命令列入口時，可在自己的虛擬環境執行 `python -m pip install -e .`；初始工具不需要額外的執行期套件。

## 文件與實作位置

- [研究計畫與驗收標準](docs/project_brief.md)
- [團隊成員與起始分工](docs/team.md)
- [指導教授背景](docs/advisor_background.md)
- [時間、終點與驗證規格](docs/protocol.md)
- [臨床定義與官方資料來源審查](docs/clinical_evidence.md)
- [近期里程碑](docs/roadmap.md)
- [資料盤點證據](docs/data_inventory.md)
- [前作與來源](docs/references.md)
- [機器可讀規格](configs/protocol.toml)
- `src/hrv_early_warning/inventory.py`：可重跑的資料盤點。
- `src/hrv_early_warning/timing.py`：事件時間界線與追蹤不足處理；不是臨床事件萃取器。
- `src/hrv_early_warning/cohort.py`、`vasopressors.py`：成人資格、輸注對齊、有效區間與首次事件。
- `src/hrv_early_warning/waveforms.py`、`waveform_index.py`：master 時間、gap、II 通道與檔案存在性。
- `src/hrv_early_warning/feasibility.py`：整合掃描、事件與 metadata 覆蓋報告。

原始資料、患者層級產物、本機路徑設定及模型產物由 `.gitignore` 排除。此 repo 的初始實作為新寫程式，既有專案與論文的研究來源列於引用文件。
