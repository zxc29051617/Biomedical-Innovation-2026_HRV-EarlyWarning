# HRV Early Warning

**重症患者循環風險預警：結合 HRV 趨勢與訊號品質評估**

這是由王獻霆與朋友自主組隊開發的專案。GitHub repo：[Biomedical-Innovation-2026_HRV-EarlyWarning](https://github.com/zxc29051617/Biomedical-Innovation-2026_HRV-EarlyWarning)。

本專案以既有 `fish_yang_MI` 的 ECG 與 HRV 研究經驗為基礎，建立獨立、可重跑的重症預警研究管線。第一版使用 MIMIC-III 成人 MetaVision ICU 資料，研究「未來 6 小時內首次開始持續使用升壓劑」的風險；敗血症是預先指定的重點分析子群。

核心研究問題：**在相同誤報負擔下，HRV 與個人過去的變化，能否比心率、血壓等常規資訊更早辨識事件？** 模型預測的是給藥紀錄中的事件，不直接判定敗血性休克或治療必要性。

目前完成資料盤點、成人 ICU 資格、首次持續升壓劑事件、同次 ICU 波形配對，以及全量 ECG 解碼、工程品質篩選與五組開發模型比較。醫學與資料定義見[臨床證據審查](docs/clinical_evidence.md)，訊號規則見[訊號品質依據](docs/signal_quality_evidence.md)。工程品質通過不代表已確認正常心律或臨床 HRV；保留測試尚未用於模型評估。

2026-11-01 的 protocol 0.2 稽核：23,327 次成人 ICU、4,862 個首次事件；426 個事件有合法預測前 II 通道 metadata，其中 372 個至少有 3 小時、220 個有完整 6 小時覆蓋。這些是檔案存在性計數，不能當作品質合格的 HRV 樣本。完整排除原因與早期事件限制見[資料盤點](docs/data_inventory.md)。

Protocol 0.3 已建立 1,014,603 個完整逐小時風險窗，完成 340,354 個五分鐘 ECG 片段；142,489 個通過工程篩選，1,529 個有預警機會的事件中有 244 個至少具一個可評分時點。配對開發驗證包含 630 位患者、41 個事件：常規模型 AUPRC 0.02419，加入歷史 RR 為 0.02698，但配對差值區間跨零，**目前沒有清楚的 RR 增益證據**；加入當下 RR 的 AUPRC 反而降低。完整數值、拒絕原因與誤報分母見[全量訊號及配對結果](docs/m2_signal_model_results.md)，隊列與生命徵象基準見 [M2 紀錄](docs/m2_results.md)。

真實訓練片段抽查已發現：通過目前工程篩選的 ECG，仍可能因搏動定位點變動而影響 RR 變異量。六例皆完整重現快取，但不構成準確率驗證；本輪只作固定工程基準，詳見[抽查證據及下一版驗證要求](docs/signal_quality_evidence.md)。

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

## ECG 與開發模型

訊號與模型需額外依賴，可在獨立環境執行 `python -m pip install -e '.[signal,model]'`。本次驗證環境為 Python 3.13.17、numpy 2.2.6、scipy 1.15.3、pandas 2.2.3、wfdb 4.3.0、scikit-learn 1.6.1。全量訊號處理可能需要數小時，逐批結果寫入 SQLite；同一 work directory 請只啟動一個訊號工作。

繪製彙總結果另需 `report` 依賴，可安裝 `python -m pip install -e '.[signal,model,report]'`；本次使用 matplotlib 3.10.3。

```bash
PYTHONPATH=src python -m hrv_early_warning vitals \
  --paths configs/paths.local.toml --output reports/local/vitals.json
PYTHONPATH=src python scripts/run_signals_managed.py \
  --paths configs/paths.local.toml --engine bounded-spawn --workers 4
PYTHONPATH=src python -m hrv_early_warning develop \
  --paths configs/paths.local.toml --output reports/local/development.json
PYTHONPATH=src python -m hrv_early_warning compare \
  --work-dir artifacts/m2 --replicates 200
```

也可指定已安裝依賴的 Python 後執行 `HRV_PYTHON=/path/to/python sh scripts/run_m2.sh`，依序完成資料可行性、生命徵象、訊號、開發模型與配對比較。

`compare` 需全量 ECG 與四組配對模型都已完成；固定已訓練模型，在相同開發驗證患者與時窗上重抽患者 200 次，計算加入 RR 特徵的指標差及 95% 區間，不重訓或讀取保留測試。彙總預設寫入 `artifacts/m2/paired_comparison.json`，可用 `--output` 改位置。它預設讀取 `configs/paths.local.toml` 以保護來源目錄，可用 `--paths` 指定其他設定。區間未包含訓練不確定性，也未校正多重比較。

比較完成後，可執行 `PYTHONPATH=src python scripts/render_m2_report.py --work-dir artifacts/m2`，產生彙總表與兩張 SVG 至 `reports/local/m2_publishable/`，供團隊審閱。工具核對完整片段數、模型來源、bootstrap 規格及各項分母；不完整或來源不一致會拒絕產生新報告，不讀患者特徵或反序列化模型，也不會自動發布。

使用 `PYTHONPATH=src python -m hrv_early_warning status` 查詢進度。若訊號工作已另行啟動，`PYTHONPATH=src python scripts/finalize_m2.py` 可等待完整快取後接續建模；它不啟動另一份 ECG 工作，預設最多等候 24 小時。工作失敗會留下明確狀態，不能將等待程式仍在執行當成訊號工作成功。

本機全量執行曾因記憶體不足而中斷，已提交片段均保留。`run_signals_managed.py` 預設使用兩個工作程序，持有同目錄鎖，將輸出直接追加至 `artifacts/m2/signal_job.log`，每 30 秒更新 `signal_job_status.json`。只有工作程序異常終止且同次執行出現新的 OOM 紀錄，才降為單程序續跑；最多三次嘗試，不改品質規則、特徵或快取指紋。其他錯誤會明確停止，完整快取通過核對後才標示完成。

上列指令選用 `bounded-spawn` 執行器：每批只傳一個 ICU 的波形中繼資料及最多 24 個片段，待處理工作上限為程序數的兩倍，避免每個程序持有整份隊列資料。它呼叫相同的凍結規劃、訊號 worker 與彙總函式；六個固定真實訓練片段的完整結果與原快取 6／6 精確相同。另保存執行器程式指紋及遷移紀錄，程式變更會拒絕直接沿用；相容性與限制見[訊號品質證據](docs/signal_quality_evidence.md)。此模式發生已確認的 OOM 工作程序失敗時，每次將程序數減半，四程序最多嘗試 `4 → 2 → 1`。管理器未指定 `--engine` 時仍保留舊執行器的兩程序預設。

若需停止，可將 `{"action":"stop"}` 存為 `artifacts/m2/signal_stop_request.json`；管理程序會清理子程序群組並保留請求檔。恢復時移除此請求檔，再執行相同命令即可沿用快取。意外終止可能留下最後一次狀態，因此應搭配 `updated_utc` 心跳及完整片段數判讀，不把過期的 `running` 當成仍在執行。

`signals` 使用官方 WFDB 解碼，不插補缺失 ECG。每小時採用更新前最後五分鐘，過去六小時以六個這樣的片段描述；事件前另做密集五分鐘覆蓋盤點，但模型不會混入這些依事件選取的額外片段。候選指標為 mean RR、SDRR 與 RMSSD of RR，尚未確認正常 NN 心搏、AF 或 pacing。

`artifacts/m2/patient_split.json` 固定全體患者的訓練／驗證／保留測試歸屬，重跑不得因結果或 ECG 可用性換人。`develop` 只使用訓練與開發驗證；品質全量尚未完成時僅可建立生命徵象基準，HRV 比較維持待完成。患者時間、特徵、模型與快取留在忽略目錄；檢視報告中的完成狀態後再引用結果。

## 文件與實作位置

- [研究計畫與驗收標準](docs/project_brief.md)
- [團隊成員與起始分工](docs/team.md)
- [指導教授背景](docs/advisor_background.md)
- [時間、終點與驗證規格](docs/protocol.md)
- [臨床定義與官方資料來源審查](docs/clinical_evidence.md)
- [近期里程碑](docs/roadmap.md)
- [資料盤點證據](docs/data_inventory.md)
- [M2 風險集合、訊號工作與開發基準](docs/m2_results.md)
- [M2 全量訊號、配對模型與實際誤報結果](docs/m2_signal_model_results.md)
- [v2 搏動定位與 RR 量測驗證準備](docs/rr_validation_v2.md)
- [前作與來源](docs/references.md)
- [機器可讀規格](configs/protocol.toml)
- `src/hrv_early_warning/inventory.py`：可重跑的資料盤點。
- `src/hrv_early_warning/timing.py`：事件時間界線與追蹤不足處理；不是臨床事件萃取器。
- `src/hrv_early_warning/cohort.py`、`vasopressors.py`：成人資格、輸注對齊、有效區間與首次事件。
- `src/hrv_early_warning/waveforms.py`、`waveform_index.py`：master 時間、gap、II 通道與檔案存在性。
- `src/hrv_early_warning/feasibility.py`：整合掃描、事件與 metadata 覆蓋報告。
- `src/hrv_early_warning/risksets.py`：完整逐小時正／負／截尾風險集合及患者切分。
- `src/hrv_early_warning/ecg.py`、`signal_quality.py`、`signal_pipeline.py`：解碼、工程篩選與可續跑結果。
- `src/hrv_early_warning/vitals.py`：完整掃描生命徵象，保留測量與登錄時間。
- `src/hrv_early_warning/modeling.py`：訓練患者擬合、驗證患者比較；保留測試鎖定。

原始資料、患者層級產物、本機路徑設定及模型產物由 `.gitignore` 排除。此 repo 的初始實作為新寫程式，既有專案與論文的研究來源列於引用文件。
