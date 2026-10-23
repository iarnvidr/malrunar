#!/usr/bin/env python3
"""Test helper: map one X window with a WM_CLASS and a WM_WINDOW_ROLE and keep it until killed.
    xwindow.py <class> <instance> <role> [title]
Uses libX11 through ctypes, so it needs nothing but desktop-base."""
import ctypes
import sys
import time

cls, instance, role = sys.argv[1:4]
title = sys.argv[4] if len(sys.argv) > 4 else instance
x11 = ctypes.CDLL("libX11.so.6")
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XDefaultRootWindow.restype = ctypes.c_ulong
x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
x11.XCreateSimpleWindow.restype = ctypes.c_ulong
x11.XCreateSimpleWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_ulong]
x11.XInternAtom.restype = ctypes.c_ulong
x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
x11.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int,
                                ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
x11.XStoreName.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p]
x11.XMapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
x11.XFlush.argtypes = [ctypes.c_void_p]
x11.XSetClassHint.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]


class ClassHint(ctypes.Structure):
    _fields_ = [("res_name", ctypes.c_char_p), ("res_class", ctypes.c_char_p)]


display = x11.XOpenDisplay(None)
if not display:
    sys.exit("xwindow: cannot open the display")
window = x11.XCreateSimpleWindow(display, x11.XDefaultRootWindow(display), 0, 0, 300, 200, 0, 0, 0)
hint = ClassHint(instance.encode(), cls.encode())
x11.XSetClassHint(display, window, ctypes.byref(hint))
x11.XStoreName(display, window, title.encode())
atom = x11.XInternAtom(display, b"WM_WINDOW_ROLE", 0)
x11.XChangeProperty(display, window, atom, 31, 8, 0, role.encode(), len(role))   # XA_STRING, 8 bit, replace
x11.XMapWindow(display, window)
x11.XFlush(display)
while True:
    time.sleep(1)
