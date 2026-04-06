from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf


class DataFetcher:
    """Fetch OHLCV with local parquet TTL cache."""

    def __init__(self, cache_dir: str | Path, ttl_hours: int = 24):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = timedelta(hours=ttl_hours)

    def _cache_path(self, ticker: str, start: str, end: str) -> Path:
        safe = ticker.replace("^", "").replace("/", "_")
        return self.cache_dir / f"{safe}_{start}_{end}.parquet"

    def _fresh(self, path: Path) -> bool:
        if not path.exists():
            return False
        modified = datetime.fromtimestamp(path.stat().st_mtime)
        return (datetime.now() - modified) < self.ttl

    @staticmethod
    def _download_end(end: str, interval: str) -> str:
        # yfinance treats `end` as exclusive, so daily ranges need +1 day
        # to reliably include the user's selected end date.
        ts = pd.Timestamp(end)
        if interval.endswith("d"):
            ts = ts + pd.Timedelta(days=1)
        return ts.strftime("%Y-%m-%d")

    @staticmethod
    def _normalize(df: pd.DataFrame) -> pd.DataFrame:
        if isinstance(df.columns, pd.MultiIndex):
            # yfinance can return multi-index columns even for one ticker
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        rename_map = {
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Adj Close": "adj_close",
            "Volume": "volume",
        }
        out = df.rename(columns=rename_map).copy()
        if "adj_close" not in out.columns and "close" in out.columns:
            out["adj_close"] = out["close"]
        out = out[["open", "high", "low", "close", "adj_close", "volume"]]
        out.index = pd.to_datetime(out.index).tz_localize(None)
        out = out.sort_index().dropna()
        return out

    @staticmethod
    def _twse_symbol(ticker: str) -> str | None:
        if ticker.endswith(".TW"):
            return ticker[:-3]
        return None

    @staticmethod
    def _parse_twse_date(value: str) -> pd.Timestamp:
        year, month, day = [int(part) for part in value.split("/")]
        return pd.Timestamp(year=year + 1911, month=month, day=day)

    @staticmethod
    def _parse_twse_number(value: str) -> float:
        text = str(value).replace(",", "").strip()
        if text in {"", "--", "---", "X", "除權息", "除息", "除權"}:
            return float("nan")
        return float(text)

    def _fetch_twse_history(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        symbol = self._twse_symbol(ticker)
        if not symbol:
            return pd.DataFrame()

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.twse.com.tw/",
        })

        start_ts = pd.Timestamp(start).replace(day=1)
        end_ts = pd.Timestamp(end).replace(day=1)
        frames: list[pd.DataFrame] = []

        for month_start in pd.date_range(start=start_ts, end=end_ts, freq="MS"):
            resp = session.get(
                "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY",
                params={
                    "date": month_start.strftime("%Y%m%d"),
                    "stockNo": symbol,
                    "response": "json",
                },
                timeout=20,
            )
            resp.raise_for_status()
            payload = resp.json()
            rows = payload.get("data") or []
            if not rows:
                continue

            parsed_rows = []
            for row in rows:
                if len(row) < 7:
                    continue
                parsed_rows.append({
                    "timestamp": self._parse_twse_date(row[0]),
                    "open": self._parse_twse_number(row[3]),
                    "high": self._parse_twse_number(row[4]),
                    "low": self._parse_twse_number(row[5]),
                    "close": self._parse_twse_number(row[6]),
                    "adj_close": self._parse_twse_number(row[6]),
                    "volume": int(self._parse_twse_number(row[1])) if pd.notna(self._parse_twse_number(row[1])) else 0,
                })

            if parsed_rows:
                frames.append(pd.DataFrame(parsed_rows))

        if not frames:
            return pd.DataFrame()

        df = pd.concat(frames, ignore_index=True)
        df = df.drop_duplicates(subset=["timestamp"]).set_index("timestamp").sort_index()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
        return df.dropna()

    def _fetch_finmind_history(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        symbol = self._twse_symbol(ticker)
        if not symbol:
            return pd.DataFrame()

        resp = requests.get(
            "https://api.finmindtrade.com/api/v4/data",
            params={
                "dataset": "TaiwanStockPrice",
                "data_id": symbol,
                "start_date": pd.Timestamp(start).strftime("%Y-%m-%d"),
                "end_date": pd.Timestamp(end).strftime("%Y-%m-%d"),
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("data") or []
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["date"])
        out = df.loc[:, ["timestamp", "open", "max", "min", "close", "Trading_Volume"]].copy()
        out = out.rename(columns={
            "max": "high",
            "min": "low",
            "Trading_Volume": "volume",
        })
        out["adj_close"] = out["close"]
        for col in ["open", "high", "low", "close", "adj_close", "volume"]:
            out[col] = pd.to_numeric(out[col], errors="coerce")
        out = out.set_index("timestamp")
        out.index = pd.to_datetime(out.index).tz_localize(None)
        out = out.sort_index()
        out = out.loc[(out.index >= pd.Timestamp(start)) & (out.index <= pd.Timestamp(end))]
        return out.dropna()

    def get_ohlcv(self, ticker: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        cache_path = self._cache_path(ticker, start, end)
        if self._fresh(cache_path):
            try:
                return pd.read_parquet(cache_path)
            except Exception:
                cache_path.unlink(missing_ok=True)

        download_end = self._download_end(end, interval)
        errors: list[str] = []

        try:
            df_raw = yf.download(
                ticker,
                start=start,
                end=download_end,
                interval=interval,
                auto_adjust=False,
                progress=False,
                actions=False,
                threads=False,
                group_by="column",
            )
        except Exception as exc:
            df_raw = pd.DataFrame()
            errors.append(f"download: {exc}")

        if df_raw is None or df_raw.empty:
            try:
                df_raw = yf.Ticker(ticker).history(
                    start=start,
                    end=download_end,
                    interval=interval,
                    auto_adjust=False,
                    actions=False,
                )
            except Exception as exc:
                df_raw = pd.DataFrame()
                errors.append(f"history: {exc}")

        if df_raw is None or df_raw.empty:
            if interval == "1d":
                try:
                    df_raw = self._fetch_twse_history(ticker, start, end)
                except Exception as exc:
                    df_raw = pd.DataFrame()
                    errors.append(f"twse: {exc}")

        if df_raw is None or df_raw.empty:
            if interval == "1d":
                try:
                    df_raw = self._fetch_finmind_history(ticker, start, end)
                except Exception as exc:
                    df_raw = pd.DataFrame()
                    errors.append(f"finmind: {exc}")

        if df_raw is None or df_raw.empty:
            detail = "; ".join(errors) if errors else "empty response"
            raise ValueError(f"No market data downloaded for {ticker} ({start}~{end}); {detail}")
        if {"open", "high", "low", "close", "adj_close", "volume"}.issubset(df_raw.columns):
            df = df_raw.copy()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df = df.sort_index().dropna()
        else:
            df = self._normalize(df_raw)
        try:
            df.to_parquet(cache_path)
        except Exception:
            cache_path.unlink(missing_ok=True)
        return df
