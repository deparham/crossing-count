-- CrossingCount for the Mac: double-click to open it. It starts the app if it is not
-- running yet (the app then opens itself in the browser), or opens the page if it is.
-- Close it with Quit, on its page. Built by packaging/mac/build_launcher.sh.

set repo to (POSIX path of (path to home folder)) & "crossing-count"
set pageAddress to "http://127.0.0.1:8780/"

try
	do shell script "/usr/bin/curl -s -o /dev/null -m 2 " & pageAddress
	set isRunning to true
on error
	set isRunning to false
end try

if isRunning then
	open location pageAddress
else
	do shell script "cd " & quoted form of repo & " && nohup /opt/homebrew/bin/uv run wizard.py > /tmp/crossing-count.log 2>&1 &"
end if
