"""config/urls.py"""
from django.contrib import admin
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.users.views import RegisterView, MeView
from apps.market_data.views import FeatureDefinitionViewSet, FeaturePresetViewSet, OHLCVView
from apps.ml_engine.views import (
    ModelRegistryView, ExperimentViewSet, LaunchTrainingView,
    TrainingRunStatusView, BacktestResultView, PredictionView, HealthCheckView,
    LiveDeploymentListCreateView, LiveDeploymentDetailView, LaunchLiveRunView,
    LiveRunStatusView, LivePredictionView, LiveFeedbackView, RetrainExperimentView,
    RunSourceDataExportView,
)
from apps.backtest.views import WalkForwardView, MonteCarloView, ComparisonView

router = DefaultRouter()
router.register("experiments",     ExperimentViewSet,    basename="experiment")
router.register("feature-presets", FeaturePresetViewSet, basename="preset")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/register/",      RegisterView.as_view()),
    path("api/auth/token/",         TokenObtainPairView.as_view()),
    path("api/auth/token/refresh/", TokenRefreshView.as_view()),
    path("api/auth/me/",            MeView.as_view()),
    path("api/features/",           FeatureDefinitionViewSet.as_view({"get": "list"})),
    path("api/market/ohlcv/",       OHLCVView.as_view()),
    path("api/models/",             ModelRegistryView.as_view()),
    path("api/",                    include(router.urls)),
    path("api/experiments/<uuid:exp_id>/train/",   LaunchTrainingView.as_view()),
    path("api/experiments/<uuid:exp_id>/retrain/", RetrainExperimentView.as_view()),
    path("api/experiments/<uuid:exp_id>/compare/", ComparisonView.as_view()),
    path("api/runs/<uuid:run_id>/status/",        TrainingRunStatusView.as_view()),
    path("api/runs/<uuid:run_id>/backtest/",      BacktestResultView.as_view()),
    path("api/runs/<uuid:run_id>/source-data/",   RunSourceDataExportView.as_view()),
    path("api/runs/<uuid:run_id>/prediction/",    PredictionView.as_view()),
    path("api/live-deployments/", LiveDeploymentListCreateView.as_view()),
    path("api/live-deployments/<uuid:deployment_id>/", LiveDeploymentDetailView.as_view()),
    path("api/live-deployments/<uuid:deployment_id>/run/", LaunchLiveRunView.as_view()),
    path("api/live-deployments/<uuid:deployment_id>/feedback/", LiveFeedbackView.as_view()),
    path("api/live-runs/<uuid:live_run_id>/status/", LiveRunStatusView.as_view()),
    path("api/live-runs/<uuid:live_run_id>/prediction/", LivePredictionView.as_view()),
    path("api/runs/<uuid:run_id>/walk-forward/",  WalkForwardView.as_view()),
    path("api/runs/<uuid:run_id>/monte-carlo/",   MonteCarloView.as_view()),
    path("api/health/",   HealthCheckView.as_view()),
    path("api/schema/",   SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/",     SpectacularSwaggerView.as_view(url_name="schema")),
]
