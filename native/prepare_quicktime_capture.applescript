on run argv
    if (count of argv) is not 1 then error "缺少 iPhone/iPad 名称。"
    set targetDeviceName to item 1 of argv

    tell application "QuickTime Player" to activate
    delay 0.5
    tell application "System Events"
        tell process "QuickTime Player"
            set frontmost to true
            if (count of windows) is 0 then
                keystroke "n" using {option down, command down}
                delay 2
            end if
            if (count of windows) is 0 then error "QUICKTIME_WINDOW_UNAVAILABLE"

            set recordingWindow to front window
            set sourceButton to missing value
            repeat with candidateButton in buttons of recordingWindow
                try
                    set buttonDescription to description of candidateButton as text
                    ignoring case
                        if buttonDescription contains "采集设备" or buttonDescription contains "capture device" then
                            set sourceButton to candidateButton
                            exit repeat
                        end if
                    end ignoring
                end try
            end repeat
            if sourceButton is missing value then
                if (count of buttons of recordingWindow) >= 2 then
                    set sourceButton to button 2 of recordingWindow
                else
                    error "QUICKTIME_SOURCE_BUTTON_UNAVAILABLE"
                end if
            end if
            perform action "AXPress" of sourceButton
            delay 0.6

            set sourceMenu to menu 1 of sourceButton
            set matchingItems to every menu item of sourceMenu whose name is targetDeviceName
            if (count of matchingItems) is 0 then
                key code 53
                error "QUICKTIME_DEVICE_NOT_FOUND"
            end if
            click item 1 of matchingItems
            delay 2

            if (count of windows) is 0 then error "QUICKTIME_PREVIEW_UNAVAILABLE"
            set previewWindow to front window
            set previewSize to size of previewWindow
            return (item 1 of previewSize as text) & "x" & (item 2 of previewSize as text)
        end tell
    end tell
end run
