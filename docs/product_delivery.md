# 研究原型交付與驗收

第一版提供可離線操作的病例回放介面，將模型分數、生命徵象、RR 變異、品質拒絕及研究證據放在同一個工作流程。這是研究檢視工具；v1 尚無清楚的歷史 RR 增益證據，也尚未完成臨床及保留測試驗證。

## 可操作的交付

- `web/index.html`：直接開啟，不需後端、帳號或套件。三個合成情境標示為示意分數。
- `scripts/serve_demo.py`：可選的 loopback 靜態伺服器，只提供公開資產，不提供患者 API。
- `scripts/export_replay.py`：驗證完整 M2 快取、來源指紋、患者分組與已存模型後，固定選取三個開發驗證病例供本機匯入。預測前保存私有選例 manifest，不讀保留測試特徵或重訓。
- `scripts/package_demo.py`：以白名單打包公開網頁及說明；不含資料、權重、設定或其他 repo 檔案。

真實本機匯出包含三例、224 個逐小時時點，113 個當下 RR 片段通過 v1 工程篩選。選中的事件病例最後六小時沒有可用 RR，所以配對模型不出分；保留此結果，不事後換成表現更好的病例。此數字只描述這次固定展示選例，不代表研究族群效能。

## 可重跑的檢查

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python -m pip install playwright==1.56.0
python -m playwright install chromium
python scripts/check_demo_browser.py
python scripts/package_demo.py
```

完整 Python 測試需要 `signal,model,report` 選用依賴；CI 以 `configs/requirements-ci.txt` 約束直接依賴版本。瀏覽器測試僅使用人工合成資料，驗證離線載入、播放、切換模型、缺值不補、品質拒絕、事件到時才顯示、未來數值不能改變當下圖表、JSON 匯入與惡意文字處理、摘要匯出、手機版及無外部請求／持久儲存。

GitHub Actions 使用標準 `ubuntu-24.04` 公開執行器，唯讀 repo 權限、無資料庫帳密、無 MIMIC 掛載。公開 ECG 工程試行與產品測試分開；只保留彙總結果、合成展示與畫面截圖，不上傳下載的原始 ECG。

## 交付狀態

2026-11-01，程式 commit `83ea74c70ae1d9b31f723753e589a3d64d1b2a02` 已完成[正式 CI](https://github.com/zxc29051617/Biomedical-Innovation-2026_HRV-EarlyWarning/actions/runs/41297440776)：

- 364 項 Python 測試全數通過，未以缺依賴跳過測試。
- Chromium 的 11 組瀏覽器操作／隔離檢查通過，包含實際 HTTP loopback 服務與私有路徑拒絕。
- 已檢視實際瀏覽器產生的 1440 px 桌機及 390 px 手機版截圖；手機無水平溢出。
- 離線 ZIP 已建立；本機與 CI SHA-256 相同：`9e71027a126d80c8122c7d757f453570ac49cc42f6a32452842b213c57e77610`。
- 固定公開 ECG pilot 完成 72／72 組評估；結果與不採用候選的決策見[定位驗證紀錄](beat_timing_validation.md)。

程式與操作文件已發布至 GitHub；離線展示可直接使用。CI artifact 含展示包與合成頁面截圖，保留七天；過期後可由同版本原始碼重建。此處的「通過」只指程式及操作驗收，不是醫療效能驗證。

線上 Site 尚未部署。本機環境無法解析部署 Git 主機，因此沒有可驗證的線上展示網址；HTML 離線模式不受影響。選用的本機 HTTP 服務已在正常 CI 環境驗證，本工作環境的 socket 限制仍可能阻止啟動。

## 下一個實際需要的資源

目前不需要付費 API 或 GPU。要把研究原型推進到可主張 HRV／臨床價值，仍需要：

1. 具 ECG 判讀能力、且各自有資料使用權限的兩位審閱者：協助確認節律、搏動定位與正常 NN 適用性，依預先固定的 ICU 片段獨立標註並處理分歧。公開 MIT-BIH pilot 無法替代此步。
2. 可持續使用的本機科學 Python 環境與既有資料掛載：本次原有 `lab410` 路徑在後段已不可見；先依 repo 依賴恢復環境，再進行新一輪患者資料運算。既有成果檔與離線展示仍在。
3. 若需要線上展示，部署環境需能正常連線到 Site 的來源 Git 主機；私有病例仍只在本機匯入。

後續研究保留原患者切分與 v1 結果，先處理量測及覆蓋問題，再加入相同 ECG 心率／歷史／缺失型態對照與同等實際誤報負擔比較；方法鎖定後才使用保留測試。不得為了展示成績事後挑選病例或反覆查看保留測試。
