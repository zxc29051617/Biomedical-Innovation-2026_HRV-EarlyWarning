# 已確認資料與尚待建立的研究隊列

2026 年 11 月 1 日在現有唯讀掛載盤點。以下為資料表與索引的彙總，並非最終模型樣本。

| 項目 | 數量 |
|---|---:|
| MIMIC-III 患者 | 46,520 |
| 住院 | 58,976 |
| ICU stays | 61,532 |
| 純 MetaVision ICU stays | 23,620 |
| 純 MetaVision 患者 | 17,710 |
| 純 MetaVision 患者在波形索引的交集 | 7,242 |
| RECORDS-waveforms 紀錄 | 22,317 |
| RECORDS-waveforms 患者 | 10,099 |
| RECORDS 全部患者目錄，含可能僅有 numerics 者 | 10,282 |

| 住院診斷標記 | 有該診斷的患者 | 患者 ID 亦在 waveform 索引 |
|---|---:|---:|
| MI 410* | 5,032 | 1,540 |
| HF 428* | 10,154 | 3,047 |
| Sepsis 99591／99592 | 4,560 | 1,370 |
| Septic shock 78552 | 2,416 | 770 |
| Cardiac arrest 4275 | 1,339 | 425 |

疾病可重疊。上表只證明患者 ID 有交集，尚未確認同次住院、成人、ICU 系統、ECG 通道、事件前時窗、訊號品質或當下可辨識的疾病資格。ICD 標記也不提供事件發生時間。

初步便利抽樣 30 筆 waveform master／segment headers 均看到 125 Hz 與 II 通道，另成功讀取 8 筆原始資料檔片段。這不是全體通道比例估計、波形解碼驗證或品質驗證。

臨床掛載含 CHARTEVENTS、LABEVENTS、MICROBIOLOGYEVENTS、INPUTEVENTS_MV／CV、OUTPUTEVENTS、PRESCRIPTIONS 等資料表。`mimic-iv-3.1.zip` 的目錄有 hosp／icu 臨床表，未見 ECG 波形。

本地 `mimic-code-2.5.0` 的部分 `concepts_postgres` 檔案只有未完成的 CREATE TABLE 開頭；不能依檔名認定 Sepsis-3 或 SOFA 已可執行。`concepts/durations/vasopressor_durations.sql` 有完整查詢結構，但藥物集合包含 dobutamine／milrinone，且須重新核對間隔與修訂紀錄後才適用本專案終點。

下一份必要證據是逐階段的患者與事件數：成人 MetaVision → 同次 ICU 波形重疊 → 有效輸注 → 事件前 6 小時可用 ECG → 品質合格 → 可形成完整預測窗。
