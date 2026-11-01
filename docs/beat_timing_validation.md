# 一致定位候選與量測驗證

2026-11-01，`consistent_extremum_candidate_v0.1.0`。此文件先於候選定位的六例診斷與獨立資料評估寫入。v1 資料、品質規則、患者分組及模型完全保留；本候選尚未通過獨立標註驗證，不會接入預警模型。既有六例已揭露問題，僅供回歸診斷，不能再稱盲測或外部驗證。

## 事前方法與用途

候選保留 XQRS 的原始搏動種子，以 200 ms 移動中位數去除局部基線，在每個種子前後 80 ms 內尋找同一極性的最大偏離點。極性由全窗各種子的正負峰幅度中位數決定，最大者至少為另一側 1.1 倍；不逐搏切換正負極性。搜尋窗不足、最大值落在搜尋邊界、非有限訊號、明顯反向優勢、峰頂多解、峰頂平台、半高寬過大或無法界定、重複／次序衝突、鄰近定位不足 200 ms 時，記錄原因並棄權。這些數字是尚未校準的工程參數，並非醫學正常值或臨床驗收門檻。寬 QRS 與不同形態可能因此被拒絕；此法不是完整節律分類或波形分界演算法。

每個候選仍保留其原始種子編號；刪除不合格點不會把缺搏前後接成正常 RR。不得以 RR 平滑度、GQRS 一致性或模型分數挑選／調整定位。GQRS 僅保留作未標註的演算法差異對照。五分鐘中若部分候選棄權，完整窗候選 RMSSD 不提供；共同原始相鄰的局部 RR／RR 差值則可另報。

## 獨立資料 benchmark 的事前規則

第一階段只接受人工審閱搏動標註的 MIT-BIH Arrhythmia 1.0.0 `.atr`，預先指定 100、118、119、200、207、214 六筆，全部使用上方通道、每筆完整非重疊五分鐘窗，不依結果挑選片段。這是方法開發 benchmark，**尚未指定或鎖定獨立保留訊號組與驗收門檻**；只能稱外部標註工程 pilot，不能稱 ICU／NN 臨床驗證完成。清單保存在 `configs/beat_timing_reference.json`，下載後 manifest 包含版本、官方來源、患者群組、逐檔 SHA-256、人工審閱性質與用途。SHA-256 是 HTTPS 取得後的位元組紀錄，不是另由官方公布的預期雜湊。未納入的 201／202 若日後擴充必須同組。MIT-BIH 上方通道通常支撐標註，但有例外及跨通道偏移；報告保留通道名稱供逐筆核對，不能把不同導程的偏移當演算法單獨誤差。

對原取樣率與抗混疊 resample_poly 至 125 Hz 的訊號分開跑未改動的 XQRS 與候選；標註保留原取樣率的秒數，**不四捨五入到 125 Hz**。另報將參考秒數單純量化到 125 Hz 格點的誤差，作為量化對照，而非可達精度保證。125 Hz 一個樣點為 8 ms，濾波、取樣相位及定位誤差可能相加或抵消。

一對一配對以時間順序的動態規劃優先最大化配對數，再最小化絕對時間差；配對容許 100 ms 僅用來決定對應。各方法分開報 sensitivity、PPV、F1、漏搏／多餘搏動；已配對搏動另報 signed bias、絕對時間誤差的中位數／95 百分位／最大值。每個方法分別與標註配對：RR 僅採該方法偵測序列與標註中皆原始相鄰的兩搏；RMSSD 僅採這兩個序列中共同原始相鄰的三搏。兩方法保留的搏動子集可能不同。另報整個五分鐘窗全 RR 的 mean RR、SDRR、RMSSD 差異及候選棄權情況。不以僅成功配對的誤差掩蓋漏搏或棄權。

MIT-BIH 標籤可描述搏動種類，但單一 `N` 不足以確認整個窗為正常竇性 NN。此階段全部稱 RR，保留逐類別計數，不提供臨床 NN-HRV 合格率。人工審閱標註仍有定位不確定性及後續自動峰值對齊；並非無誤差的逐樣本人為真值。後續 NSTDB、Fantasia、LUDB 及 ICU 雙人盲標的分組／節律資格設計見 [v2 驗證準備](rr_validation_v2.md)。

## 執行與狀態

實作為 `src/hrv_early_warning/beat_timing.py`，另有獨立下載／驗證 CLI。39 個測試已在 lab410 的 numpy 2.2.6、scipy 1.15.3、wfdb 4.3.0 通過，涵蓋定位邊界、反相、寬峰、重複點、最大配對數／最小時間差、漏搏與棄權後的原始鄰接、100 ms 配對與精度的區別及精確邊界、檔案雜湊與路徑檢查、官方下載轉址限制、跨窗彙總的分母與百分位意義，以及原生 360 Hz／125 Hz 的 WFDB 合成 round trip。合成測試不是獨立標註證據。

首次公開 CI 執行期間、尚未查看任何定位效能結果時，獨立程式審查發現評估器的浮點邊界問題：360 Hz 的第 360／396 樣點恰差 100 ms，但浮點相減可能得到 0.10000000000000009 秒而被排除。`beat_timing_metrics_v0.1.1` 只在配對比較加入 1e-12 秒的數值誤差容許，並回報此數字；新增剛好 100 ms 與超過邊界 2e-12 秒的雙向測試。候選定位版本、全部參數、100 ms 實質配對規則與六筆清單均未改變，這是評估器數值修正，不是依 pilot 效能調參。第一輪 CI 僅保留執行紀錄，不作正式結果；正式 pilot 須以修正後的版本重跑。

第一輪 CI 實際在第一個檔案的官方轉址檢查失敗，未產生 benchmark 結果。查核 [PhysioNet 官方 Files 說明](https://physionet.org/content/mitdb/1.0.0/#files-panel) 明列 `s3://physionet-open/mitdb/1.0.0/`，原始 `100.hea` URL 也轉向同路徑的官方 S3 HTTPS 主機。因此下載器補上相容性：每次跟隨轉址前，只允許原 PhysioNet URL 或 `https://physionet-open.s3.amazonaws.com/mitdb/1.0.0/` 下完全相同檔名；不同主機、版本、檔名或額外查詢參數均拒絕。manifest 同時保留原始 `source_url` 與實際 `resolved_url`。這項修正沒有變更資料版本、清單、標註或演算法。

```sh
# 在 repo 根目錄，使用已安裝 signal 依賴的 Python。
PYTHONPATH=src python -m unittest discover -s tests -p test_beat_timing.py -v
python scripts/download_reference_ecg.py --output-dir artifacts/rr_validation/reference
PYTHONPATH=src python scripts/validate_beat_timing.py check-data --manifest artifacts/rr_validation/reference/manifest.json
PYTHONPATH=src python scripts/validate_beat_timing.py benchmark --manifest artifacts/rr_validation/reference/manifest.json --output-dir artifacts/rr_validation/pilot
PYTHONPATH=src python scripts/validate_beat_timing.py spotcheck --output-dir reports/local/beat_timing_v2
```

下載器固定只向 PhysioNet 1.0.0 取這六筆的 `.hea`、`.dat`、`.atr`，約 12 MB，無需帳密；輸出目錄已有 manifest 時拒絕覆寫。`check-data` 在評估前驗證完整清單、來源、大小與 SHA-256。benchmark 會列出每筆預定窗數、原生／125 Hz 成功及失敗數、完整候選可用窗數與逐窗結果，任何失敗窗均保留並使命令失敗，不靜默排除。輸出為 `benchmark_summary.json`，不含波形或 ICU 資料；公有 CI 可只上傳彙總，無須上傳原始參考資料。

CI 另輸出一行 `PILOT_SUMMARY_JSON=`，其內容也保留在完整 JSON 的 `compact_summary`。每個取樣率、方法分開加總成功評估窗的 reference／detected／matched 搏數及 micro sensitivity／PPV，同時呈現總窗數、失敗窗數與完整候選窗數。定位誤差是各窗中位數的中位數與各窗 p95 的中位數，**後者不是 pooled p95**；RR 是各窗共同原始相鄰 RR 的 MAE 中位數，RMSSD 是各窗共同三搏差異的 signed／absolute 中位數，均附可用窗數。每個方法分別與標註配對，其可評估子集可能不同，因此不能將兩種方法彙總值的差直接視為相同搏動集合的配對效果。

本機缺少獨立標註且無法解析 PhysioNet DNS，已改由普通 GitHub CI 完成固定官方下載與評估，未傳送任何 ICU 資料。2026-11-01 正式結果如下；更完整的驗證仍需未用於方法開發的資料、節律／NN 資格與 ICU 雙人盲標。

## 正式公開標註 pilot 結果

來源 commit `83ea74c70ae1d9b31f723753e589a3d64d1b2a02`，[CI 執行紀錄](https://github.com/zxc29051617/Biomedical-Innovation-2026_HRV-EarlyWarning/actions/runs/41297440771)。六筆紀錄共 36 個五分鐘窗，各跑 360 Hz 與 125 Hz，共 72 組評估，全部完成；每個取樣率均有 13,214 個參考搏動，不能將重複取樣率當成不同病例。完整逐窗報告在該次 CI artifact，永久保留的[機器可讀彙總](results/beat_timing_pilot_summary.json)另記來源 commit、run 與 artifact 雜湊。本節為執行後追加，事前文件指紋以來源 commit 中的檔案為準。

| 取樣率／方法 | 匹配／偵測搏數 | sensitivity | PPV | 定位誤差：各窗 p95 中位數 (ms) | RR MAE：各窗中位數 (ms) | RMSSD 絕對差：各窗中位數 (ms) |
|---|---:|---:|---:|---:|---:|---:|
| 125 Hz XQRS | 13,197／13,684 | 99.87% | 96.44% | 52.16 | 17.74 | 9.96 |
| 125 Hz 候選 | 11,517／11,543 | 87.16% | 99.77% | 6.89 | 2.99 | 0.77 |
| 360 Hz XQRS | 13,138／13,533 | 99.42% | 97.08% | 2.78 | 1.44 | 1.20 |
| 360 Hz 候選 | 11,586／11,634 | 87.68% | 99.59% | 5.56 | 1.57 | 0.41 |

每個方法各自的匹配子集不同；誤差統計可用窗為 XQRS 36／36、候選 35／36。表中「各窗 p95 的中位數」不是全部搏動誤差的第 95 百分位。RR／RMSSD 數字也不是兩方法在相同保留搏動上的配對效果，更不是整段五分鐘 NN-HRV 精度。

125 Hz 候選只有 **9／36** 窗保留全部非空 XQRS 種子，360 Hz 為 **11／36**。這個數量不保證參考搏動全被正確偵測，不代表訊號或正常 NN 合格率。候選在 125 Hz 的保留匹配子集誤差較小，同時 sensitivity 降低；原生 360 Hz 下 baseline 定位誤差與 RR MAE 反而較小，沒有一致優勢。

**決策：不將此候選接入 M2。** 不依本輪結果調參後再把相同六筆稱為獨立驗證。先補充不同形態／節律的棄權分析、相同搏動子集對照及未參與方法開發的訊號驗證，再判斷是否值得建立 v2 快取。這個結果不證明 ICU 泛化、NN-HRV 適用性或預警效益。

固定六例診斷已執行，結果在忽略版控的 `reports/local/beat_timing_v2_final/training_diagnostic_summary.json`，輸入仍是原來的 `selection.private.json`。三個 v1 accepted 樣本的候選保留搏數分別為 550/550、516/516、436/439；第三例的 3 搏因峰頂平台棄權，完整五分鐘候選 RMSSD 留空。樣本 1 的全 RR RMSSD 從 62.22 ms 變為 5.92 ms，樣本 2 為 7.98 ms 與 6.24 ms；這只是定位演算法輸出改變，沒有人工參考，因此不能稱誤差下降。樣本 3 在剩餘 428 個原始相鄰三搏的候選 RMSSD 為 19.33 ms，不能拿它與原本全部 437 個三搏直接做公平比較；JSON 另保留完全相同 seed 集合的 baseline 對照。

兩個 rejected 樣本分別因非有限／缺資料、原版在搏動偵測前拒絕而不評估；另一個 rejected 樣本雖保留全部 592 個候選，仍維持 v1 的拒絕決策，未升格為可用 HRV。全部結果皆為 125 Hz、8 ms 格點，沒有修改品質快取或重算預警模型。

沒有通用 RMSSD 誤差合格值；本次不設定通過／不通過門檻，也不宣稱可改善模型表現。

官方來源：[MIT-BIH 1.0.0](https://physionet.org/content/mitdb/1.0.0/)、[標註與定位程序](https://physionet.org/physiobank/database/html/mitdbdir/intro.htm)。原資料授權為 Open Data Commons Attribution License v1.0；需保留版本與原作者引用：Moody GB, Mark RG. The impact of the MIT-BIH Arrhythmia Database. IEEE Eng Med Biol Mag. 2001;20(3):45–50. doi:10.1109/51.932724。
