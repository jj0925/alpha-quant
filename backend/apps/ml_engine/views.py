import csv
import re
from datetime import datetime, timedelta, time as dt_time

from django.db import connections, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import filters, permissions, status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
from apps.ml_engine.tasks import _evaluate_feedback_for_deployment


def _serialize_run_summary(run):
    data = {
        "id": str(run.id),
        "status": run.status,
        "epochs_done": run.epochs_done,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "model_name": getattr(run.model_arch, "display_name", None),
        "model_arch_id": str(run.model_arch_id) if getattr(run, "model_arch_id", None) else None,
        "hparams": run.hparams,
    }
    backtest = getattr(run, "backtest", None)
    if backtest:
        data["backtest"] = {
            "total_return": backtest.total_return,
            "sharpe_ratio": backtest.sharpe_ratio,
            "max_drawdown": backtest.max_drawdown,
        }
    return data


def _yesterday():
    return timezone.localdate() - timedelta(days=1)


def _today():
    return timezone.localdate()


def _next_run_at(deployment):
    if not getattr(deployment, "auto_predict_enabled", False) or not getattr(deployment, "auto_predict_time", None):
        return None

    tz = timezone.get_current_timezone()
    now = timezone.localtime()
    scheduled = timezone.make_aware(
        datetime.combine(now.date(), deployment.auto_predict_time),
        tz,
    )
    if deployment.last_auto_predicted_for == now.date() or scheduled <= now:
        scheduled += timedelta(days=1)
    return scheduled


def _find_existing_live_cycle_run(deployment, cycle_date=None):
    cycle_date = cycle_date or _today()
    return (
        deployment.runs.filter(
            Q(prediction_date=cycle_date) |
            Q(created_at__date=cycle_date, status__in=["pending", "training", "done"])
        )
        .exclude(status="failed")
        .order_by("-created_at", "-id")
        .first()
    )


def _serialize_live_run_brief(run):
    if not run:
        return None
    return {
        "id": str(run.id),
        "status": run.status,
        "signal": run.signal,
        "prediction_date": run.prediction_date,
        "target_date": run.target_date,
        "confidence": run.confidence,
        "prob_long": run.prob_long,
        "prob_short": run.prob_short,
        "prob_neutral": run.prob_neutral,
        "training_window_start": run.training_window_start,
        "training_window_end": run.training_window_end,
        "created_at": run.created_at,
    }


def _parse_time_string(value):
    if isinstance(value, dt_time):
        return value
    if not value:
        return dt_time(18, 10)
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    raise ValueError("Invalid time format. Expected HH:MM or HH:MM:SS")


def _table_exists(table_name: str, using: str = "default") -> bool:
    return table_name in connections[using].introspection.table_names()


def _safe_export_component(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or fallback)).strip("-._")
    return cleaned or fallback


def _delete_experiment_records(exp) -> None:
    from apps.ml_engine.models import (
        BacktestResult,
        Experiment,
        ModelArtifact,
        PredictionRecord,
        TrainingRun,
    )

    run_ids = list(TrainingRun.objects.filter(experiment=exp).values_list("id", flat=True))

    # Optional backtest-analysis tables may not exist in every environment.
    if run_ids and _table_exists("walk_forward_results"):
        from apps.backtest.models import WalkForwardResult
        WalkForwardResult.objects.filter(run_id__in=run_ids).delete()

    if run_ids and _table_exists("monte_carlo_results"):
        from apps.backtest.models import MonteCarloResult
        MonteCarloResult.objects.filter(run_id__in=run_ids).delete()

    if _table_exists("benchmark_comparisons"):
        from apps.backtest.models import BenchmarkComparison
        BenchmarkComparison.objects.filter(experiment=exp).delete()

    PredictionRecord.objects.filter(run_id__in=run_ids).delete()
    BacktestResult.objects.filter(run_id__in=run_ids).delete()
    ModelArtifact.objects.filter(run_id__in=run_ids).delete()
    TrainingRun.objects.filter(id__in=run_ids).delete()
    Experiment.objects.filter(id=exp.id).delete()


def _delete_live_deployment_records(deployment) -> None:
    from apps.ml_engine.models import LiveDeployment, LivePredictionFeedback, LiveRun

    run_ids = list(LiveRun.objects.filter(deployment=deployment).values_list("id", flat=True))
    LivePredictionFeedback.objects.filter(deployment=deployment).delete()
    if run_ids:
        LiveRun.objects.filter(id__in=run_ids).delete()
    LiveDeployment.objects.filter(id=deployment.id).delete()


class ModelRegistryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.ml_engine.models import ModelRegistry

        data = list(ModelRegistry.objects.filter(is_active=True).values(
            "id",
            "arch",
            "display_name",
            "description",
            "default_hparams",
        ))
        for item in data:
            item["id"] = str(item["id"])
        return Response(data)


class ExperimentViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["status"]
    ordering_fields = ["created_at"]
    ordering = ["-created_at"]

    def get_queryset(self):
        from apps.ml_engine.models import Experiment

        return Experiment.objects.filter(user=self.request.user).prefetch_related(
            "runs__model_arch",
            "runs__backtest",
        )

    def list(self, request, *args, **kwargs):
        qs = self.get_queryset()
        data = []
        for exp in qs:
            runs = list(exp.runs.all().order_by("-started_at"))
            latest_run = runs[0] if runs else None
            latest_done = next((r for r in runs if r.status == "done" and hasattr(r, "backtest")), None)
            data.append({
                "id": str(exp.id),
                "name": exp.name,
                "description": exp.description,
                "ticker": exp.ticker,
                "benchmark": exp.benchmark,
                "date_start": exp.date_start,
                "date_end": exp.date_end,
                "status": exp.status,
                "feature_ids": exp.feature_ids,
                "random_seed": exp.random_seed,
                "split_config": exp.split_config or {"train": 70, "val": 15, "test": 15},
                "runs": [_serialize_run_summary(latest_run)] if latest_run else [],
                "latest_backtest": (
                    {
                        "total_return": latest_done.backtest.total_return,
                        "sharpe_ratio": latest_done.backtest.sharpe_ratio,
                        "max_drawdown": latest_done.backtest.max_drawdown,
                        "run_id": str(latest_done.id),
                    } if latest_done else None
                ),
                "created_at": exp.created_at,
                "updated_at": exp.updated_at,
            })
        return Response(data)

    def create(self, request, *args, **kwargs):
        from apps.ml_engine.models import Experiment

        d = request.data
        exp = Experiment.objects.create(
            user=request.user,
            name=d.get("name", ""),
            description=d.get("description", ""),
            ticker=d.get("ticker", "2603.TW"),
            benchmark=d.get("benchmark", "0050.TW"),
            date_start=d.get("date_start", "2020-01-01"),
            date_end=d.get("date_end", _yesterday()),
            random_seed=int(d.get("random_seed", 42)),
            split_config=d.get("split_config") or {"train": 70, "val": 15, "test": 15},
            feature_ids=d.get("feature_ids", []),
        )
        return Response({"id": str(exp.id), "name": exp.name, "status": exp.status}, status=201)

    def partial_update(self, request, pk=None, *args, **kwargs):
        from apps.ml_engine.models import Experiment

        exp = Experiment.objects.get(id=pk, user=request.user)
        for field in (
            "name",
            "description",
            "ticker",
            "benchmark",
            "date_start",
            "date_end",
            "feature_ids",
            "status",
            "random_seed",
            "split_config",
        ):
            if field in request.data:
                setattr(exp, field, request.data[field])
        exp.save()
        return Response({"id": str(exp.id)})

    def retrieve(self, request, pk=None, *args, **kwargs):
        from apps.ml_engine.models import Experiment

        exp = Experiment.objects.get(id=pk, user=request.user)
        runs = [_serialize_run_summary(run) for run in exp.runs.all().order_by("-started_at").select_related("model_arch").prefetch_related("backtest")]
        return Response({
            "id": str(exp.id),
            "name": exp.name,
            "description": exp.description,
            "ticker": exp.ticker,
            "benchmark": exp.benchmark,
            "date_start": exp.date_start,
            "date_end": exp.date_end,
            "status": exp.status,
            "feature_ids": exp.feature_ids,
            "random_seed": exp.random_seed,
            "split_config": exp.split_config or {"train": 70, "val": 15, "test": 15},
            "created_at": exp.created_at,
            "updated_at": exp.updated_at,
            "runs": runs,
        })

    def destroy(self, request, pk=None, *args, **kwargs):
        from apps.ml_engine.models import Experiment

        exp = Experiment.objects.filter(id=pk, user=request.user).first()
        if not exp:
            return Response({"error": "Experiment not found"}, status=404)
        with transaction.atomic():
            _delete_experiment_records(exp)
        return Response(status=204)


class LaunchTrainingView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, exp_id):
        from apps.ml_engine.models import Experiment, ModelRegistry, TrainingRun
        from apps.ml_engine.tasks import run_training

        exp = Experiment.objects.get(id=exp_id, user=request.user)
        arch_id = request.data.get("model_arch_id")
        hparams = request.data.get("hparams", {})
        arch = ModelRegistry.objects.get(id=arch_id)
        run = TrainingRun.objects.create(experiment=exp, model_arch=arch, hparams=hparams)
        exp.status = "queued"
        exp.save(update_fields=["status"])

        task = run_training.apply_async(args=[str(run.id)], queue="training")
        run.celery_task_id = task.id
        run.save(update_fields=["celery_task_id"])
        return Response({"run_id": str(run.id), "task_id": task.id}, status=202)


class RetrainExperimentView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, exp_id):
        from apps.ml_engine.models import Experiment, ModelRegistry, TrainingRun
        from apps.ml_engine.tasks import run_training

        exp = Experiment.objects.prefetch_related("runs__model_arch").get(id=exp_id, user=request.user)
        payload = request.data
        model_arch_id = payload.get("model_arch_id")
        hparams = payload.get("hparams") or {}
        feature_ids = payload.get("feature_ids")
        fork_name = payload.get("fork_name")

        if not model_arch_id:
            latest_run = exp.runs.order_by("-started_at", "-id").first()
            if latest_run:
                model_arch_id = str(latest_run.model_arch_id)
                if not hparams:
                    hparams = latest_run.hparams

        if not model_arch_id:
            return Response({"error": "model_arch_id is required"}, status=400)

        model_arch = ModelRegistry.objects.get(id=model_arch_id)
        experiment_fields = {
            "name": payload.get("name", exp.name),
            "description": payload.get("description", exp.description),
            "ticker": payload.get("ticker", exp.ticker),
            "benchmark": payload.get("benchmark", exp.benchmark),
            "date_start": payload.get("date_start", exp.date_start),
            "date_end": payload.get("date_end", exp.date_end),
            "random_seed": int(payload.get("random_seed", exp.random_seed)),
            "split_config": payload.get("split_config") or exp.split_config or {"train": 70, "val": 15, "test": 15},
            "feature_ids": feature_ids if feature_ids is not None else exp.feature_ids,
        }

        if exp.status == "done":
            if not fork_name:
                return Response({"error": "fork_name is required for completed experiments"}, status=400)
            target_exp = Experiment.objects.create(
                user=request.user,
                name=fork_name,
                description=experiment_fields["description"],
                ticker=experiment_fields["ticker"],
                benchmark=experiment_fields["benchmark"],
                date_start=experiment_fields["date_start"],
                date_end=experiment_fields["date_end"],
                random_seed=experiment_fields["random_seed"],
                split_config=experiment_fields["split_config"],
                feature_ids=experiment_fields["feature_ids"],
                status="queued",
            )
            action = "forked"
        else:
            target_exp = exp
            for field, value in experiment_fields.items():
                setattr(target_exp, field, value)
            target_exp.status = "queued"
            target_exp.save()
            action = "retrained"

        run = TrainingRun.objects.create(
            experiment=target_exp,
            model_arch=model_arch,
            hparams=hparams,
            status="pending",
        )
        task = run_training.apply_async(args=[str(run.id)], queue="training")
        run.celery_task_id = task.id
        run.save(update_fields=["celery_task_id"])

        return Response({
            "action": action,
            "experiment_id": str(target_exp.id),
            "run_id": str(run.id),
            "task_id": task.id,
        }, status=202)


class TrainingRunStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, run_id):
        from apps.ml_engine.models import TrainingRun

        run = TrainingRun.objects.select_related("experiment", "model_arch").get(
            id=run_id,
            experiment__user=request.user,
        )
        return Response({
            "id": str(run.id),
            "status": run.status,
            "epochs_done": run.epochs_done,
            "loss_history": run.loss_history,
            "error_msg": run.error_msg,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "celery_task_id": run.celery_task_id,
            "train_size": run.train_size,
            "val_size": run.val_size,
            "test_size": run.test_size,
            "hparams": run.hparams,
            "model_arch": {
                "id": str(run.model_arch_id),
                "display_name": run.model_arch.display_name,
                "arch": run.model_arch.arch,
            },
            "experiment": {
                "id": str(run.experiment_id),
                "status": run.experiment.status,
                "split_config": run.experiment.split_config,
                "random_seed": run.experiment.random_seed,
            },
            "server_time": timezone.now(),
        })


class BacktestResultView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, run_id):
        from apps.ml_engine.models import BacktestResult, TrainingRun

        run = TrainingRun.objects.get(id=run_id, experiment__user=request.user)
        try:
            bt = BacktestResult.objects.get(run=run)
        except BacktestResult.DoesNotExist:
            return Response({"status": "not_ready"}, status=202)
        return Response({
            "metrics": {
                "total_return": bt.total_return,
                "bh_return": bt.bh_return,
                "annualized_ret": bt.annualized_ret,
                "sharpe_ratio": bt.sharpe_ratio,
                "calmar_ratio": bt.calmar_ratio,
                "max_drawdown": bt.max_drawdown,
                "win_rate": bt.win_rate,
                "turnover_rate": bt.turnover_rate,
            },
            "equity_curve": bt.equity_curve,
            "bh_curve": bt.bh_curve,
            "drawdown_curve": bt.drawdown_curve,
            "position_log": bt.position_log,
        })


class RunSourceDataExportView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, run_id):
        from apps.market_data.models import OHLCVBar
        from apps.ml_engine.models import TrainingRun

        run = TrainingRun.objects.select_related("experiment").get(
            id=run_id,
            experiment__user=request.user,
        )
        exp = run.experiment
        raw_fields = ("timestamp", "open", "high", "low", "close", "adj_close", "volume")

        stock_rows = {
            row["timestamp"]: row
            for row in OHLCVBar.objects.filter(
                ticker=exp.ticker,
                timestamp__gte=exp.date_start,
                timestamp__lte=exp.date_end,
            ).order_by("timestamp").values(*raw_fields)
        }
        benchmark_rows = {
            row["timestamp"]: row
            for row in OHLCVBar.objects.filter(
                ticker=exp.benchmark,
                timestamp__gte=exp.date_start,
                timestamp__lte=exp.date_end,
            ).order_by("timestamp").values(*raw_fields)
        }

        # The training pipeline aligns stock and benchmark on their shared dates.
        aligned_dates = sorted(set(stock_rows).intersection(benchmark_rows))
        if not aligned_dates:
            return Response({"error": "No aligned source data found for this run."}, status=404)

        stock_name = _safe_export_component(exp.ticker, "stock")
        benchmark_name = _safe_export_component(exp.benchmark, "benchmark")
        filename = (
            f"source_data_{stock_name}_vs_{benchmark_name}_"
            f"{exp.date_start}_{exp.date_end}_{str(run.id)[:8]}.csv"
        )

        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Access-Control-Expose-Headers"] = "Content-Disposition"
        response.write("\ufeff")

        writer = csv.writer(response)
        writer.writerow([
            "date",
            "stock_ticker",
            "stock_open",
            "stock_high",
            "stock_low",
            "stock_close",
            "stock_adj_close",
            "stock_volume",
            "benchmark_ticker",
            "benchmark_open",
            "benchmark_high",
            "benchmark_low",
            "benchmark_close",
            "benchmark_adj_close",
            "benchmark_volume",
        ])

        for current_date in aligned_dates:
            stock = stock_rows[current_date]
            benchmark = benchmark_rows[current_date]
            writer.writerow([
                current_date.isoformat(),
                exp.ticker,
                stock["open"],
                stock["high"],
                stock["low"],
                stock["close"],
                stock["adj_close"],
                stock["volume"],
                exp.benchmark,
                benchmark["open"],
                benchmark["high"],
                benchmark["low"],
                benchmark["close"],
                benchmark["adj_close"],
                benchmark["volume"],
            ])

        return response


class PredictionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, run_id):
        from apps.ml_engine.models import PredictionRecord, TrainingRun

        run = TrainingRun.objects.get(id=run_id, experiment__user=request.user)
        pred = PredictionRecord.objects.filter(run=run).order_by("-prediction_date").first()
        if not pred:
            return Response({"error": "No prediction yet"}, status=404)
        return Response({
            "signal": pred.signal,
            "prob_long": pred.prob_long,
            "prob_short": pred.prob_short,
            "prob_neutral": pred.prob_neutral,
            "confidence": pred.confidence,
            "directional_edge": round(float(pred.prob_long - pred.prob_short), 4),
            "rsi_14": pred.rsi_14,
            "vol_ann": pred.vol_ann,
            "excess_ret": 0.0,
            "stop_loss_pct": pred.stop_loss_pct,
            "target_pct": pred.target_pct,
            "prediction_date": str(pred.prediction_date),
            "target_date": str(pred.target_date),
        })


class LiveDeploymentListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.ml_engine.models import LiveDeployment

        rows = (
            LiveDeployment.objects
            .filter(user=request.user)
            .select_related("model_arch", "source_experiment")
            .prefetch_related("runs", "feedback_items")
        )
        data = []
        for deployment in rows:
            latest_run = deployment.runs.order_by("-created_at").first()
            feedback_latest = deployment.feedback_items.order_by("-target_date").first()
            existing_today = _find_existing_live_cycle_run(deployment)
            next_run_at = _next_run_at(deployment)
            data.append({
                "id": str(deployment.id),
                "name": deployment.name,
                "description": deployment.description,
                "ticker": deployment.ticker,
                "benchmark": deployment.benchmark,
                "status": deployment.status,
                "date_start": deployment.date_start,
                "date_end": deployment.date_end,
                "model_arch": {
                    "id": str(deployment.model_arch_id),
                    "display_name": deployment.model_arch.display_name,
                    "arch": deployment.model_arch.arch,
                },
                "feature_ids": deployment.feature_ids,
                "hparams": deployment.hparams,
                "random_seed": deployment.random_seed,
                "auto_predict_enabled": deployment.auto_predict_enabled,
                "auto_predict_time": deployment.auto_predict_time.strftime("%H:%M:%S") if deployment.auto_predict_time else None,
                "last_auto_predicted_for": deployment.last_auto_predicted_for,
                "next_run_at": next_run_at,
                "schedule_status": "enabled" if deployment.auto_predict_enabled else "disabled",
                "today_prediction_done": bool(existing_today and existing_today.status == "done"),
                "today_prediction_in_progress": bool(existing_today and existing_today.status in {"pending", "training"}),
                "today_prediction_run_id": str(existing_today.id) if existing_today else None,
                "source_experiment_id": str(deployment.source_experiment_id) if deployment.source_experiment_id else None,
                "latest_run": _serialize_live_run_brief(latest_run),
                "latest_feedback": (
                    {
                        "target_date": feedback_latest.target_date,
                        "hit_rate": feedback_latest.hit_rate,
                        "cumulative_pnl": feedback_latest.cumulative_pnl,
                        "alpha_drift": feedback_latest.alpha_drift,
                        "was_correct": feedback_latest.was_correct,
                    } if feedback_latest else None
                ),
                "created_at": deployment.created_at,
                "updated_at": deployment.updated_at,
            })
        return Response(data)

    def post(self, request):
        from apps.ml_engine.models import Experiment, LiveDeployment

        exp = Experiment.objects.get(id=request.data.get("source_experiment_id"), user=request.user)
        source_run = (
            exp.runs
            .filter(status="done")
            .select_related("model_arch")
            .order_by("-finished_at", "-started_at", "-id")
            .first()
        )
        if not source_run:
            return Response({"error": "Experiment has no completed run to deploy."}, status=400)

        payload = request.data
        auto_predict_time = _parse_time_string(payload.get("auto_predict_time"))
        deployment = LiveDeployment.objects.create(
            user=request.user,
            source_experiment=exp,
            source_run=source_run,
            model_arch=source_run.model_arch,
            name=payload.get("name") or f"{exp.name} Live",
            description=payload.get("description", exp.description),
            ticker=payload.get("ticker", exp.ticker),
            benchmark=payload.get("benchmark", exp.benchmark),
            date_start=payload.get("date_start", exp.date_start),
            date_end=payload.get("date_end", _yesterday()),
            feature_ids=exp.feature_ids,
            hparams=source_run.hparams,
            random_seed=payload.get("random_seed", exp.random_seed),
            auto_predict_enabled=bool(payload.get("auto_predict_enabled", False)),
            auto_predict_time=auto_predict_time,
            status="draft",
        )
        return Response({"id": str(deployment.id), "status": deployment.status}, status=201)


class LiveDeploymentDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, deployment_id):
        from apps.ml_engine.models import LiveDeployment

        deployment = LiveDeployment.objects.select_related("model_arch", "source_experiment", "source_run").get(
            id=deployment_id,
            user=request.user,
        )
        runs = deployment.runs.order_by("-created_at")
        latest_run = runs.first()
        next_run_at = _next_run_at(deployment)
        existing_today = _find_existing_live_cycle_run(deployment)
        return Response({
            "id": str(deployment.id),
            "name": deployment.name,
            "description": deployment.description,
            "ticker": deployment.ticker,
            "benchmark": deployment.benchmark,
            "status": deployment.status,
            "date_start": deployment.date_start,
            "date_end": deployment.date_end,
            "feature_ids": deployment.feature_ids,
            "hparams": deployment.hparams,
            "random_seed": deployment.random_seed,
            "auto_predict_enabled": deployment.auto_predict_enabled,
            "auto_predict_time": deployment.auto_predict_time.strftime("%H:%M:%S") if deployment.auto_predict_time else None,
            "last_auto_predicted_for": deployment.last_auto_predicted_for,
            "next_run_at": next_run_at,
            "schedule_status": "enabled" if deployment.auto_predict_enabled else "disabled",
            "today_prediction_done": bool(existing_today and existing_today.status == "done"),
            "today_prediction_in_progress": bool(existing_today and existing_today.status in {"pending", "training"}),
            "today_prediction_run_id": str(existing_today.id) if existing_today else None,
            "source_experiment_id": str(deployment.source_experiment_id) if deployment.source_experiment_id else None,
            "source_run_id": str(deployment.source_run_id) if deployment.source_run_id else None,
            "model_arch": {
                "id": str(deployment.model_arch_id),
                "display_name": deployment.model_arch.display_name,
                "arch": deployment.model_arch.arch,
            },
            "latest_run": _serialize_live_run_brief(latest_run),
            "runs": [
                {
                    "id": str(run.id),
                    "status": run.status,
                    "prediction_date": run.prediction_date,
                    "target_date": run.target_date,
                    "signal": run.signal,
                    "confidence": run.confidence,
                    "prob_long": run.prob_long,
                    "prob_short": run.prob_short,
                    "prob_neutral": run.prob_neutral,
                    "training_window_start": run.training_window_start,
                    "training_window_end": run.training_window_end,
                    "created_at": run.created_at,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                } for run in runs
            ],
            "created_at": deployment.created_at,
            "updated_at": deployment.updated_at,
        })

    def patch(self, request, deployment_id):
        from apps.ml_engine.models import LiveDeployment

        deployment = LiveDeployment.objects.get(id=deployment_id, user=request.user)
        for field in (
            "name", "description", "ticker", "benchmark",
            "date_start", "date_end", "random_seed", "status",
            "auto_predict_enabled",
        ):
            if field in request.data:
                setattr(deployment, field, request.data[field])
        if "auto_predict_time" in request.data:
            deployment.auto_predict_time = _parse_time_string(request.data["auto_predict_time"])
        deployment.save()
        return Response({"id": str(deployment.id)})

    def delete(self, request, deployment_id):
        from apps.ml_engine.models import LiveDeployment

        deployment = LiveDeployment.objects.filter(id=deployment_id, user=request.user).first()
        if not deployment:
            return Response({"error": "Live deployment not found"}, status=404)
        with transaction.atomic():
            _delete_live_deployment_records(deployment)
        return Response(status=204)


class LaunchLiveRunView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, deployment_id):
        from apps.ml_engine.models import LiveDeployment, LiveRun
        from apps.ml_engine.tasks import run_live_prediction

        deployment = LiveDeployment.objects.get(id=deployment_id, user=request.user)
        existing_today = _find_existing_live_cycle_run(deployment)
        if existing_today:
            return Response({
                "error": "Today prediction already exists for this deployment.",
                "live_run_id": str(existing_today.id),
                "status": existing_today.status,
            }, status=409)

        if "date_start" in request.data:
            deployment.date_start = request.data["date_start"]
        deployment.date_end = request.data.get("date_end", _yesterday())
        deployment.status = "queued"
        deployment.save(update_fields=["date_start", "date_end", "status", "updated_at"])

        live_run = LiveRun.objects.create(deployment=deployment, status="pending")
        task = run_live_prediction.apply_async(args=[str(live_run.id)], queue="training")
        live_run.celery_task_id = task.id
        live_run.save(update_fields=["celery_task_id"])
        return Response({"live_run_id": str(live_run.id), "task_id": task.id}, status=202)


class LiveRunStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, live_run_id):
        from apps.ml_engine.models import LiveRun

        run = LiveRun.objects.select_related("deployment", "deployment__model_arch").get(
            id=live_run_id,
            deployment__user=request.user,
        )
        return Response({
            "id": str(run.id),
            "status": run.status,
            "epochs_done": run.epochs_done,
            "loss_history": run.loss_history,
            "error_msg": run.error_msg,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "celery_task_id": run.celery_task_id,
            "train_size": run.train_size,
            "training_window_start": run.training_window_start,
            "training_window_end": run.training_window_end,
            "deployment": {
                "id": str(run.deployment_id),
                "name": run.deployment.name,
                "status": run.deployment.status,
            },
            "model_arch": {
                "id": str(run.deployment.model_arch_id),
                "display_name": run.deployment.model_arch.display_name,
                "arch": run.deployment.model_arch.arch,
            },
            "server_time": timezone.now(),
        })


class LivePredictionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, live_run_id):
        from apps.ml_engine.models import LiveRun

        run = LiveRun.objects.get(id=live_run_id, deployment__user=request.user)
        if run.status != "done" or not run.signal:
            return Response({"status": "not_ready"}, status=202)
        return Response({
            "signal": run.signal,
            "prob_long": run.prob_long,
            "prob_short": run.prob_short,
            "prob_neutral": run.prob_neutral,
            "confidence": run.confidence,
            "directional_edge": round(float((run.prob_long or 0.0) - (run.prob_short or 0.0)), 4),
            "rsi_14": run.rsi_14,
            "vol_ann": run.vol_ann,
            "stop_loss_pct": run.stop_loss_pct,
            "target_pct": run.target_pct,
            "prediction_date": str(run.prediction_date),
            "target_date": str(run.target_date),
            "training_window_start": str(run.training_window_start),
            "training_window_end": str(run.training_window_end),
            "headline": (
                f"基於 {run.training_window_start} 到 {run.training_window_end} 的數據，"
                f"明日模型操作建議為：{run.signal}"
            ),
        })


class LiveFeedbackView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, deployment_id):
        from apps.ml_engine.models import LivePredictionFeedback, LiveDeployment

        deployment = LiveDeployment.objects.get(id=deployment_id, user=request.user)
        refresh = request.query_params.get("refresh", "1") != "0"
        if refresh:
            _evaluate_feedback_for_deployment(deployment)
        rows = list(
            LivePredictionFeedback.objects.filter(deployment=deployment).order_by("target_date").values(
                "prediction_date",
                "target_date",
                "predicted_signal",
                "actual_return",
                "predicted_return",
                "realized_pnl",
                "was_correct",
                "hit_rate",
                "cumulative_pnl",
                "alpha_drift",
            )
        )
        latest_run = deployment.runs.filter(status="done").order_by("-target_date", "-created_at").first()
        summary = {
            "count": len(rows),
            "latest_hit_rate": rows[-1]["hit_rate"] if rows else 0.0,
            "latest_cumulative_pnl": rows[-1]["cumulative_pnl"] if rows else 0.0,
            "latest_alpha_drift": rows[-1]["alpha_drift"] if rows else 0.0,
            "latest_target_date": rows[-1]["target_date"] if rows else None,
            "latest_prediction_date": rows[-1]["prediction_date"] if rows else None,
            "feedback_ready": bool(rows),
            "pending_reason": None,
        }
        if not rows:
            if not latest_run:
                summary["pending_reason"] = "No completed live run yet."
            elif latest_run.target_date and latest_run.target_date > _today():
                summary["pending_reason"] = "Latest target date has not arrived yet."
            else:
                summary["pending_reason"] = "Actual OHLCV for latest target date is not available yet."
        return Response({"summary": summary, "items": rows})


class HealthCheckView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        from django.core.cache import cache

        checks = {}
        try:
            connections["default"].cursor().execute("SELECT 1")
            checks["postgres"] = "ok"
        except Exception as exc:
            checks["postgres"] = str(exc)
        try:
            cache.set("hc", "1", 5)
            checks["redis"] = "ok"
        except Exception as exc:
            checks["redis"] = str(exc)
        code = 200 if all(value == "ok" for value in checks.values()) else 503
        return Response({"status": "ok" if code == 200 else "degraded", "checks": checks}, status=code)
