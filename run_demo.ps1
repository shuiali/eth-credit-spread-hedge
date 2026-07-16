[CmdletBinding()]
param(
    [ValidateRange(1, 1000)]
    [int]$LevelCount = 20,
    [ValidateRange(1, 86400)]
    [int]$RunSeconds = 3600,
    [string]$ShortSymbol = "ETH-31JUL26-1950-P-USDT",
    [string]$LongSymbol = "ETH-31JUL26-1850-P-USDT",
    [decimal]$OptionQuantity = 0.1,
    [decimal]$MinimumNetCredit = 0.01,
    [decimal]$MaximumEntryDeviationBps = 100
)

$ErrorActionPreference = "Stop"
$env:ETH_HEDGE_ENVIRONMENT = "DEMO"
$env:ETH_HEDGE_LEVEL_COUNT = $LevelCount.ToString()
$env:ETH_HEDGE_STOP_MODE = "ENTRY_PERCENT"
$env:ETH_HEDGE_ENTRY_STOP_RATE = "0.0015"
$env:ETH_HEDGE_RECOVERY_MODE = "FULL_NEXT_TP"
$env:ETH_HEDGE_LOCK_POLICY = "UNHEDGED"
$env:ETH_HEDGE_TRIGGER_PRICE_SOURCE = "LAST_TRADE"
$env:RUN_BYBIT_DEMO_MUTATIONS = "FULL_STRATEGY_DEMO"

try {
    & python -m dotenv run --no-override -- python -m eth_credit_hedge.interfaces.demo_strategy_runner run `
        --cycle-mode OPEN_NEW `
        --short-symbol $ShortSymbol `
        --long-symbol $LongSymbol `
        --option-quantity $OptionQuantity `
        --min-net-credit $MinimumNetCredit `
        --max-entry-deviation-bps $MaximumEntryDeviationBps `
        --run-seconds $RunSeconds `
        --shutdown-policy CLOSE_ALL

    if ($LASTEXITCODE -ne 0) {
        throw "The integrated demo strategy exited with code $LASTEXITCODE."
    }
}
finally {
    Remove-Item Env:RUN_BYBIT_DEMO_MUTATIONS -ErrorAction SilentlyContinue
}
