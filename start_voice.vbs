' Start voice monitoring (recorder + live ledger refresh), no window.
' Double-click this file. Safe to double-click twice: both have single-instance locks.
' Location-independent: works from any folder, on any machine.
Option Explicit
Dim fso, sh, root, pyw
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = root & "\python\pythonw.exe"               ' install package layout
If Not fso.FileExists(pyw) Then
    pyw = root & "\.venv\Scripts\pythonw.exe"    ' dev machine layout
End If
If Not fso.FileExists(pyw) Then
    MsgBox "Python runtime not found:" & vbCrLf & pyw, 16, "Voice Recorder"
    WScript.Quit 1
End If
sh.CurrentDirectory = root
sh.Run """" & pyw & """ """ & root & "\voice_daemon.py""", 0, False
' Ledger keeps itself current: recompute this boot every 5 minutes,
' so the page never lags behind to the previous shutdown.
sh.Run """" & pyw & """ """ & root & "\vlive.py""", 0, False
