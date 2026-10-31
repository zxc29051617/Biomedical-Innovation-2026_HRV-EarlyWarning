# 臨床定義與資料語義的線上證據審查

查證日期：2026-11-01。審查對象：[研究規格](protocol.md)與 [protocol.toml](../configs/protocol.toml) 的可行性規則，審查後落實於版本 0.2。此文件的證據審查只整理公開論文、學會指引與 MIMIC 官方文件／程式；未進行個案判讀。實作後的獨立資料稽核見[資料盤點](data_inventory.md)。

結論：可以先把可查證的醫學定義、藥物對應及時間規則落實成工程規格，不必等待人工逐案判讀才開始。線上來源能支持「如何定義及重建紀錄」，不能證明某位患者當時確實需要用藥，也不能取代本地資料品質稽核或模型驗證。本專案目前的適當終點仍是**本次 ICU 的首次指定升壓劑輸注，且該次連續暴露達研究門檻**。

## 證據分類

| 分類 | 意義 | 工程處理 |
| --- | --- | --- |
| `source-supported` | 原始研究、官方指引或資料文件直接支持的事實 | 保留來源與版本；只在來源適用範圍內使用 |
| `project-defined` | 為可重現研究而選定的族群、門檻、視窗或清理規則 | 在模型評估前凍結；用預先指定敏感度分析檢查 |
| `unobservable` | 單靠目前表格不能可靠確認的病理、適應症或當時資訊 | 保持未知並計數；不推定為陰性、不冒充診斷 |

同一規則可能包含不同層次：例如某藥屬升壓藥有來源支持，但把它納入「四藥主要終點」是本專案選擇。

## 藥物與終點：規則—來源—支持範圍

2026-03-23 發布的 [SSC 2026 官方指引](https://www.sccm.org/clinical-resources/guidelines/guidelines/surviving-sepsis-campaign-international-guidelines-for-management-of-sepsis-and-septic-shock-2026)已更新 2021 版。它適用於成人 sepsis／septic shock 的治療；本專案是歷史成人 ICU 給藥預測，不能把指引中的治療建議改寫成所有 ICU 患者的診斷標籤。

| 規則／藥物 | 分類 | 查證結果與適用限制 | 本專案處理 |
| --- | --- | --- | --- |
| Norepinephrine，MV `221906` | `source-supported`：藥物及 ID；`project-defined`：主要終點 | SSC 2026 保留其主要第一線地位；ID 見 [MIMIC-III 官方藥物區間程式](https://github.com/MIT-LCP/mimic-code/blob/main/mimic-iii/concepts/durations/vasopressor_durations.sql) | 納入；不是 sepsis 的充分證據 |
| Epinephrine，MV `221289` | 同上 | SSC 2026 討論其加用及合併心功能異常情境；同一官方程式列出 ID | 納入；不推定個別給藥原因 |
| Vasopressin，MV `222315` | 同上 | SSC 2026 支持在 norepinephrine 需求增加時加用；官方程式列出 ID | 納入；不可把不同藥物原始數值直接當成同一劑量 |
| Phenylephrine，MV `221749` | `source-supported`：可作升壓治療及 ID；`project-defined`：納入主要四藥 | [Morelli 等 2008 隨機試驗](https://doi.org/10.1186/cc7121)研究其與 norepinephrine 的血流動力學比較；樣本僅 32 人。這支持它是研究過的升壓治療，不能宣稱它與 norepinephrine 有相等臨床效益或是現行第一線推薦 | 保留於成人 ICU 治療使用終點，報告各藥及照護單位分布；不可稱「SSC 標準四藥」 |
| Dopamine，MV `221662` | `source-supported`：藥物及 ID；`project-defined`：僅作敏感度分析 | SSC 2026 優先推薦 norepinephrine；這不表示歷史 dopamine 給藥不算治療 | 主要事件不含它，但既往／首次 dopamine 仍影響未治療風險集合；另報加入 dopamine 的結果 |
| Dobutamine `221653`、milrinone `221986` | `source-supported`：藥物角色不同；`project-defined`：排除主要終點 | SSC 2026 的 inotropes 段落區分強心支持與升壓支持；官方廣義 `vasopressor_durations.sql` 卻將這兩藥一起列入 | 不直接照搬整張官方衍生表作主要終點；排除不代表沒有循環功能異常 |
| 連續至少 60 分鐘；30／120 分鐘敏感度分析 | `project-defined` | 上述指引與資料文件沒有提供本專案這個標籤門檻的臨床標準地位 | 以秒／分鐘實算；不能稱 60 分鐘是 shock 診斷標準 |
| 四主藥區間聯集、可無間斷換藥、零容許間隙 | `project-defined` | 是「持續指定升壓支持」的操作化；同時兩藥不能把時間算兩遍 | 先整理有效實際區間，再求聯集；不讓 dopamine 補上主要四藥的缺口 |
| 6 小時歷史、每小時更新、6 小時主要／2 小時次要預測窗 | `project-defined` | 研究設計，不是醫師應等待的處置時間，也不保證能提早這麼久預警 | 依實際警報至事件的間隔報告提前量，不把 horizon 當成已達成效能 |
| 給藥開始等於 septic shock、必要治療或生理惡化起點 | `unobservable` | [Sepsis-3 原始研究](https://jamanetwork.com/journals/jama/fullarticle/2492876)的 septic shock 還包含 sepsis 背景、升壓劑維持 MAP 與輸液後 lactate >2 mmol/L 等條件；開始用藥本身不足 | 使用「觀察到的治療啟用」；不輸出治療建議或 shock 確診 |

SSC 2026 支持在某些不穩定情況同時開始輸液與升壓支持。Sepsis-3 原研究也沒有建立一個通用的「充分輸液」數值門檻。因此不能另外要求先完成固定 30 mL/kg，才承認原本記錄到的升壓事件；本專案並未重建充分輸液或 septic shock onset。[SSC 2026](https://www.sccm.org/clinical-resources/guidelines/guidelines/surviving-sepsis-campaign-international-guidelines-for-management-of-sepsis-and-septic-shock-2026)、[Sepsis-3 方法與 Delphi 結果](https://jamanetwork.com/journals/jama/fullarticle/2492876)

## INPUTEVENTS_MV：欄位事實與研究處理分開

以下欄位意義核對自 [MIMIC-III INPUTEVENTS_MV 官方文件](https://mimic.mit.edu/docs/iii/tables/inputevents_mv.html)及 [Time types](https://mimic.mit.edu/docs/iii/about/time.html)。表中「處理」是依該語義提出的工程規則，不代表原資料提供者已替本研究驗證。

| 欄位／情境 | 分類與官方支持範圍 | 處理及容易犯的錯誤 |
| --- | --- | --- |
| `STARTTIME`／`ENDTIME` | `source-supported`：該列給藥區間起訖 | 用 start 定義開始；反向或缺失區間不補造持續時間 |
| `STORETIME` | `source-supported`：人工輸入／確認時間 | 與給藥開始分開保存。標籤可用後來確認的結局；特徵不可使用 t 之後才可得的值 |
| `RATE`／`RATEUOM` | `source-supported`：給藥速率及單位 | 正值、有限數與可辨識單位是 `project-defined` 有效列條件；單位未知不等於沒有給藥 |
| `ORIGINALRATE` | `source-supported`：原計畫速率，可能與 `RATE` 不同 | 不用它覆蓋已記錄的實際速率；亦不拿計畫速率當未來風險特徵 |
| `Rewritten` | `source-supported`：被重寫且原量／速率未送達 | 不計該列給藥；保留排除計數 |
| `Changed`／`Paused`／`Stopped`／`FinishedRunning` | `source-supported`：描述這段給藥如何結束 | 不整筆刪除已送達的區間；尤其 `Paused` 不能讓後續同訂單跨越停藥空白 |
| `CANCELREASON`／comments | `source-supported`：有取消／修訂相關欄位；數字細碼本次未能從官方文件確認 | 不自行指定 0／1／2 意義。相互矛盾或無法解讀者標 `unresolved`，另計對首次事件的影響 |
| Bolus | `source-supported`：官方描述 bolus 起訖通常相差一分鐘 | 優先依給藥分類辨識；單憑一分鐘長度的排除屬研究 heuristic，需另計，不能反推所有短列都是 bolus |
| `ORDERID`／`LINKORDERID` | `source-supported`：同溶液成分／同訂單變更的連結 | 不把鹽水成分當升壓藥；不以同 link ID 證明無間斷給藥 |

官方文件沒有詳列全部取消代碼，而且網頁 comments 欄位命名與 [官方 PostgreSQL 建表程式](https://github.com/MIT-LCP/mimic-code/blob/main/mimic-iii/buildmimic/postgres/postgres_create_tables.sql)的 `COMMENTS_EDITEDBY`／`COMMENTS_CANCELEDBY` 不完全一致。實作應讀取當前資料欄名並記錄版本，不能假設網頁中的所有欄位都存在；仍不需要為了這件事輸出患者列。

[官方 `vasopressor_durations.sql`](https://github.com/MIT-LCP/mimic-code/blob/main/mimic-iii/concepts/durations/vasopressor_durations.sql)的 MetaVision 部分先按 `ICUSTAY_ID, LINKORDERID` 取最早 start 與最晚 end。這是可檢查的程式事實，**不能保證中間沒有停藥**。本研究零間隙規則應從逐列有效區間合併，官方程式只用於藥物對應與方法參照，不能稱已完全重現其終點。[官方單藥 dose 程式](https://github.com/MIT-LCP/mimic-code/blob/main/mimic-iii/concepts/durations/norepinephrine_dose.sql)則保留 MetaVision 各列 start／end／rate，亦未替本專案處理全部品質條件。

## 資格、日期與可對齊性

| 規則 | 分類 | 來源支持與實作界線 |
| --- | --- | --- |
| matched waveform 與 clinical 日期對齊 | `source-supported` | [官方 matched subset 文件](https://physionet.org/content/mimic3wdb-matched/1.0/)明示其 surrogate dates 對應 MIMIC-III clinical v1.4；使用 matched master header 的時間及 subject ID，再與同次 ICU 區間相交 |
| 同患者有波形就代表這次 ICU 有 ECG | `unobservable` | 同一官方文件說明可能有多段紀錄、波形邊界不等於 ICU 邊界、通道非全程存在；檔案數或同 ID 不能代替實際時間與 ECG 通道覆蓋 |
| shifted 日期可排序不同患者的真實入院年代 | `unobservable` | [Time types](https://mimic.mit.edu/docs/iii/about/time.html)只保留同患者內一致性；不同患者的未來年份不能直接做真實年代切分 |
| 成年篩選與 >89 歲 | `source-supported`：DOB 隱匿機制；`project-defined`：建模表示 | [PATIENTS 文件](https://mimic.mit.edu/docs/iii/tables/patients.html)說明曾超過 89 歲者 DOB 改為首次入院前 300 年。應保留成人身分及 `age_obscured` 類別，不將約 300 年當真實年齡；若使用 90／91.4 代表值，須明列為研究編碼／插補，91.4 只是該隱匿群中位數 |
| 出院 ICD 可作當下 sepsis 納入條件 | `unobservable` | [DIAGNOSES_ICD 文件](https://mimic.mit.edu/docs/iii/tables/diagnoses_icd.html)明示住院結束時為帳務產生；只作回溯描述子群，不能冒充已完成 Sepsis-3 臨床判定 |
| `PRESCRIPTIONS.STARTDATE` 是精確抗生素給藥時間 | `unobservable` | [PRESCRIPTIONS 文件](https://mimic.mit.edu/docs/iii/tables/prescriptions.html)定義的是處方有效日期；[Time types](https://mimic.mit.edu/docs/iii/about/time.html)說明 DATE 欄位只有日粒度，午夜不是已知實際時刻 |
| 抗生素＋培養配對後可回填較早的線上資格 | `unobservable`；禁止回填是 `project-defined` 防洩漏規則 | [官方 suspicion_of_infection.sql](https://github.com/MIT-LCP/mimic-code/blob/main/mimic-iii/concepts/treatment/suspicion_of_infection.sql)會搜尋先後發生的培養／處方；這是回溯演算法。`max(證據可得時間)` 僅在兩個時間本身可信時有意義，無法補回未記錄的可得時間 |

目前成人 MetaVision 主隊列不需感染資格，能避開處方日粒度對小時級預警的限制。感染／敗血症子群必須按採用的實際定義命名，不能把 ICD 子群改稱即時 Sepsis-3 隊列。以上兩點支持現行主方向，無需因此停止資料工程。

## 需在標籤程式與 protocol 凍結前明確落實的事項

以下為本次證據審查導出的研究設計要求；不是外部來源提供的標準答案。時間分離、未知資料與截尾已落實於可行性程式；完整風險集合、各照護單位分層與逐案抽查仍待下一階段。

1. **標籤時間與可得時間分離。** 使用有效輸注 `STARTTIME` 作回溯結局時間，保留 `STORETIME` 作記錄延遲稽核；不要要求未來結局的 `STORETIME <= t`。對特徵仍限制測量和儲存時間。最終狀態／修訂資料可幫助回溯整理標籤，不能當作 t 當下已知特徵；目前不宣稱完整重建當時的 EHR 畫面。
2. **標籤確認範圍可能超過預測窗。** 若輸注於 `s <= t+6h` 開始，確認是否持續 60 分鐘需觀察至 `s+60min`，可能晚於 `t+6h`；只有 start 落在窗內還不足以成立。確認不足應為未知／截尾，不因缺少結束紀錄標成陰性，也不必對所有無輸注者強加相同額外一小時住院條件。
3. **不明首輸注不能刪掉後假裝從未治療。** 若最早疑似給藥的單位、取消狀態或連續性無法確認，保留 `unresolved_first_exposure`，在該點停止可靠風險追蹤或排除相應時窗。真正 `Rewritten` 的未送達列與資訊不明列不可混為一類。
4. **已知短輸注／dopamine 與未知資料分開計數。** 兩者可能都讓主要事件追蹤停止，但前者是已觀察到的其他治療事件，後者是無法判定。若評估程式均以 censor 處理，應報告原因及可能的資訊性截尾，不宣稱估計了所有 ICU 循環惡化的自然風險。
5. **可觀察「首次」的範圍受限。** 規格限定本次 ICU，應另列入 ICU 時已在輸注、ICU 外／轉入前用藥不明者。不能把未在此表看到之前給藥，當成整次住院或一生首次。
6. **未知不可被漂亮分數掩蓋。** 產出逐步納入數、五藥各自事件數、無法解讀／bolus／rewritten 原因、duration 門檻差異，以及匹配 ECG 的時間覆蓋。目前可行性程式提供給藥列數、清理原因、事件總數、duration 敏感度與 ECG metadata 覆蓋；各藥首次事件與照護單位分布仍需補齊。

## 可支持的作品主張

可支持：依公開醫學與資料定義，建立可稽核的首次持續升壓劑使用標籤，再檢驗 HRV 對未來治療啟用風險的增量價值。

目前不支持：診斷 septic shock、判斷醫師是否應給藥、證實充分輸液、保證改善預後、宣稱已具有外部或臨床部署驗證。醫學背景隊友及指導教授可進一步核對操作定義與案例；這是後續加強驗證，不是本輪工程必須等待的前置許可。

## 來源核對紀錄

- SSC 2026：已讀 SCCM 官方完整建議頁之 fluid resuscitation、vasopressors、inotropes 段落；DOI `10.1097/CCM.0000000000007075`。2021 僅為前版背景，不稱最新。
- Sepsis-3 shock 原始研究：已讀 JAMA 全文的定義、Delphi 及方法段落；DOI `10.1001/jama.2016.0289`。
- Phenylephrine RCT：已讀原始摘要及可取得的全文結果／討論索引；DOI `10.1186/cc7121`。PMC 直接開啟曾遇瀏覽驗證，因此未以未讀段落延伸治療結論。
- MIMIC：已讀上方官方表格文件、時間文件、matched dataset 說明，以及公開 SQL 的實際條件；不是只按檔名推測。
- GitHub 連結目前指向 `main`，內容可能更新。正式凍結時應固定來源 commit 與對應字典版本；本文件不宣稱已固定上游版本或已執行官方 SQL。
