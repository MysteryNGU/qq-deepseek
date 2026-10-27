@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo 正在导出配置、记忆和 skills，不包含源码与运行环境。
echo 压缩包包含真实密钥，不要公开分享。模型和运行依赖另行迁移。
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -X utf8 "scripts\export_private_bundle.py" --root "%CD%" %*
) else (
    python -X utf8 "scripts\export_private_bundle.py" --root "%CD%" %*
)
set "export_result=%ERRORLEVEL%"
if not "%export_result%"=="0" echo 导出失败，请查看上面的原因；不要使用 .partial 文件。
if "%export_result%"=="0" echo 已导出，默认不做额外校验。输出在项目 backups 文件夹。
pause
exit /b %export_result%
