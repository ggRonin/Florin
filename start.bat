@echo off
chcp 65001 >nul
cd /d "%~dp0"
:loop
echo [%date% %time%] запуск бота
python main.py run
echo [%date% %time%] бот остановился (код %errorlevel%), перезапуск через 30 сек... Ctrl+C чтобы выйти
timeout /t 30 /nobreak >nul
goto loop
