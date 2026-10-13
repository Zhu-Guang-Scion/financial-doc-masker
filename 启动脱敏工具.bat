@echo off
chcp 65001 >nul
title 财税文档脱敏工具（本地离线版）
cd /d "%~dp0"

set PY=D:\Anaconda3\python.exe
if not exist "%PY%" set PY=python

"%PY%" main.py
if errorlevel 1 (
    echo.
    echo 启动失败，请把上方错误信息截图反馈。
    pause
)
