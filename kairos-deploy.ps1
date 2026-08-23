<#
  kairos-deploy.ps1 — deploiement / redemarrage propre du bot Kairos.

  Usage (depuis le dossier du bot) :
    .\kairos-deploy.ps1            # redemarre le bot (apres edition config/.env)
    .\kairos-deploy.ps1 -Merge     # merge la branche de dev PUIS redemarre

  Ce que ca fait, de facon fiable :
    - (optionnel) git merge --ff-only de la branche de dev
    - tue TOUS les process du bot, en gerant le cas "Acces refuse" (re-essaie)
    - refuse de demarrer si un orphelin survit (evite la double instance = double ordre)
    - demarre + VERIFIE : instance unique + dashboard qui repond
#>
param([switch]$Merge)

Set-Location "C:\Users\Brice Cuny\OneDrive\Bureau\bots"
$branch = "claude/bot-trading-memory-97306b"

function Get-BotProcs {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match 'run_with_restart|main\.py' }
}

# 1. Merge optionnel du code de dev
if ($Merge) {
    Write-Host "-> Merge de $branch ..." -ForegroundColor Cyan
    git merge --ff-only $branch
}

# 2. Arret fiable de tous les process bot (boucle anti "acces refuse")
Write-Host "-> Arret du bot..." -ForegroundColor Cyan
Stop-ScheduledTask -TaskName KairosBot -ErrorAction SilentlyContinue
$deadline = (Get-Date).AddSeconds(15)
while ((Get-BotProcs) -and (Get-Date) -lt $deadline) {
    foreach ($p in Get-BotProcs) {
        try   { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop }
        catch { }   # acces refuse / deja mort : on retente au tour suivant
    }
    Start-Sleep -Milliseconds 800
}
if (Get-BotProcs) {
    $ids = (Get-BotProcs).ProcessId -join ', '
    Write-Host "X Des process bot survivent (PID: $ids). NE PAS demarrer (risque de doublon)." -ForegroundColor Red
    Write-Host "  Relance le script, ou tue-les a la main (Gestionnaire des taches), puis reessaie." -ForegroundColor Red
    return
}
Write-Host "  OK, tous les process sont arretes." -ForegroundColor Green

# 3. Demarrage
Write-Host "-> Demarrage..." -ForegroundColor Cyan
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName KairosBot
Start-Sleep -Seconds 9

# 4. Verification : instance unique (1 tree = 2 main.py) + dashboard
$nMain = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
           Where-Object { $_.CommandLine -match 'main\.py' }).Count
Write-Host ""
if ($nMain -eq 0) {
    Write-Host "X Aucun main.py — le bot n'a pas demarre. Regarde C:\Kairos\logs\trading.log" -ForegroundColor Red
} elseif ($nMain -gt 2) {
    Write-Host "X $nMain process main.py -> DOUBLE INSTANCE probable ! Relance le script." -ForegroundColor Red
} else {
    try {
        $pf = Invoke-RestMethod "http://localhost:8080/api/portfolio" -TimeoutSec 6
        $v  = [math]::Round($pf.total, 2)
        Write-Host "OK - Bot demarre, instance unique. Portefeuille : `$$v" -ForegroundColor Green
        Write-Host "   (fais Ctrl+F5 sur le dashboard pour voir la derniere version)" -ForegroundColor DarkGray
    } catch {
        Write-Host "OK - Process demarre ($nMain main.py). Dashboard pas encore pret : attends ~10s + Ctrl+F5." -ForegroundColor Yellow
    }
}
