Set WshShell = WScript.CreateObject("WScript.Shell")
Set Shortcut = WshShell.CreateShortcut(WScript.Arguments(0))
Shortcut.TargetPath = WScript.Arguments(1)
Shortcut.Arguments = WScript.Arguments(2)
Shortcut.WorkingDirectory = WScript.Arguments(3)
Shortcut.Description = WScript.Arguments(4)
Shortcut.IconLocation = WScript.Arguments(5)
Shortcut.Save
