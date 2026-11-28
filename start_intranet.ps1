$ErrorActionPreference = "Stop"

Write-Host "ClassInk Intranet - démarrage sécurisé" -ForegroundColor Cyan
Write-Host "Saisissez la clé dédiée INTRANET_AUTH_SECRET configurée sur Render."
Write-Host "Cette clé n'est pas enregistrée dans un fichier sur cet ordinateur."
Write-Host ""

$secureKey = Read-Host "Clé intranet" -AsSecureString
if ($secureKey.Length -lt 32) {
    throw "La clé intranet est trop courte ou vide."
}

$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $env:INTRANET_AUTH_SECRET = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    $exePath = Join-Path $PSScriptRoot "ClassInkIntranet.exe"
    if (-not (Test-Path $exePath)) {
        throw "ClassInkIntranet.exe est introuvable dans ce dossier."
    }

    Write-Host ""
    Write-Host "Passerelle active. Gardez cette fenêtre ouverte." -ForegroundColor Green
    Write-Host "Port local : 5001"
    Write-Host "Contrôle : http://127.0.0.1:5001/healthz"
    Write-Host ""
    & $exePath
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Le serveur s'est arrêté avec le code $LASTEXITCODE."
    }
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    Remove-Item Env:INTRANET_AUTH_SECRET -ErrorAction SilentlyContinue
    $secureKey.Dispose()
}
