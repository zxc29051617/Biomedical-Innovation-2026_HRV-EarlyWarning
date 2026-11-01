# 一致定位候選與量測驗證

2026-11-01，`consistent_extremum_candidate_v0.1.0`。此文件先於候選定位的六例診斷與獨立資料評估寫入。v1 資料、品質規則、患者分組及模型完全保留；本候選尚未通過獨立標註驗證，不會接入預警模型。既有六例已揭露問題，僅供回歸診斷，不能再稱盲測或外部驗證。

## 事前方法與用途

候選保留 XQRS 的原始搏動種子，以 200 ms 移動中位數去除局部基線，在每個種子前後 80 ms 內尋找同一極性的最大偏離點。極性由全窗各種子的正負峰幅度中位數決定，最大者至少為另一側 1.1 倍；不逐搏切換正負極性。搜尋窗不足、最大值落在搜尋邊界、非有限訊號、明顯反向優勢、峰頂多解、峰頂平台、半高寬過大或無法界定、重複／次序衝突、鄰近定位不足 200 ms 時，記錄原因並棄權。這些數字是尚未校準的工程參數，並非醫學正常值或臨床驗收門檻。寬 QRS 與不同形態可能因此被拒絕；此法不是完整節律分類或波形分界演算法。

每個候選仍保留其原始種子編號；刪除不合格點不會把缺搏前後接成正常 RR。不得以 RR 平滑度、GQRS 一致性或模型分數挑選／調整定位。GQRS 僅保留作未標註的演算法差異對照。五分鐘中若部分候選棄權，完整窗候選 RMSSD 不提供；共同原始相鄰的局部 RR／RR 差值則可另報。

## 獨立資料 benchmark 的事前規則

第一階段只接受人工審閱搏動標註的 MIT-BIH Arrhythmia 1.0.0 `.atr`，預先指定 100、118、119、200、207、214 六筆，全部使用上方通道、每筆完整非重疊五分鐘窗，不依結果挑選片段。這是方法開發 benchmark，**尚未指定或鎖定獨立保留訊號組與驗收門檻**；只能稱外部標註工程 pilot，不能稱 ICU／NN 臨床驗證完成。清單保存在 `configs/beat_timing_reference.json`，下載後 manifest 包含版本、官方來源、患者群組、逐檔 SHA-256、人工審閱性質與用途。SHA-256 是 HTTPS 取得後的位元組紀錄，不是另由官方公布的預期雜湊。未納入的 201／202 若日後擴充必須同組。MIT-BIH 上方通道通常支撐標註，但有例外及跨通道偏移；報告保留通道名稱供逐筆核對，不能把不同導程的偏移當演算法單獨誤差。

對原取樣率與抗混疊 resample_poly 至 125 Hz 的訊號分開跑未改動的 XQRS 與候選；標註保留原取樣率的秒數，**不四捨五入到 125 Hz**。另報將參考秒數單純量化到 125 Hz 格點的誤差，作為量化對照，而非可達精度保證。125 Hz 一個樣點為 8 ms，濾波、取樣相位及定位誤差可能相加或抵消。

一對一配對以時間順序的動態規劃優先最大化配對數，再最小化絕對時間差；配對容許 100 ms 僅用來決定對應。各方法分開報 sensitivity、PPV、F1、漏搏／多餘搏動；已配對搏動另報 signed bias、絕對時間誤差的中位數／95 百分位／最大值。RR 僅採兩方法與標註中都原始相鄰的兩搏；RMSSD 比較僅採共同原始相鄰三搏，另報整個五分鐘窗全 RR 的 mean RR、SDRR、RMSSD 差異及候選棄權情況。不以僅成功配對的誤差掩蓋漏搏或棄權。

MIT-BIH 標籤可描述搏動種類，但單一 `N` 不足以確認整個窗為正常竇性 NN。此階段全部稱 RR，保留逐類別計數，不提供臨床 NN-HRV 合格率。人工審閱標註仍有定位不確定性及後續自動峰值對齊；並非無誤差的逐樣本人為真值。後續 NSTDB、Fantasia、LUDB 及 ICU 雙人盲標的分組／節律資格設計見 [v2 驗證準備](rr_validation_v2.md)。

## 執行與狀態

實作為 `src/hrv_early_warning/beat_timing.py`，另有獨立下載／驗證 CLI。32 個測試已在 lab410 的 numpy 2.2.6、scipy 1.15.3、wfdb 4.3.0 通過，涵蓋定位邊界、反相、寬峰、重複點、最大配對數／最小時間差、漏搏與棄權後的原始鄰接、100 ms 配對與精度的區別、檔案雜湊與路徑檢查，以及原生 360 Hz／125 Hz 的 WFDB 合成 round trip。合成測試不是獨立標註證據。

```sh
# 在 repo 根目錄；本機 Python 可使用 lab410 的直譯器。
PYTHONPATH=src python -m unittest discover -s tests -p test_beat_timing.py -v
python scripts/download_reference_ecg.py --output-dir artifacts/rr_validation/reference
PYTHONPATH=src python scripts/validate_beat_timing.py check-data --manifest artifacts/rr_validation/reference/manifest.json
PYTHONPATH=src python scripts/validate_beat_timing.py benchmark --manifest artifacts/rr_validation/reference/manifest.json --output-dir artifacts/rr_validation/pilot
PYTHONPATH=src python scripts/validate_beat_timing.py spotcheck --output-dir reports/local/beat_timing_v2
```

下載器固定只向 PhysioNet 1.0.0 取這六筆的 `.hea`、`.dat`、`.atr`，約 12 MB，無需帳密；輸出目錄已有 manifest 時拒絕覆寫。`check-data` 在評估前驗證完整清單、來源、大小與 SHA-256。benchmark 會列出每筆預定窗數、原生／125 Hz 成功及失敗數、完整候選可用窗數與逐窗結果，任何失敗窗均保留並使命令失敗，不靜默排除。輸出為 `benchmark_summary.json`，不含波形或 ICU 資料；公有 CI 可只上傳彙總，無須上傳原始參考資料。

截至本機執行，本機未找到既有獨立 `.atr`／`.qrs`／`.qrsc`，PhysioNet DNS 無法解析，官方 WFDB GitHub sample-data 的二進位亦無法經文字 fetch 取得。因此本機尚未完成公開標註 pilot；上述下載＋benchmark 已準備讓可連網的普通 CI 執行，執行後才能登錄實測數字。若 CI 亦無法連線，最小資源需求就是從官方下載這 18 個檔案（六筆各三檔）及其來源版本，不需要新的 ICU 資料或 API 金鑰。更完整的驗證仍需未用於方法開發的資料、節律／NN 資格與 ICU 雙人盲標。

固定六例診斷已執行，結果在忽略版控的 `reports/local/beat_timing_v2_final/training_diagnostic_summary.json`，輸入仍是原來的 `selection.private.json`。三個 v1 accepted 樣本的候選保留搏數分別為 550/550、516/516、436/439；第三例的 3 搏因峰頂平台棄權，完整五分鐘候選 RMSSD 留空。樣本 1 的全 RR RMSSD 從 62.22 ms 變為 5.92 ms，樣本 2 為 7.98 ms 與 6.24 ms；這只是定位演算法輸出改變，沒有人工參考，因此不能稱誤差下降。樣本 3 在剩餘 428 個原始相鄰三搏的候選 RMSSD 為 19.33 ms，不能拿它與原本全部 437 個三搏直接做公平比較；JSON 另保留完全相同 seed 集合的 baseline 對照。

兩個 rejected 樣本分別因非有限／缺資料、原版在搏動偵測前拒絕而不評估；另一個 rejected 樣本雖保留全部 592 個候選，仍維持 v1 的拒絕決策，未升格為可用 HRV。全部結果皆為 125 Hz、8 ms 格點，沒有修改品質快取或重算預警模型。

沒有通用 RMSSD 誤差合格值；本次不設定通過／不通過門檻，也不宣稱可改善模型表現。

官方來源：[MIT-BIH 1.0.0](https://physionet.org/content/mitdb/1.0.0/)、[標註與定位程序](https://physionet.org/physiobank/database/html/mitdbdir/intro.htm)。原資料授權為 Open Data Commons Attribution License v1.0；需保留版本與原作者引用：Moody GB, Mark RG. The impact of the MIT-BIH Arrhythmia Database. IEEE Eng Med Biol Mag. 2001;20(3):45–50. doi:10.1109/51.932724。
