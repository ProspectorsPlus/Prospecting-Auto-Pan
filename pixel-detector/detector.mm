// pixel-detector/detector.mm
//
// ScreenCaptureKit-backed pixel color reader, exposed to Python via pybind11.
//
// Prerequisites (not run by this repo, do these once by hand):
//   xcode-select --install
//   pip install pybind11
//
// Streams the main display through ScreenCaptureKit and lets Python read the
// color of one target pixel at up to the stream's frame rate. The capture
// callback runs on its own dispatch queue and never touches Python or the
// GIL; it only updates a small atomic pixel state that get_pixel_color()
// reads. Screen Recording permission is requested the first time
// SCShareableContent is asked for, and that prompt is asynchronous — this
// file never guesses how long that takes with a fixed sleep; instead
// start_stream() blocks (with the GIL released) on the real callback, and
// is_ready() lets Python poll for the first real frame after the stream is
// up.

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#import <Cocoa/Cocoa.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#import <CoreGraphics/CoreGraphics.h>

#include <atomic>
#include <cmath>
#include <mutex>
#include <stdexcept>

namespace py = pybind11;

namespace {

std::atomic<uint8_t> g_r{0};
std::atomic<uint8_t> g_g{0};
std::atomic<uint8_t> g_b{0};
std::atomic<bool> g_ready{false};

std::mutex g_target_mutex;
double g_target_x_logical = 0.0;
double g_target_y_logical = 0.0;
double g_target_box_logical = 1.0;

double BackingScaleFactor() {
    NSScreen *screen = [NSScreen mainScreen];
    return screen ? screen.backingScaleFactor : 1.0;
}

}  // namespace

@interface PixelStreamOutput : NSObject <SCStreamOutput, SCStreamDelegate>
@end

@implementation PixelStreamOutput

- (void)stream:(SCStream *)stream
    didOutputSampleBuffer:(CMSampleBufferRef)sampleBuffer
                    ofType:(SCStreamOutputType)type {
    if (type != SCStreamOutputTypeScreen) {
        return;
    }
    if (!CMSampleBufferIsValid(sampleBuffer)) {
        return;
    }

    CVPixelBufferRef pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer);
    if (pixelBuffer == NULL) {
        return;
    }

    CVPixelBufferLockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);

    size_t width = CVPixelBufferGetWidth(pixelBuffer);
    size_t height = CVPixelBufferGetHeight(pixelBuffer);
    size_t bytesPerRow = CVPixelBufferGetBytesPerRow(pixelBuffer);
    uint8_t *base = static_cast<uint8_t *>(CVPixelBufferGetBaseAddress(pixelBuffer));

    double scale = BackingScaleFactor();
    double targetXLogical;
    double targetYLogical;
    double targetBoxLogical;
    {
        std::lock_guard<std::mutex> lock(g_target_mutex);
        targetXLogical = g_target_x_logical;
        targetYLogical = g_target_y_logical;
        targetBoxLogical = g_target_box_logical;
    }
    long centerX = std::lround(targetXLogical * scale);
    long centerY = std::lround(targetYLogical * scale);
    // Same NxN mean-box semantics as CapturedFrame.sample_mean_rgb (only the
    // top/left edge is clamped; the bottom/right edge is clamped by the loop
    // bounds below, matching numpy's slice-truncation there) - so a value
    // read here previews exactly what the production frame pipeline would
    // read at the same client point.
    long boxPhysical = std::max<long>(1, std::lround(targetBoxLogical * scale));
    long half = boxPhysical / 2;
    long top = std::max<long>(0, centerY - half);
    long left = std::max<long>(0, centerX - half);
    long bottom = std::min<long>(static_cast<long>(height), top + boxPhysical);
    long right = std::min<long>(static_cast<long>(width), left + boxPhysical);

    if (base != NULL && top < bottom && left < right) {
        // BGRA32, ScreenCaptureKit's default sample buffer layout.
        double sumB = 0.0, sumG = 0.0, sumR = 0.0;
        long count = 0;
        for (long y = top; y < bottom; ++y) {
            const uint8_t *row = base + static_cast<size_t>(y) * bytesPerRow;
            for (long x = left; x < right; ++x) {
                const uint8_t *pixel = row + static_cast<size_t>(x) * 4;
                sumB += pixel[0];
                sumG += pixel[1];
                sumR += pixel[2];
                ++count;
            }
        }
        if (count > 0) {
            g_b.store(static_cast<uint8_t>(std::lround(sumB / count)), std::memory_order_relaxed);
            g_g.store(static_cast<uint8_t>(std::lround(sumG / count)), std::memory_order_relaxed);
            g_r.store(static_cast<uint8_t>(std::lround(sumR / count)), std::memory_order_relaxed);
            g_ready.store(true, std::memory_order_release);
        }
    }

    CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
}

- (void)stream:(SCStream *)stream didStopWithError:(NSError *)error {
    // Surfaced implicitly: is_ready() simply stops advancing past this point.
}

@end

namespace {

SCStream *g_stream = nil;
PixelStreamOutput *g_output = nil;

void SetTargetLocked(double x, double y, double box_px) {
    std::lock_guard<std::mutex> lock(g_target_mutex);
    g_target_x_logical = x;
    g_target_y_logical = y;
    g_target_box_logical = box_px;
}

void StartStreamImpl(double x, double y, double box_px) {
    SetTargetLocked(x, y, box_px);
    g_ready.store(false, std::memory_order_release);

    if (g_stream != nil) {
        return;  // Already streaming; target above is enough.
    }

    dispatch_semaphore_t contentSemaphore = dispatch_semaphore_create(0);
    __block SCDisplay *targetDisplay = nil;
    __block NSString *contentErrorDescription = nil;

    [SCShareableContent
        getShareableContentWithCompletionHandler:^(SCShareableContent *content, NSError *error) {
            if (error != nil) {
                contentErrorDescription = error.localizedDescription;
            } else {
                CGDirectDisplayID mainDisplayId = CGMainDisplayID();
                for (SCDisplay *display in content.displays) {
                    if (display.displayID == mainDisplayId) {
                        targetDisplay = display;
                        break;
                    }
                }
                if (targetDisplay == nil && content.displays.count > 0) {
                    targetDisplay = content.displays.firstObject;
                }
            }
            dispatch_semaphore_signal(contentSemaphore);
        }];

    // This is the step gated on the (possibly first-ever, user-facing,
    // arbitrarily slow) Screen Recording permission prompt.
    dispatch_semaphore_wait(contentSemaphore, DISPATCH_TIME_FOREVER);

    if (targetDisplay == nil) {
        std::string detail = contentErrorDescription != nil
                                  ? std::string(contentErrorDescription.UTF8String)
                                  : std::string("no shareable display found");
        throw std::runtime_error("ScreenCaptureKit: " + detail);
    }

    SCContentFilter *filter = [[SCContentFilter alloc] initWithDisplay:targetDisplay
                                                       excludingWindows:@[]];

    double scale = BackingScaleFactor();
    SCStreamConfiguration *config = [[SCStreamConfiguration alloc] init];
    config.width = static_cast<size_t>(std::lround(targetDisplay.width * scale));
    config.height = static_cast<size_t>(std::lround(targetDisplay.height * scale));
    config.pixelFormat = kCVPixelFormatType_32BGRA;
    config.colorSpaceName = kCGColorSpaceSRGB;
    config.minimumFrameInterval = CMTimeMake(1, 60);
    config.queueDepth = 3;
    config.showsCursor = NO;

    g_output = [[PixelStreamOutput alloc] init];
    g_stream = [[SCStream alloc] initWithFilter:filter configuration:config delegate:g_output];

    dispatch_queue_t captureQueue =
        dispatch_queue_create("pixel-detector.capture", DISPATCH_QUEUE_SERIAL);
    NSError *addError = nil;
    BOOL added = [g_stream addStreamOutput:g_output
                                       type:SCStreamOutputTypeScreen
                         sampleHandlerQueue:captureQueue
                                      error:&addError];
    if (!added) {
        g_stream = nil;
        g_output = nil;
        std::string detail =
            addError != nil ? std::string(addError.localizedDescription.UTF8String) : "";
        throw std::runtime_error("ScreenCaptureKit: addStreamOutput failed: " + detail);
    }

    dispatch_semaphore_t startSemaphore = dispatch_semaphore_create(0);
    __block NSString *startErrorDescription = nil;
    [g_stream startCaptureWithCompletionHandler:^(NSError *error) {
        if (error != nil) {
            startErrorDescription = error.localizedDescription;
        }
        dispatch_semaphore_signal(startSemaphore);
    }];
    dispatch_semaphore_wait(startSemaphore, DISPATCH_TIME_FOREVER);

    if (startErrorDescription != nil) {
        g_stream = nil;
        g_output = nil;
        throw std::runtime_error("ScreenCaptureKit: startCapture failed: " +
                                  std::string(startErrorDescription.UTF8String));
    }
}

void StartStream(double x, double y, double box_px) {
    py::gil_scoped_release release;
    StartStreamImpl(x, y, box_px);
}

void UpdateTarget(double x, double y, double box_px) { SetTargetLocked(x, y, box_px); }

py::tuple GetPixelColor() {
    return py::make_tuple(static_cast<int>(g_r.load(std::memory_order_relaxed)),
                           static_cast<int>(g_g.load(std::memory_order_relaxed)),
                           static_cast<int>(g_b.load(std::memory_order_relaxed)));
}

bool IsReady() { return g_ready.load(std::memory_order_acquire); }

}  // namespace

PYBIND11_MODULE(pixel_detector, m) {
    m.doc() = "ScreenCaptureKit-backed pixel color reader, NxN mean-box sampling.";
    m.def("start_stream", &StartStream, py::arg("x"), py::arg("y"), py::arg("box_px") = 1.0,
          "Start streaming the main display and set the initial target "
          "(logical screen coordinates). box_px is the mean-sample box side, "
          "in the same logical-point units as x/y; 1 reads a single pixel. "
          "Blocks until the stream is confirmed started; does not wait for "
          "the first frame (use is_ready() for that).");
    m.def("update_target", &UpdateTarget, py::arg("x"), py::arg("y"), py::arg("box_px") = 1.0,
          "Change what get_pixel_color() reports: target point and mean-box "
          "side, both in logical screen coordinates.");
    m.def("get_pixel_color", &GetPixelColor,
          "Return the last-sampled mean (r, g, b) over the target box, 0-255 each.");
    m.def("is_ready", &IsReady, "True once at least one real frame has landed.");
}
