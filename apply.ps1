# Uso:
#   .\apply.ps1 <job_url>          - candidata-se a 1 vaga
#   .\apply.ps1                    - roda a fila em data\jobs.txt (uma URL por linha)
# Equivalente Windows do apply.sh (que usa `source .env` do Linux).
param([string]$JobUrl)
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
Get-Content -LiteralPath ".env" | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
        [System.Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim(), 'Process')
    }
}
if ($JobUrl) {
    .\.venv\Scripts\python.exe -u main.py $JobUrl
} else {
    .\.venv\Scripts\python.exe -u main.py --queue data\jobs.txt
}
