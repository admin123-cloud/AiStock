$log='F:\AiStock\app\scripts\research\wstock_export_monitor.log'
$last=0; $stable=0
for($i=0;$i -lt 240;$i++){
  Start-Sleep -Seconds 30
  $f=@(Get-ChildItem 'E:\AStockHistory\wstock\输出\轮转暂存' -Recurse -Filter *.csv -ErrorAction SilentlyContinue)
  $n=$f.Count; $bytes=($f|Measure-Object Length -Sum).Sum
  "$(Get-Date -Format HH:mm:ss) files=$n bytes=$bytes" | Out-File $log -Append
  if($n -eq $last){$stable++}else{$stable=0}
  $last=$n
  if($stable -ge 6){ "STABLE at files=$n" | Out-File $log -Append; break }
}
