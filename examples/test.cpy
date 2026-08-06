import "ApplicationServices/ApplicationServices.h"

def callback(proxy void*, type_: int, event void*, user_data void*):
    if type_ == kCGEventTapDisabledByTimeout or \
       type_ == kCGEventTapDisabledByUserInput:
        CGEventTapEnable(user_data, true)
        return event

    if type_ == kCGEventKeyDown or \
       type_ == kCGEventKeyUp or \
       type_ == kCGEventFlagsChanged:
        return null      # Drop the event

    return event         # Pass everything else through

def main():
    mask = \
        CGEventMaskBit(kCGEventKeyDown) | \
        CGEventMaskBit(kCGEventKeyUp) | \
        CGEventMaskBit(kCGEventFlagsChanged)

    tap = CGEventTapCreate( \
        kCGSessionEventTap, \
        kCGHeadInsertEventTap, \
        kCGEventTapOptionDefault, \
        mask, \
        callback, \
        null \
    )

    if tap == null:
        print("Failed to create event tap")
        return 1

    let source = CFMachPortCreateRunLoopSource(null, tap, 0)

    CFRunLoopAddSource( \
        CFRunLoopGetCurrent(), \
        source, \
        kCFRunLoopCommonModes \
    )

    CGEventTapEnable(tap, true)
    CFRunLoopRun()

    return 0