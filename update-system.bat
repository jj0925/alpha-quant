@echo off
echo ===================================================
echo [AlphaQuant] 正在從 GitHub 拉取最新官方進度...
echo ===================================================
git pull origin main

echo.
echo ===================================================
echo [AlphaQuant] 正在重啟容器並照著「官方藍圖」更新資料庫...
echo ===================================================
docker compose up -d --build
docker compose exec backend python manage.py migrate

echo.
echo ===================================================
echo [AlphaQuant] 更新完成！系統已同步至最新版本！
echo ===================================================
pause