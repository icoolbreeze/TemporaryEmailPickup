Option Explicit

' 隐藏 Python 的控制台窗口，并沿用 start.bat 原有的 Python 解析方式。
Dim shell, fileSystem, projectDir, command
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

projectDir = fileSystem.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = projectDir
command = "python.exe " & Chr(34) & projectDir & "\\app.py" & Chr(34)
shell.Run command, 0, False
