@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

:: FastAPI 服务管理脚本
:: 用法: fastapi_service.bat [start|stop|restart|status]

set "SERVICE_NAME=fastapi"
set "PORT=8100"
set "PROJECT_DIR=%~dp0.."
set "PID_FILE=%PROJECT_DIR%\fastapi.pid"

:: ============================================
:: 虚拟环境配置（根据实际情况修改）
:: ============================================
set "VENV_PYTHON=%PROJECT_DIR%\.venv\Scripts\python.exe"

:: 如果虚拟环境不存在，使用系统 Python
if not exist "%VENV_PYTHON%" (
    set "VENV_PYTHON=python"
)
:: ============================================

:: 切换到项目目录
cd /d "%PROJECT_DIR%"

if "%1"=="" goto start
if /i "%1"=="start" goto start
if /i "%1"=="stop" goto stop
if /i "%1"=="restart" goto restart
if /i "%1"=="status" goto status
goto usage

:start
call :is_running
if !errorlevel! equ 0 (
    echo [WARN] 服务已在运行中
    goto :eof
)
echo [INFO] 正在启动 FastAPI 服务...
if not exist "%PROJECT_DIR%\logs" mkdir "%PROJECT_DIR%\logs"

start /b "%VENV_PYTHON%" -m uvicorn app.main:app --host 0.0.0.0 --port %PORT% --reload > logs\fastapi.log 2>&1

:: 等待服务启动
timeout /t 3 /nobreak >nul

:: 获取Python进程PID
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
    echo %%a > "%PID_FILE%"
    echo [INFO] 服务启动成功 ^(PID: %%a, Port: %PORT%^)
    goto :eof
)

echo [ERROR] 服务启动失败，请查看日志: logs\fastapi.log
goto :eof

:stop
call :is_running
if !errorlevel! neq 0 (
    echo [WARN] 服务未运行
    if exist "%PID_FILE%" del "%PID_FILE%"
    goto :eof
)

for /f "tokens=2" %%a in ('findstr /C:"PID_FILE" "%PROJECT_DIR%\.git\config" 2^>nul') do set "SAVED_PID=%%a"
if exist "%PID_FILE%" (
    set /p SAVED_PID=<"%PID_FILE%"
)
if defined SAVED_PID (
    echo [INFO] 正在停止服务 ^(PID: %SAVED_PID%^)
    taskkill /F /PID %SAVED_PID% >nul 2>&1
)

:: 强制终止可能残留的uvicorn进程
taskkill /F /IM python.exe /FI "WINDOWTITLE eq *uvicorn*" >nul 2>&1

if exist "%PID_FILE%" del "%PID_FILE%"
echo [INFO] 服务已停止
goto :eof

:restart
echo [INFO] 重启服务...
call :stop
timeout /t 2 /nobreak >nul
call :start
goto :eof

:status
call :is_running
if !errorlevel! equ 0 (
    if exist "%PID_FILE%" (
        set /p PID=<"%PID_FILE%"
        echo [INFO] 服务运行中 ^(PID: %PID%, Port: %PORT%^
    ) else (
        echo [INFO] 服务运行中 ^(Port: %PORT%^
    )
) else (
    echo [WARN] 服务未运行
)
goto :eof

:is_running
if exist "%PID_FILE%" (
    set /p PID=<"%PID_FILE%"
    tasklist /FI "PID eq %PID%" | findstr /i "python.exe" >nul
    if !errorlevel! equ 0 exit /b 0
)
exit /b 1

:usage
echo 用法: fastapi_service.bat [start^|stop^|restart^|status]
echo.
echo   start   - 启动服务
echo   stop    - 停止服务
echo   restart - 重启服务
echo   status  - 查看服务状态
exit /b 1
