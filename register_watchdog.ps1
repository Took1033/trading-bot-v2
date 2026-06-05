# register_watchdog.ps1 — enregistre la tache planifiee HORAIRE du watchdog Kairos.
#
# A lancer UNE FOIS, depuis le dossier du bot (apres merge sur main) :
#     powershell -ExecutionPolicy Bypass -File register_watchdog.ps1
#
# Pour retirer la tache :
#     schtasks /Delete /TN "KairosWatchdog" /F
#
# La tache appelle  python watchdog.py  toutes les heures. watchdog.py est
# self-locating (il se place dans son propre dossier), donc aucun working-dir
# special n'est requis.

$ErrorActionPreference = "Stop"

$dir    = $PSScriptRoot
$script = Join-Path $dir "watchdog.py"

# python : PATH d'abord, sinon emplacement connu.
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = "C:\Users\Brice Cuny\AppData\Local\Programs\Python\Python311\python.exe" }

if (-not (Test-Path $script)) { throw "watchdog.py introuvable dans $dir" }
if (-not (Test-Path $py))     { throw "python introuvable : $py" }

$action  = New-ScheduledTaskAction -Execute $py -Argument "`"$script`""

# Declencheur : une fois, puis repetition toutes les heures, indefiniment.
# (assignation de .Repetition = recette fiable sur Windows PowerShell 5.1)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date)
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)).Repetition

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable

Register-ScheduledTask -TaskName "KairosWatchdog" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Surveillance externe horaire de Kairos Alpha (Telegram si anomalie)." `
    -Force | Out-Null

Write-Host "Tache 'KairosWatchdog' enregistree (horaire)."
Write-Host "Test immediat (sans envoyer) :  python `"$script`" --dry-run"
Write-Host "Retirer la tache           :  schtasks /Delete /TN KairosWatchdog /F"
