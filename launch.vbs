' X Bookmarks launcher: start the local server if needed, then open the app window.
' Interpreter resolution: .venv\Scripts\pythonw.exe  ->  python.txt (one line)  ->  pythonw.exe on PATH
Dim sh, fso, base, py, f, ts
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base

py = fso.BuildPath(base, ".venv\Scripts\pythonw.exe")
If Not fso.FileExists(py) Then
  py = "pythonw.exe"
  If fso.FileExists(fso.BuildPath(base, "python.txt")) Then
    Set f = fso.OpenTextFile(fso.BuildPath(base, "python.txt"), 1)
    ts = Trim(f.ReadLine())
    f.Close
    If ts <> "" Then py = ts
  End If
End If

sh.Run """" & py & """ """ & fso.BuildPath(base, "serve.py") & """ --open", 0, False
