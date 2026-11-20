on run argv
    set requestFile to POSIX file (item 1 of argv)
    set messageText to read requestFile as «class utf8»
    set secondsLeft to (item 2 of argv) as integer
    activate
    try
        set answer to display dialog messageText with title "MCP Guardian — Approval required" buttons {"Block", "Allow once"} default button "Block" cancel button "Block" with icon caution giving up after secondsLeft
        if gave up of answer then return "timeout"
        if button returned of answer is "Allow once" then return "allow"
        return "block"
    on error number -128
        return "block"
    end try
end run
