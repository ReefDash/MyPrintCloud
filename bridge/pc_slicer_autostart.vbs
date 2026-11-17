' Optional: run a slicing-only worker on a Windows PC, hidden (put a shortcut to this in shell:startup).
' Use this when another device (e.g. an Android tablet) is the printer link. Never run two printer links at once.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
d = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = d
sh.Run "cmd /c python -u bridge.py --role slicer --name pc >> pcslicer.out.log 2>> pcslicer.log", 0, False
