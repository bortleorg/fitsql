# Start fitsql (backend + frontend dev server)
$root = $PSScriptRoot

Write-Host "Starting backend..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root\backend'; .\venv\Scripts\activate; uvicorn main:app --reload --port 8000" -WindowStyle Normal

Start-Sleep -Seconds 2

Write-Host "Starting frontend..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root\frontend'; npm run dev" -WindowStyle Normal

Write-Host ""
Write-Host "Backend:  http://localhost:8000" -ForegroundColor Green
Write-Host "Frontend: http://localhost:5173" -ForegroundColor Green
Write-Host "API docs: http://localhost:8000/docs" -ForegroundColor Green
