' Starts Memoria in the background with no window, under a watchdog that
' restarts it if it crashes. Messages are written to memory_store\server.log
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = folder
shell.Run """" & folder & "\.venv\Scripts\pythonw.exe"" """ & folder & "\keep_memoria_running.py""", 0, False
