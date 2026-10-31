# 前作與來源

此專案沿用 `fish_yang_MI` 的研究經驗：ECG 讀取、R 波、HRV、非線性特徵及模型比較。初始 repo 的程式重新撰寫，未複製原專案的程式、論文 PDF、掛載腳本、憑證或患者資料。

## 原有研究基礎

- Wei-Yu Yang，2025，*Prediction of Mortality for ICU Patients with Myocardial Infarction Based on Heart Rate Variability: A Retrospective Cohort Study with the MIMIC-III Database*。使用者提供的碩士論文與 [fish_yang_MI 原始專案](https://github.com/golioss1886/fish_yang_MI)。目前本地副本未見授權檔；後續若直接移植程式，需確認授權與保留來源。
- [Huang 等，2025](https://doi.org/10.1016/j.compbiomed.2024.109635)：MIMIC-III HRV 與 ICU 死亡研究，適合當原專案的直接前作；事件對齊取樣的結果不能直接當固定時間預警效能。

## 本題直接前作

- [Soliman 等，2024](https://pmc.ncbi.nlm.nih.gov/articles/PMC10977921/)：HRV、脈搏到達時間及血壓預測休克，提供多模態生理資訊的對照方向。
- [Duval 等，2025](https://doi.org/10.1186/s12911-025-03274-3)：MIMIC-IV 電子病歷預測升壓劑開始使用；本專案需量化 HRV 超過常規資料的增益。
- [Keng 等，2025](https://doi.org/10.1016/j.bspc.2024.106854)：HRV 預測疑似敗血症患者惡化；跨資料庫結果包含轉移學習，不能當未調整模型的直接外驗。
- [van Wijk 等，2023](https://doi.org/10.1186/s13049-023-01078-w)：五分鐘 HRV 的平均與斜率分析，說明趨勢已有前作。
- [Lee 等，2023](https://doi.org/10.1038/s41746-023-00960-2)：HRV 心跳停止預測，提供訊號品質、患者切分與 AUPRC 評估範例。
- [Britsch 等，2025](https://doi.org/10.1038/s43856-025-01192-z)：固定觀察時點的動態 48 小時死亡預測；輸入主要為 EHR，借鏡的是時間設計與外部驗證。

## 資料與定義

- [MIMIC-III Clinical Database v1.4](https://physionet.org/content/mimiciii/1.4/)。
- [MIMIC-III Waveform Database Matched Subset v1.0](https://physionet.org/content/mimic3wdb-matched/1.0/)。
- [Sepsis-3 休克定義研究](https://jamanetwork.com/journals/jama/fullarticle/2492876)：說明為何不能把單純開始升壓劑稱為敗血性休克。
- [官方 MIMIC-III 升壓藥區間程式](https://github.com/MIT-LCP/mimic-code/blob/main/mimic-iii/concepts/durations/vasopressor_durations.sql)。
- [官方 MIMIC-III 感染懷疑程式](https://github.com/MIT-LCP/mimic-code/blob/main/mimic-iii/concepts/treatment/suspicion_of_infection.sql)：回溯 infection time 與即時可得資格要分開。

競賽限制來自使用者提供的《2026 全國大學院校生醫電資創新設計競賽活動辦法 fiin.docx》，送件前仍須核對主辦單位公告。文獻數字只支持各自研究，不視為本專案已達成的效果。
