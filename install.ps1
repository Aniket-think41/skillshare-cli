#!/usr/bin/env pwsh
param(
    [string]$Version = "latest"
)

$Repo = "Aniket-think41/skillshare-cli"

if ($Version -eq "latest") {
    $Url = "https://github.com/$Repo/releases/latest/download/skillshare-windows-amd64.exe"
} else {
    $Url = "https://github.com/$Repo/releases/download/$Version/skillshare-windows-amd64.exe"
}

$Out = "$env:TEMP\skillshare.exe"
Write-Host "Downloading skillshare for Windows..."
Invoke-WebRequest -Uri $Url -OutFile $Out

# Install to a directory in PATH
$InstallDir = "$env:USERPROFILE\.skillshare\bin"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Move-Item -Force $Out "$InstallDir\skillshare.exe"

# Add to PATH if not already there
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$InstallDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$UserPath;$InstallDir", "User")
    $env:Path += ";$InstallDir"
}

Write-Host "Installed skillshare to $InstallDir\skillshare.exe"
Write-Host "You may need to restart your terminal for PATH changes to take effect."
