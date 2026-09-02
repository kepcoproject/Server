@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ============================================
echo   스마트 에너지 백엔드 서버
echo ============================================
echo.

where python > nul 2>&1
if errorlevel 1 (
    echo [오류] Python이 설치되어 있지 않습니다.
    echo        https://www.python.org/downloads/ 에서 설치한 뒤 다시 실행하세요.
    echo        설치할 때 "Add Python to PATH" 체크를 꼭 하세요.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo [1/4] 가상환경을 만드는 중... 처음 한 번만 실행됩니다.
    python -m venv .venv
    if errorlevel 1 (
        echo [오류] 가상환경 생성에 실패했습니다.
        pause
        exit /b 1
    )
)

echo [2/4] 필요한 패키지를 설치하는 중...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo [오류] 패키지 설치에 실패했습니다. 인터넷 연결을 확인하세요.
    pause
    exit /b 1
)

if /I "%~1"=="--demo" (
    echo [3/4] 더미 데이터를 새로 만드는 중... [기존 데이터를 모두 지웁니다]
    ".venv\Scripts\python.exe" scripts\seed_demo_data.py --reset
) else (
    echo [3/4] 기존 데이터를 그대로 둡니다.
    echo        화면 개발용 더미 데이터가 필요하면  start-backend.bat --demo  로 실행하세요.
)

echo [4/4] 서버를 시작합니다.
echo.
echo     API 문서   http://localhost:8000/docs
echo     데이터     http://localhost:8000/api/data/latest
echo.
echo     더미 데이터로 새로 시작   start-backend.bat --demo
echo.
echo     종료하려면 Ctrl+C 를 누르세요.
echo ============================================
echo.

".venv\Scripts\python.exe" -m uvicorn app.main:app --reload --port 8000
pause
