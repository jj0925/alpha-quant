"""
apps/ml_engine/tasks.py
Celery tasks for async ML training & prediction.
"""
import os, logging
from pathlib import Path

from celery import shared_task
import pandas as pd

log = logging.getLogger(__name__)


def _upsert_ohlcv_frame(df_raw: pd.DataFrame, tick: str) -> int:
    from apps.market_data.models import OHLCVBar

    records = []
    for ts, row in df_raw.iterrows():
        records.append(OHLCVBar(
            ticker=tick,
            timestamp=ts.date(),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            adj_close=float(row["adj_close"]),
            volume=int(row["volume"]),
        ))

    if not records:
        return 0

    OHLCVBar.objects.bulk_create(
        records,
        update_conflicts=True,
        update_fields=["open", "high", "low", "close", "adj_close", "volume"],
        unique_fields=["ticker", "timestamp"],
    )
    return len(records)


def _fetch_and_store_range(fetcher, tick: str, start, end) -> int:
    padded_start = (pd.Timestamp(start) - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    padded_end = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    df = fetcher.get_ohlcv(tick, padded_start, padded_end)
    return _upsert_ohlcv_frame(df, tick)


@shared_task(bind=True, queue="data_fetch", name="fetch_market_data")
def fetch_market_data(self, ticker: str, benchmark: str,
                      start: str, end: str) -> dict:
    """Download OHLCV and upsert into TimescaleDB."""
    from apps.ml_engine.pipeline.data_fetcher import DataFetcher
    from django.conf import settings

    log.info(f"Fetching {ticker} | {start} → {end}")

    fetcher = DataFetcher(
        cache_dir=Path(settings.BASE_DIR) / "data" / "cache",
        ttl_hours=24,
    )
    n1 = _fetch_and_store_range(fetcher, ticker, start, end)
    n2 = _fetch_and_store_range(fetcher, benchmark, start, end)
    return {"stock_rows": n1, "bench_rows": n2}


@shared_task(bind=True, queue="training", name="run_training",
             soft_time_limit=3600, time_limit=4000)
def run_training(self, run_id: str) -> dict:
    """
    Full ML pipeline for a TrainingRun:
    1. Load OHLCV from TimescaleDB
    2. Build feature windows (FeatureEngine)
    3. Train model (get_trainer)
    4. Run backtest (BacktestEngine)
    5. Predict tomorrow
    6. Persist artifacts & results
    """
    from apps.ml_engine.models import TrainingRun, ModelArtifact, BacktestResult, PredictionRecord
    from apps.ml_engine.pipeline.feature_engine import FeatureEngine, BacktestEngine
    from apps.ml_engine.pipeline.trainer import get_trainer
    from apps.ml_engine.pipeline.data_fetcher import DataFetcher
    from apps.market_data.models import OHLCVBar
    from django.db.models import Min, Max
    from django.conf import settings
    from django.utils import timezone

    run = TrainingRun.objects.select_related("experiment", "model_arch").get(id=run_id)
    exp = run.experiment

    try:
        run.status     = "training"
        run.started_at = timezone.now()
        run.celery_task_id = self.request.id
        run.save(update_fields=["status","started_at","celery_task_id"])
        exp.status = "running"
        exp.save(update_fields=["status"])

        # ── 1. Load data ────────────────────────────────────
        fetcher = DataFetcher(
            cache_dir=Path(settings.BASE_DIR) / "data" / "cache",
            ttl_hours=24,
        )
        for tick in (exp.ticker, exp.benchmark):
            try:
                inserted = _fetch_and_store_range(fetcher, tick, exp.date_start, exp.date_end)
                log.info("Synced %s rows for %s before training run %s", inserted, tick, run_id)
            except Exception as fetch_err:
                log.warning(
                    "Market fetch failed for run %s ticker %s: %s. Fallback to existing DB rows.",
                    run_id,
                    tick,
                    fetch_err,
                )

        def _load(tick, start, end):
            qs = OHLCVBar.objects.filter(
                ticker=tick, timestamp__gte=start, timestamp__lte=end
            ).order_by("timestamp").values("timestamp","open","high","low","adj_close","volume")
            rows = list(qs)
            if not rows:
                agg = OHLCVBar.objects.filter(ticker=tick).aggregate(
                    first_date=Min("timestamp"),
                    last_date=Max("timestamp"),
                )
                first_date = agg["first_date"]
                last_date = agg["last_date"]
                if first_date and last_date:
                    raise ValueError(
                        f"No OHLCV rows for {tick} in selected range {start}~{end}. "
                        f"Available DB range is {first_date}~{last_date}."
                    )
                raise ValueError(
                    f"No OHLCV rows for {tick} in selected range {start}~{end}. "
                    "Fetch also failed, so TimescaleDB currently has no cached rows for this ticker."
                )
            df = pd.DataFrame(rows).set_index("timestamp")
            df.index = pd.to_datetime(df.index)
            return df.ffill().dropna()

        stock_df = _load(exp.ticker,    exp.date_start, exp.date_end)
        bench_df = _load(exp.benchmark, exp.date_start, exp.date_end)
        stock_df, bench_df = stock_df.align(bench_df, join="inner")

        # ── 2. Feature engineering ───────────────────────────
        from apps.market_data.models import FeatureDefinition
        feat_names = []
        for fid in exp.feature_ids:
            fd = FeatureDefinition.objects.get(id=fid)
            feat_names.append(fd.name)

        hp       = run.hparams
        seq_len  = hp.get("seq_length", 60)
        engine   = FeatureEngine(feat_names, seq_length=seq_len)
        data     = engine.build(stock_df, bench_df)

        # ── 3. Train / test split ────────────────────────────
        N        = len(data["windows"])
        if N < 20:
            raise ValueError("Not enough training windows. Please expand date range or reduce seq_length.")
        split    = int(N * hp.get("train_ratio", 0.8))
        if split <= 0 or split >= N:
            raise ValueError("Invalid train_ratio produced empty train/test split.")
        X_train  = data["windows"][:split]
        meta_train = {k: data[k][:split] for k in ("next_ret","bench_ret","vol_20d")}
        X_test   = data["windows"][split:]
        test_dates = data["dates"][split:]
        actual_ret = data["next_ret"][split:]

        run.train_size = split
        run.test_size  = N - split
        run.save(update_fields=["train_size","test_size"])

        # ── 4. Train ─────────────────────────────────────────
        trainer = get_trainer(run.model_arch.arch, len(feat_names), hp)

        def _progress(epoch, loss):
            run.epochs_done  = epoch
            run.loss_history = run.loss_history + [{"epoch": epoch, "loss": round(loss, 6)}]
            run.save(update_fields=["epochs_done","loss_history"])

        result = trainer.fit(X_train, meta_train, callback=_progress)

        # ── 5. Save artifact ─────────────────────────────────
        artifact_dir  = settings.MODEL_ARTIFACTS_DIR
        artifact_path = os.path.join(artifact_dir, f"{run_id}.pt")
        trainer.save(artifact_path)
        ModelArtifact.objects.create(
            run=run, artifact_path=artifact_path,
            model_size_kb=os.path.getsize(artifact_path) // 1024,
        )

        # ── 6. Backtest ──────────────────────────────────────
        probs_test  = trainer.predict(X_test)
        bt_engine   = BacktestEngine(
            confidence_threshold=hp.get("confidence_threshold", 0.45),
            transaction_cost    =hp.get("transaction_cost", 0.002),
        )
        bt_result = bt_engine.run(probs_test, actual_ret, test_dates)
        m = bt_result["metrics"]
        BacktestResult.objects.create(
            run=run,
            total_return=m["total_return"],    bh_return=m["bh_return"],
            annualized_ret=m["annualized_ret"], sharpe_ratio=m["sharpe_ratio"],
            calmar_ratio=m["calmar_ratio"],    max_drawdown=m["max_drawdown"],
            win_rate=m["win_rate"],            turnover_rate=m["turnover_rate"],
            equity_curve=bt_result["equity_curve"],
            bh_curve=bt_result["bh_curve"],
            drawdown_curve=bt_result["drawdown_curve"],
            position_log=bt_result["position_log"],
        )

        # ── 7. Predict tomorrow ──────────────────────────────
        last_window = engine.build_last_window(stock_df, bench_df)
        last_probs  = trainer.predict(last_window)
        tmr         = bt_engine.predict_tomorrow(last_probs, data["feat_df"])

        from datetime import date, timedelta
        today = date.today()
        PredictionRecord.objects.update_or_create(
            run=run, prediction_date=today,
            defaults=dict(
                target_date  = today + timedelta(days=1),
                signal       = tmr["signal"],
                prob_long    = tmr["prob_long"],
                prob_short   = tmr["prob_short"],
                prob_neutral = tmr["prob_neutral"],
                confidence   = tmr["confidence"],
                rsi_14       = tmr["rsi_14"],
                vol_ann      = tmr["vol_ann"],
                stop_loss_pct= tmr["stop_loss_pct"],
                target_pct   = tmr["target_pct"],
            )
        )

        # ── 8. Finalize ──────────────────────────────────────
        run.status      = "done"
        run.finished_at = timezone.now()
        run.save(update_fields=["status","finished_at"])
        exp.status = "done"
        exp.save(update_fields=["status"])

        log.info(f"TrainingRun {run_id} completed | Sharpe={m['sharpe_ratio']}")
        return {"status": "done", "run_id": run_id, "metrics": m, "prediction": tmr}

    except Exception as exc:
        run.status    = "failed"
        run.error_msg = str(exc)
        run.save(update_fields=["status","error_msg"])
        exp.status = "failed"
        exp.save(update_fields=["status"])
        log.exception(f"TrainingRun {run_id} failed: {exc}")
        raise
