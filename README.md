# AlphaQuant

一個以前後端分離架構打造的量化研究平台，讓使用者可以從「選股與期間設定 → 選特徵 → 選模型 → 啟動訓練 → 看回測 → 看明日訊號」走完整個研究流程。

這個專案的核心目標不是直接幫你下單，而是把量化研究流程做成一套可操作、可擴充、可觀察的系統。你可以把它理解成：

- 前端負責操作介面與結果呈現
- 後端負責 API、權限、資料管理
- Celery Worker 負責重運算工作
- Redis 負責任務佇列與快取
- PostgreSQL 負責一般業務資料
- TimescaleDB 負責時間序列市場資料

如果你是第一次接觸這類專案，這份 README 會特別偏向「新手也看得懂」的寫法。

---

## 1. 這個專案在做什麼

AlphaQuant 是一個全端量化研究平台，主要功能包含：

- 使用者登入 / 註冊 / JWT 驗證
- 建立研究實驗（Experiment）
- 選擇股票、基準、期間、特徵、模型
- 啟動非同步模型訓練
- 訓練完成後自動產出回測報告
- 根據最新資料產出隔日操作建議
- 支援多種模型架構，例如 Transformer、LSTM、GRU、TCN、XGBoost、LightGBM

它的典型使用流程如下：

1. 使用者在前端建立一個實驗。
2. 前端把資料送到 Django API。
3. API 建立 `Experiment` 與 `TrainingRun`。
4. API 把訓練工作丟給 Celery。
5. Celery Worker 抓市場資料、算特徵、訓練模型、做回測、存預測結果。
6. 前端輪詢訓練狀態，最後顯示回測頁和預測頁。

---

## 2. 給完全新手的快速理解

如果你不知道什麼是前後端分離、Celery、Redis、雙資料庫，可以先看這一段。

### 前後端分離是什麼

前後端分離就是：

- 前端：React，負責畫畫面、表單、圖表、互動
- 後端：Django REST Framework，負責提供 API

前端不直接碰資料庫，它只會呼叫 API。  
後端不直接管畫面長怎樣，它只回 JSON。

這樣的好處是：

- 前端可以專心做 UI/UX
- 後端可以專心做資料與業務邏輯
- 未來要換 App、Web、行動端比較容易

### Celery Worker 是什麼

有些工作很重，不適合使用者按按鈕後同步等待，例如：

- 抓歷史行情
- 算多個技術指標
- 訓練 PyTorch / XGBoost / LightGBM 模型
- 跑回測
- 做 Walk-forward 或 Monte Carlo

這些都會花時間，所以後端不直接在 API request 內做，而是把工作丟進任務佇列，交給 Celery Worker 在背景處理。

### Redis 是什麼

Redis 在這個專案裡主要扮演兩個角色：

- Celery broker：任務佇列中介站
- Django cache：一些快取用途

你可以把它想成「非常快的記憶體型資料服務」，專門處理很快、很短、很頻繁的資料交換。

### 為什麼要分 PostgreSQL 和 TimescaleDB

因為兩類資料很不一樣：

- 一般資料：使用者、實驗、訓練紀錄、回測結果、預測結果
- 時間序列資料：每日 OHLCV 行情

一般資料適合放在 PostgreSQL。  
大量時間序列資料則更適合放在 TimescaleDB。

所以這個專案做了資料庫分工：

- PostgreSQL：業務資料
- TimescaleDB：行情資料

目前程式還有一層保底設計：如果沒設定 `timescale` 資料庫，`market_data.OHLCVBar` 可以回退寫到預設 PostgreSQL。

---

## 3. 系統架構總覽

```text
使用者
  ↓
React SPA (Vite)
  ↓ HTTP / JWT
Django REST API
  ↓ 建立訓練任務
Redis
  ↓
Celery Worker
  ├─ 抓市場資料
  ├─ 算特徵
  ├─ 訓練模型
  ├─ 產生回測
  └─ 產生明日預測
  ↓
PostgreSQL / TimescaleDB / artifacts
```

### 每個服務各自負責什麼

| 服務 | 角色 | 實際責任 |
|---|---|---|
| `frontend` | React 單頁應用 | 表單、儀表板、訓練進度、回測圖、預測卡片 |
| `backend` | Django API | 驗證、路由、資料模型、建立任務、回傳結果 |
| `celery_worker` | 背景工作者 | 訓練、回測、預測、資料同步等重任務 |
| `celery_beat` | 排程器 | 定時觸發工作 |
| `flower` | 監控工具 | 看 Celery 任務執行狀況 |
| `postgres` | 關聯式資料庫 | 使用者、實驗、訓練紀錄、回測結果、預測結果 |
| `timescale` | 時間序列資料庫 | `OHLCVBar` 市場資料 |
| `redis` | 快取與任務佇列 | Celery broker、cache |

---

## 4. 實際資料流是怎麼跑的

這一段最適合想理解專案內部怎麼動的人。

### 建立實驗到看到結果的完整流程

1. 使用者在前端的「新建實驗」頁面輸入：
   - 股票代碼
   - 基準代碼
   - 開始 / 結束日期
   - 特徵組合
   - 模型架構
   - 超參數
2. 前端呼叫 `/api/experiments/` 建立 `Experiment`
3. 前端呼叫 `/api/experiments/{id}/train/` 建立 `TrainingRun`
4. Django 把 `run_training` 任務丟到 Celery queue
5. Celery Worker 執行：
   - 抓資料或更新資料庫中的 OHLCV
   - 組出特徵矩陣
   - 切 train/test
   - 訓練模型
   - 存模型 artifact
   - 跑回測
   - 產出明日預測
6. 前端持續輪詢 `/api/runs/{id}/status/`
7. 訓練完成後前端跳去：
   - `/run/{id}/backtest`
   - `/run/{id}/prediction`

### 對應的後端程式位置

- 建立實驗與啟動訓練：[backend/apps/ml_engine/views.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\apps\ml_engine\views.py)
- 訓練主流程：[backend/apps/ml_engine/tasks.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\apps\ml_engine\tasks.py)
- 特徵工程與回測：[backend/apps/ml_engine/pipeline/feature_engine.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\apps\ml_engine\pipeline\feature_engine.py)
- 模型訓練器工廠：[backend/apps/ml_engine/pipeline/trainer.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\apps\ml_engine\pipeline\trainer.py)

---

## 5. 專案目錄導覽

```text
alpha-quant/
├─ docker-compose.yml
├─ README.md
├─ backend/
│  ├─ manage.py
│  ├─ requirements.txt
│  ├─ config/
│  │  ├─ settings.py
│  │  ├─ urls.py
│  │  ├─ celery_app.py
│  │  └─ db_router.py
│  └─ apps/
│     ├─ users/
│     ├─ market_data/
│     ├─ ml_engine/
│     └─ backtest/
└─ frontend/
   ├─ package.json
   ├─ vite.config.js
   └─ src/
      ├─ App.jsx
      ├─ api/
      ├─ components/
      ├─ pages/
      └─ store/
```

### 你可以把它粗分成這幾塊

#### `frontend/`

前端 React 專案，主要頁面有：

- [frontend/src/pages/LoginPage.jsx](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\frontend\src\pages\LoginPage.jsx)
- [frontend/src/pages/DashboardPage.jsx](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\frontend\src\pages\DashboardPage.jsx)
- [frontend/src/pages/ExperimentPage.jsx](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\frontend\src\pages\ExperimentPage.jsx)
- [frontend/src/pages/TrainingPage.jsx](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\frontend\src\pages\TrainingPage.jsx)
- [frontend/src/pages/BacktestPage.jsx](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\frontend\src\pages\BacktestPage.jsx)
- [frontend/src/pages/PredictionPage.jsx](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\frontend\src\pages\PredictionPage.jsx)

#### `backend/apps/users/`

處理使用者相關功能：

- 註冊
- 取得目前登入者資料
- 驗證依賴 JWT

#### `backend/apps/market_data/`

處理市場資料與特徵目錄：

- OHLCV 行情資料
- 特徵定義 `FeatureDefinition`
- 使用者特徵預設組合 `FeaturePreset`

#### `backend/apps/ml_engine/`

整個量化研究核心：

- `Experiment`
- `TrainingRun`
- `ModelRegistry`
- `BacktestResult`
- `PredictionRecord`
- Celery 訓練任務
- 特徵工程
- 模型工廠

#### `backend/apps/backtest/`

進階分析：

- Walk-forward validation
- Monte Carlo permutation
- 多個 run 比較表

---

## 6. 資料模型怎麼看

如果你不確定資料表彼此關係，先記這條主線：

```text
User
  └─ Experiment
      └─ TrainingRun
          ├─ ModelArtifact
          ├─ BacktestResult
          └─ PredictionRecord
```

### 核心概念

#### `Experiment`

一個研究主題。  
例如：「長榮 2020-2026，使用 RSI + MACD + Transformer」。

#### `TrainingRun`

同一個實驗可以跑很多次。  
每一次用不同模型或不同超參數，都會是一個新的 `TrainingRun`。

#### `ModelRegistry`

系統支援哪些模型，就登記在這裡。  
前端的模型選單也是從這裡來。

#### `FeatureDefinition`

這是特徵字典。  
定義有哪些指標、屬於哪個分類、說明、計算函式是哪個。

#### `BacktestResult`

訓練完後產出的完整回測結果，包含：

- 總報酬
- 年化報酬
- Sharpe
- 最大回撤
- 資金曲線
- 部位紀錄

#### `PredictionRecord`

訓練完後針對下一個交易日產出的建議：

- LONG / SHORT / NEUTRAL
- 各方向機率
- 信心度
- 風險管理參考值

資料模型可從這裡讀起：

- [backend/apps/ml_engine/models.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\apps\ml_engine\models.py)
- [backend/apps/market_data/models.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\apps\market_data\models.py)

---

## 7. API 概覽

### 認證

| 方法 | 路徑 | 說明 |
|---|---|---|
| `POST` | `/api/auth/register/` | 註冊 |
| `POST` | `/api/auth/token/` | 登入取得 JWT |
| `POST` | `/api/auth/token/refresh/` | 更新 access token |
| `GET` | `/api/auth/me/` | 取得目前使用者 |

### 實驗與訓練

| 方法 | 路徑 | 說明 |
|---|---|---|
| `GET` | `/api/models/` | 取得可用模型清單 |
| `GET` | `/api/experiments/` | 取得所有實驗 |
| `POST` | `/api/experiments/` | 建立實驗 |
| `PATCH` | `/api/experiments/{id}/` | 更新實驗內容 |
| `POST` | `/api/experiments/{id}/train/` | 啟動訓練 |
| `GET` | `/api/runs/{id}/status/` | 查訓練狀態 |
| `GET` | `/api/runs/{id}/backtest/` | 查回測結果 |
| `GET` | `/api/runs/{id}/prediction/` | 查明日預測 |

### 特徵與市場資料

| 方法 | 路徑 | 說明 |
|---|---|---|
| `GET` | `/api/features/` | 取得可用特徵目錄 |
| `GET` | `/api/feature-presets/` | 取得特徵預設組合 |
| `POST` | `/api/feature-presets/` | 建立特徵預設組合 |
| `GET` | `/api/market/ohlcv/` | 查 OHLCV 歷史資料 |

### 進階回測分析

| 方法 | 路徑 | 說明 |
|---|---|---|
| `GET` | `/api/experiments/{id}/compare/` | 比較同一實驗多個 run |
| `GET/POST` | `/api/runs/{id}/walk-forward/` | Walk-forward |
| `GET/POST` | `/api/runs/{id}/monte-carlo/` | Monte Carlo |

Swagger 文件位置：

- `http://localhost:8000/api/docs/`

主路由定義在：

- [backend/config/urls.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\config\urls.py)

---

## 8. 前端是怎麼組的

前端使用：

- React 18
- React Router
- React Query
- Zustand
- Axios
- Recharts
- Vite

### 前端主要責任

- 管理登入狀態
- 送 API 請求
- 進行畫面跳轉
- 輪詢訓練狀態
- 畫回測圖與預測資訊

### 幾個重要檔案

- 路由入口：[frontend/src/App.jsx](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\frontend\src\App.jsx)
- API client：[frontend/src/api/client.js](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\frontend\src\api\client.js)
- JWT 狀態管理：[frontend/src/store/authStore.js](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\frontend\src\store\authStore.js)
- 實驗建立頁：[frontend/src/pages/ExperimentPage.jsx](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\frontend\src\pages\ExperimentPage.jsx)

### 如果你想改前端，通常從哪裡開始

| 想改的東西 | 先看哪裡 |
|---|---|
| 頁面路由 | `frontend/src/App.jsx` |
| 登入流程 | `frontend/src/pages/LoginPage.jsx`、`frontend/src/api/client.js` |
| 新建實驗流程 | `frontend/src/pages/ExperimentPage.jsx` |
| 訓練進度 UI | `frontend/src/pages/TrainingPage.jsx` |
| 回測圖表 | `frontend/src/pages/BacktestPage.jsx` |
| 明日訊號卡片 | `frontend/src/pages/PredictionPage.jsx` |
| 共用版型 | `frontend/src/components/ui/Layout.jsx` |

---

## 9. 後端是怎麼組的

後端使用：

- Django 5
- Django REST Framework
- SimpleJWT
- Celery
- Redis
- PostgreSQL
- TimescaleDB

### 後端主要責任

- 使用者與 JWT 驗證
- 提供 REST API
- 管理資料模型
- 把重任務丟給 Celery
- 提供訓練狀態與結果查詢

### 幾個重要檔案

- Django 設定：[backend/config/settings.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\config\settings.py)
- API 路由：[backend/config/urls.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\config\urls.py)
- Celery 設定：[backend/config/celery_app.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\config\celery_app.py)
- 多資料庫路由：[backend/config/db_router.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\config\db_router.py)

### 如果你想改後端，通常從哪裡開始

| 想改的東西 | 先看哪裡 |
|---|---|
| API 行為 | `backend/apps/*/views.py` |
| 資料表結構 | `backend/apps/*/models.py` |
| 訓練流程 | `backend/apps/ml_engine/tasks.py` |
| 特徵計算 | `backend/apps/ml_engine/pipeline/feature_engine.py` |
| 模型支援 | `backend/apps/ml_engine/pipeline/trainer.py` |
| 預設特徵 / 模型註冊 | `backend/apps/market_data/management/commands/seed_features.py` |

---

## 10. 為什麼 Celery Worker 要和 Backend 分開

這是很多新手第一次看到會疑惑的地方。

### Backend 為什麼不自己訓練就好

因為 API 的工作應該是快進快出。  
如果一個 request 要等 20 秒、2 分鐘、甚至更久，使用體驗和系統穩定性都會很差。

所以比較好的做法是：

- API 只負責接收任務、建立紀錄、回傳 `202 Accepted`
- 真正重的運算交給 Worker

### 分開的好處

- 前端不會卡住
- API 不容易 timeout
- 訓練失敗不會直接拖垮 Web 服務
- 可以獨立擴增 Worker 數量

這也是為什麼 `docker-compose.yml` 裡會看到：

- `backend`
- `celery_worker`
- `celery_beat`
- `flower`

各自是不同容器。

---

## 11. 為什麼市場資料要分出去

`OHLCVBar` 這類資料有兩個特徵：

- 很多列
- 幾乎都跟時間有關

這類資料非常適合放時間序列資料庫。  
所以本專案用 `TimescaleDB` 存市場 OHLCV。

目前路由器設計在：

- [backend/config/db_router.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\config\db_router.py)

它會把 `market_data.OHLCVBar` 導到 `timescale`。  
如果沒有 `timescale` 設定，才回退到 `default`。

這種寫法有兩個好處：

- 正式環境可以走雙資料庫
- 本地開發還是可以先用單資料庫跑起來

---

## 12. 快速啟動

### 方法一：Docker Compose

最推薦新手用這個方式，因為依賴最多，手動裝容易漏。

```bash
git clone <your-repo-url>
cd alpha-quant
docker compose up -d --build
```

啟動後常用入口：

- 前端：`http://localhost:3000`
- 後端 API：`http://localhost:8000/api/`
- API 文件：`http://localhost:8000/api/docs/`
- Flower：`http://localhost:5555`

如果要建立管理員：

```bash
docker compose exec backend python manage.py createsuperuser
```

### 方法二：本地手動開發

#### Backend

```bash
cd backend
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_features
python manage.py runserver
```

#### Celery Worker

另開一個終端：

```bash
cd backend
celery -A config worker -l info -Q training,prediction,data_fetch
```

#### Frontend

另開一個終端：

```bash
cd frontend
npm install
npm run dev
```

前端預設 API 位址來自：

- `VITE_API_URL`
- 若沒設定，會使用 `http://localhost:8000/api`

定義位置：

- [frontend/src/api/client.js](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\frontend\src\api\client.js)

---

## 13. Docker Compose 裡的服務解說

設定檔位置：

- [docker-compose.yml](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\docker-compose.yml)

### `postgres`

主要業務資料庫，存：

- users
- experiments
- training_runs
- model_artifacts
- backtest_results
- prediction_records

### `timescale`

存市場時間序列資料，主要是：

- `ohlcv_bars`

### `redis`

存 Celery 任務佇列與快取。

### `backend`

啟動時會做幾件事：

1. migrate 預設資料庫
2. migrate `timescale` 上的 `market_data`
3. 執行 `seed_features`
4. 啟動 gunicorn

### `celery_worker`

負責執行：

- `run_training`
- `fetch_market_data`
- `run_walk_forward`
- `run_monte_carlo`

### `celery_beat`

負責排程型工作。

### `flower`

Celery 監控頁面。

### `frontend`

打包後由 nginx 提供靜態頁面。

---

## 14. 目前支援的特徵與模型

### 特徵分類

目前種子資料會注入以下特徵：

- 裸 K 價量：`Stock_Ret`、`Vol_Change`、`ATR_Range`、`OBV`
- 動能 / 均值回歸：`RSI_14`、`Stoch_K`、`CCI_20`
- 趨勢：`MACD_Hist`
- 波動率：`Vol_20d`、`BB_Width`
- 相對強弱：`Bench_Ret`、`Excess_Ret`

定義位置：

- [backend/apps/market_data/management/commands/seed_features.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\apps\market_data\management\commands\seed_features.py)
- [backend/apps/ml_engine/pipeline/feature_engine.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\apps\ml_engine\pipeline\feature_engine.py)

### 模型

目前支援：

- Transformer
- LSTM
- GRU
- TCN
- XGBoost
- LightGBM

工廠位置：

- [backend/apps/ml_engine/pipeline/trainer.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\apps\ml_engine\pipeline\trainer.py)

---

## 15. 如果你想改功能，從哪裡改

這一段是給「知道要改，但不知道從哪個檔案下手」的人。

### 想新增一個特徵

1. 在 [backend/apps/ml_engine/pipeline/feature_engine.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\apps\ml_engine\pipeline\feature_engine.py) 新增計算函式
2. 把它加進 `FEATURE_FN_MAP`
3. 在 [backend/apps/market_data/management/commands/seed_features.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\apps\market_data\management\commands\seed_features.py) 加入特徵定義
4. 重新執行：

```bash
python manage.py seed_features
```

### 想新增一種模型

1. 在 [backend/apps/ml_engine/pipeline/trainer.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\apps\ml_engine\pipeline\trainer.py) 新增 trainer 類別
2. 實作統一介面：
   - `fit`
   - `predict`
   - `save`
   - `load`
3. 在 `get_trainer()` 工廠加入分支
4. 在 `seed_features.py` 的 `MODEL_SEED` 加入模型註冊資料
5. 重跑：

```bash
python manage.py seed_features
```

### 想改訓練流程

先看：

- [backend/apps/ml_engine/tasks.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\apps\ml_engine\tasks.py)

這裡是完整主流程，包含：

- 抓資料
- 載入資料
- 組 feature windows
- train/test split
- fit
- 存 artifact
- 跑 backtest
- 存 prediction

### 想改回測邏輯

先看：

- [backend/apps/ml_engine/pipeline/feature_engine.py](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\backend\apps\ml_engine\pipeline\feature_engine.py)

`BacktestEngine` 就在這裡。

### 想改前端實驗建立流程

先看：

- [frontend/src/pages/ExperimentPage.jsx](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\frontend\src\pages\ExperimentPage.jsx)

### 想改訓練完成後的頁面跳轉

先看：

- [frontend/src/pages/TrainingPage.jsx](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\frontend\src\pages\TrainingPage.jsx)

### 想改回測圖表

先看：

- [frontend/src/pages/BacktestPage.jsx](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\frontend\src\pages\BacktestPage.jsx)

### 想改預測卡片與風險建議

先看：

- [frontend/src/pages/PredictionPage.jsx](c:\Users\jacky\Downloads\alpha-quant-v2\alpha-quant\frontend\src\pages\PredictionPage.jsx)

---

## 16. 常見名詞對照

### OHLCV

- Open
- High
- Low
- Close
- Volume

也就是 K 線最基本的市場資料。

### Feature

從原始行情資料算出來的特徵值，例如 RSI、MACD、波動率、超額報酬等。

### Backtest

把策略放回歷史資料中模擬，看看如果當時照策略操作，績效會長怎樣。

### Sharpe Ratio

風險調整後報酬的一種指標。通常越高越好，但也要搭配回撤一起看。

### Max Drawdown

從高點回落到低點的最大幅度。越大通常代表策略波動風險越高。

### Walk-forward

把資料切成多個時間窗，反覆做「訓練一段、測試下一段」，比較接近真實交易世界的驗證法。

### Monte Carlo

用大量模擬方法去看目前策略表現有沒有可能只是運氣好。

---

## 17. 常見問題排查

### 前端可以開，但訓練一直卡在 pending

通常先檢查：

- `celery_worker` 有沒有啟動
- `redis` 有沒有啟動
- worker 與 backend 的 `REDIS_URL` 是否一致

訓練頁其實也有提示這個情況。

### API 正常，但抓不到市場資料

先看：

- 外部資料來源是否可連線
- `timescale` 或 `default` 內是否已有 `OHLCVBar`
- ticker 與日期範圍是否正確

### 訓練直接 failed

常見原因：

- 選的日期範圍太短
- `seq_length` 太長，導致可用 window 太少
- 選到不存在的 feature
- 市場資料根本沒抓到

在 `run_training` 裡已有一些明確錯誤訊息可以幫助排查。

---

## 18. 這份 README 最適合誰

這份文件特別適合：

- 第一次接手這個專案的人
- 只懂前端但想看懂後端架構的人
- 只懂後端但想知道前端怎麼接的人
- 想弄懂 Celery / Redis / 雙資料庫怎麼串的人
- 想知道「我要改功能該從哪個檔案下手」的人

---

## 19. 開發建議

如果你要繼續擴這個專案，建議優先補強幾件事：

- 補 `.env.example`
- 補 migration 檔
- 補單元測試與整合測試
- 補市場資料同步排程文件
- 補 artifact 存放策略說明
- 補正式環境部署說明
- 補權限與錯誤處理規格

這樣專案會更像一個可長期維護的產品，而不只是能跑的 demo。

---

## 20. 免責聲明

本專案僅供研究、學習與工程實作用途，不構成任何投資建議。  
金融市場具有高度風險，請自行判斷並承擔決策結果。
