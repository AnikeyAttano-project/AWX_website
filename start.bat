@echo off
echo Starting AWX-WEB-lite Backend...

cd backend

if not exist "venv\" (
    echo Creating virtual environment...
    python -m venv venv
)

call venv\Scripts\activate

echo Installing dependencies...
pip install -r requirements.txt

echo.
echo Starting server on http://localhost:8000
echo Frontend: Open frontend\index.html in browser
echo.

python main.py
