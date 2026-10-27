# Cedrus H3: 1080-line NV12 upload fix

## Failure and evidence

One-KVM can probe h264_v4l2m2m at 1280x720, but the real 1920x1080 stream can
segfault in the video-encoder thread while copying a software frame to V4L2.

The affected FFmpeg upload routine uses the negotiated V4L2 height for source
reads. Cedrus rounds 1080 rows up to 1088. A tightly packed NV12 AVFrame contains
3,110,400 bytes, while the physical 1920x1088 buffer occupies 3,133,440 bytes.
The old Y copy consumes 8 rows from the following UV plane, and the UV copy
reads 4 rows (7,680 bytes) past the end of the input.

The captured H3 registers contained height 1088 and size 0x2fd000 (3,133,440).
The faulting load addressed 0xb143b008, inside the PROT_NONE mapping beginning
at 0xb143b000. The stripped backtrace cannot name the exact caller, but the
registers, mappings, source routine and guard-page reproduction agree.

## Change

The FFmpeg patch handles single-buffer NV12/NV21 queues, including a single
buffer exposed by the multiplanar API. It copies visible Y/UV rows with the
source stride, places UV after the driver's padded Y plane, and extends edge
pixels into horizontal/vertical padding. It rejects insufficient destination
storage and invalid strides before writing. Other pixel formats and separate
NV12M/NV21M buffers keep their previous paths.

Both ARM Dockerfiles apply the patch after the existing format-size guard.
The original guard is still necessary when a driver negotiates a frame smaller
than the requested image.

## Build from the One-KVM repository root

This branch starts from fix/configurable-tcp-nodelay and preserves those changes.

```bash
# Existing checkout; commit or stash unrelated local changes first.
git fetch origin
git switch --track origin/fix/cedrus-nv12-height

# Rebuild the ARMv7 cross image and force relinking of its static FFmpeg.
# Keep your already working frontend build / web/dist.
cargo clean -p hwcodec --target armv7-unknown-linux-gnueabihf
bash build/build-images.sh armv7
bash build/package-deb.sh
```

If the branch already exists locally, switch to it and fast-forward it instead
of using --track again. Existing CHINAMIRRO settings can still be used.

Updating only a Rust executable without rebuilding the cross image will not
patch its static FFmpeg. If building against a separately installed FFmpeg,
apply the patch to that FFmpeg source and rebuild/relink the library.

## Regression test

After applying the patch to an FFmpeg checkout:

```bash
python3 build/cross/tests/test-v4l2-nv12-upload.py \
  /path/to/ffmpeg/libavcodec/v4l2_buffers.c
```

Optionally add --original /path/to/unpatched/libavcodec/v4l2_buffers.c to
require that the original routine reproduces SIGSEGV in a child process.
The test needs Linux, Python 3 and a host C compiler; it does not need a codec
device or FFmpeg development packages. It compiles the actual extracted upload
functions against a small test shim.

Checked: tight 1080 -> padded 1088 input, unchanged 720p, NV12/NV21, padded
source/destination strides, negative source stride, odd visible dimensions,
single/multiplanar queue APIs and rejected buffer/stride bounds.

These tests verify copying and bounds. A full cross-build, the target FFmpeg
ABI, encoded-frame cropping and sustained H3 streaming still need validation.

## Target validation

Use the existing temporary-module script and restore the original module
after testing. First establish 1280x720 MJPEG capture with H.264/V4L2 M2M; then
repeat at 1920x1080. Confirm the actual negotiated dimensions in the log.
Check static and moving desktop content, reconnect, frame geometry and CPU
usage. Switch back to MJPEG and automatic encoder selection before restoring
the original module. Keep the kernel module experimental until these pass.

