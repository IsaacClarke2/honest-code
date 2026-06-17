# VHS tape — generates demo.gif from the tamper demo.
# Install vhs (https://github.com/charmbracelet/vhs), then:  vhs demo/demo.tape
Output demo.gif

Set FontSize 17
Set Width 1180
Set Height 760
Set Padding 18
Set Theme "Catppuccin Mocha"
Set PlaybackSpeed 1.0

Type "bash demo/tamper_demo.sh"
Sleep 600ms
Enter
Sleep 7s
