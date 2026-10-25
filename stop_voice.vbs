' Stop voice recording gracefully.
' Writes data\STOP; the daemon and its children flush the current WAV and exit.
' Location-independent: works from any folder, on any machine.
Option Explicit
Dim fso, root, dataDir, f
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
dataDir = root & "\data"
If Not fso.FolderExists(dataDir) Then fso.CreateFolder(dataDir)
Set f = fso.CreateTextFile(dataDir & "\STOP", True)
f.Write Now
f.Close
MsgBox "Recording stopped." & vbCrLf & "The current clip has been saved.", _
       64, "Voice Recorder"
