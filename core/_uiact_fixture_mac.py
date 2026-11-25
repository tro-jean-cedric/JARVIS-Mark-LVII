"""A test window for core/uiact.py: one of each control (button, checkbox,
slider, pop-up, text field, password field, disabled button), built from native AppKit
controls (pyobjc), so core/uiact.py is tried against real Cocoa widgets.
Writes its true state to argv[1] (JSON) five times a second."""
import json
import sys

import AppKit
import objc
from Foundation import NSObject, NSTimer

STATE = sys.argv[1]
app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    ((200, 200), (380, 330)),
    AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable,
    AppKit.NSBackingStoreBuffered, False)
win.setTitle_("UIAct Native Fixture")
view = win.contentView()
clicks = [0]


class Handler(NSObject):
    def count_(self, _sender):
        clicks[0] += 1

    def tick_(self, _timer):
        with open(STATE, "w") as f:
            json.dump({"clicks": clicks[0], "dark": bool(dark.state()),
                       "volume": int(round(vol.doubleValue())),
                       "fruit": str(fruit.titleOfSelectedItem()),
                       "note": str(note.stringValue()), "password": str(pw.stringValue())}, f)


handler = Handler.alloc().init()


def button(title, y, kind=None):
    b = AppKit.NSButton.alloc().initWithFrame_(((20, y), (200, 28)))
    b.setTitle_(title)
    if kind is not None:
        b.setButtonType_(kind)
    else:
        b.setBezelStyle_(AppKit.NSBezelStyleRounded)
    view.addSubview_(b)
    return b


count = button("Count", 280)
count.setTarget_(handler)
count.setAction_(objc.selector(handler.count_, signature=b"v@:@"))
dark = button("Dark mode", 245, AppKit.NSButtonTypeSwitch)
vol = AppKit.NSSlider.alloc().initWithFrame_(((20, 210), (300, 24)))
vol.setMinValue_(0)
vol.setMaxValue_(100)
vol.setDoubleValue_(10)
vol.setAccessibilityLabel_("Volume")
view.addSubview_(vol)
fruit = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(((20, 170), (200, 28)), False)
fruit.addItemsWithTitles_(["Apple", "Banana", "Cherry"])
fruit.setAccessibilityLabel_("Fruit")
view.addSubview_(fruit)
note = AppKit.NSTextField.alloc().initWithFrame_(((20, 130), (300, 24)))
note.setAccessibilityLabel_("Note")
view.addSubview_(note)
pw = AppKit.NSSecureTextField.alloc().initWithFrame_(((20, 95), (300, 24)))
pw.setAccessibilityLabel_("Password")
view.addSubview_(pw)
locked = button("Locked", 55)
locked.setEnabled_(False)

NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
    0.2, handler, objc.selector(handler.tick_, signature=b"v@:@"), None, True)
win.makeKeyAndOrderFront_(None)
app.activateIgnoringOtherApps_(True)
app.run()
