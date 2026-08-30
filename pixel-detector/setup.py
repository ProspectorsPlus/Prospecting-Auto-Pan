# pixel-detector/setup.py
#
# Prerequisites (run once by hand, not by this script):
#   xcode-select --install
#   pip install pybind11
#
# Build with:
#   python setup.py build_ext --inplace

import pybind11
from distutils.unixccompiler import UnixCCompiler
from setuptools import Extension, setup

# setuptools/distutils' default compiler only recognizes .c/.cc/.cpp/.cxx/.m
# out of the box -- .mm (Objective-C++) needs to be registered by hand.
if ".mm" not in UnixCCompiler.src_extensions:
    UnixCCompiler.src_extensions.append(".mm")
UnixCCompiler.language_map[".mm"] = "objc++"

ext_modules = [
    Extension(
        "pixel_detector",
        ["detector.mm"],
        include_dirs=[pybind11.get_include()],
        language="c++",
        extra_compile_args=[
            "-std=c++17",
            "-stdlib=libc++",
            "-O3",
            "-fobjc-arc",  # without ARC every Cocoa alloc/init here leaks
        ],
        extra_link_args=[
            "-stdlib=libc++",
            "-framework",
            "Cocoa",
            "-framework",
            "ScreenCaptureKit",
            "-framework",
            "CoreMedia",
            "-framework",
            "CoreVideo",
            "-framework",
            "CoreGraphics",
        ],
    ),
]

setup(
    name="pixel_detector",
    version="0.1.0",
    description="ScreenCaptureKit-backed single-pixel color reader",
    ext_modules=ext_modules,
)
