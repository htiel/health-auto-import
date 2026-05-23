# HAE TCP server - definitive query for HRN + Workouts + Health Metrics
# Known-good envelope: method=callTool, params={name, arguments}
# Server version v0.0.1 (legacy), per probe results.
#
# Usage:  pwsh -File query.ps1
# Target: 192.168.1.203:9000  (edit below if different)

function Send-Once($json) {
  $c = New-Object System.Net.Sockets.TcpClient
  try { $c.Connect('192.168.1.203', 9000) } catch { return "CONNECT_FAIL: $($_.Exception.Message)" }
  $s = $c.GetStream(); $s.ReadTimeout = 15000
  $b = [Text.Encoding]::UTF8.GetBytes($json + "`n")
  $s.Write($b,0,$b.Length); $s.Flush()
  $sb  = New-Object System.Text.StringBuilder
  $buf = New-Object byte[] 32768
  while ($true) {
    try {
      $n = $s.Read($buf, 0, $buf.Length)
      if ($n -le 0) { break }
      [void]$sb.Append([Text.Encoding]::UTF8.GetString($buf, 0, $n))
      $cur = $sb.ToString().Trim()
      if ($cur.Length -gt 0) {
        try { [void]($cur | ConvertFrom-Json -ErrorAction Stop); break } catch { }
      }
    } catch { break }
  }
  $c.Close()
  return $sb.ToString()
}

function Call($name, $argsHash) {
  $body = @{ jsonrpc='2.0'; id=[string](Get-Random); method='callTool'; params=@{ name=$name; arguments=$argsHash } }
  $json = $body | ConvertTo-Json -Compress -Depth 10
  Write-Host "REQ: $json"
  Send-Once $json
}

$now    = Get-Date
$endTs  = $now.ToString('yyyy-MM-dd HH:mm:ss ') + $now.ToString('zzz').Replace(':','')
$start  = $now.AddDays(-30)
$startTs= $start.ToString('yyyy-MM-dd HH:mm:ss ') + $start.ToString('zzz').Replace(':','')
"window: $startTs  ->  $endTs"
""

function Summarize($label, $resp) {
  "==================================================================="
  $label
  "==================================================================="
  "raw length: $($resp.Length)"
  try {
    $j = $resp | ConvertFrom-Json -ErrorAction Stop
    if ($j.error) { "ERROR code=$($j.error.code) msg=$($j.error.message)"; return }
    $rj = $j.result | ConvertTo-Json -Depth 6
    "result preview:"
    $rj.Substring(0, [Math]::Min(1800, $rj.Length))
    foreach ($k in 'ecg','heartRateNotifications','workouts','data') {
      if ($j.result.$k -ne $null) {
        $cnt = @($j.result.$k).Count
        "[count $k = $cnt]"
      }
    }
    if ($j.result.content) {
      $text = $j.result.content[0].text
      if ($text) {
        "[MCP content.text length=$($text.Length)]"
        try {
          $inner = $text | ConvertFrom-Json -ErrorAction Stop
          foreach ($k in 'ecg','heartRateNotifications','workouts','data','metrics') {
            if ($inner.$k -ne $null) { "[inner count $k = $(@($inner.$k).Count)]" }
          }
        } catch { }
      }
    }
  } catch {
    "Not parseable as JSON. First 400 chars:"
    $resp.Substring(0, [Math]::Min(400, $resp.Length))
  }
  ""
}

# 0) ECG control test
Summarize 'ECG (30d, control)' (Call 'ecg' @{ start=$startTs; end=$endTs })

# 1) Heart Rate Notifications - 30 days
Summarize 'HEART NOTIFICATIONS (30d)' (Call 'heart_notifications' @{ start=$startTs; end=$endTs })

# 2) Workouts - 30 days, no metadata/routes (keep small)
Summarize 'WORKOUTS (30d, minimal)' (Call 'workouts' @{ start=$startTs; end=$endTs; includeMetadata=$false; includeRoutes=$false })

# 3) Health Metrics sample - active energy & step count last 7d, aggregated daily
$start7  = $now.AddDays(-7)
$start7Ts= $start7.ToString('yyyy-MM-dd HH:mm:ss ') + $start7.ToString('zzz').Replace(':','')
Summarize 'HEALTH_METRICS (7d, step+energy)' (Call 'health_metrics' @{ start=$start7Ts; end=$endTs; metrics='step_count,active_energy'; interval='days'; aggregate=$true })
