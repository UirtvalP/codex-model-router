[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Prompt,

    [switch]$RouteOnly,
    [switch]$HeuristicOnly,

    [ValidateSet("read-only", "workspace-write", "danger-full-access")]
    [string]$Sandbox,

    [string]$WorkingDirectory = (Get-Location).Path,
    [string]$Resume,
    [switch]$ResumeLast,
    [switch]$JsonEvents,
    [switch]$NoLog
)

$routerScript = Join-Path $PSScriptRoot "tools\codex_route.py"
$pythonCandidates = @(
    (Join-Path $PSScriptRoot ".venv\Scripts\python.exe"),
    (Join-Path $PSScriptRoot "venv\Scripts\python.exe")
)
$routerArguments = @($routerScript, "--cd", $WorkingDirectory)

if ($RouteOnly) {
    $routerArguments += "--route-only"
}
if ($HeuristicOnly) {
    $routerArguments += "--heuristic-only"
}
if ($Sandbox) {
    $routerArguments += @("--sandbox", $Sandbox)
}
if ($Resume) {
    $routerArguments += @("--resume", $Resume)
}
if ($ResumeLast) {
    $routerArguments += "--resume-last"
}
if ($JsonEvents) {
    $routerArguments += "--json-events"
}
if ($NoLog) {
    $routerArguments += "--no-log"
}
if ($Prompt) {
    $routerArguments += @("--prompt", $Prompt)
}

$venvPython = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if ($venvPython) {
    & $venvPython @routerArguments
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 @routerArguments
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python @routerArguments
} else {
    Write-Error "Python 3.9 or newer was not found."
    exit 127
}

exit $LASTEXITCODE
