Option Explicit

Dim shell, fso, scriptDir, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File " & _
  Chr(34) & scriptDir & "\tdx_gateway_memory_watchdog.ps1" & Chr(34) & _
  " -ThresholdGB 16 -Port 8765 -IntervalSeconds 30"

shell.Run command, 0, False
