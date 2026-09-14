-- CrossingCount for the Mac, from this project folder: double-click to open it in its own
-- window (another window, if it is already open). Close it by closing the window, or with
-- Quit on its page. Built by packaging/mac/build_launcher.sh.

set repo to (POSIX path of (path to home folder)) & "crossing-count"
do shell script "cd " & quoted form of repo & " && nohup /opt/homebrew/bin/uv run wizard.py > /tmp/crossing-count.log 2>&1 &"
