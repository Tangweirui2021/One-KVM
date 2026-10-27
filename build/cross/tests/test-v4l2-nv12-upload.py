#!/usr/bin/env python3
"""Compile the actual FFmpeg upload functions against a small Linux test shim.

Usage: python3 test-v4l2-nv12-upload.py PATCHED_V4L2_BUFFERS_C
       [--original ORIGINAL_V4L2_BUFFERS_C]

No codec device or FFmpeg development packages are needed. Guard pages catch
reads past tightly packed input and writes past the driver buffer. The optional
original-source check must reproduce SIGSEGV in a child with core dumps disabled.
This validates the copy routine, not the full FFmpeg ABI or hardware encoder.
"""
import argparse
from pathlib import Path
import re
import signal
import subprocess
import tempfile


def function(source, name):
    match = re.search(r"^static int " + re.escape(name) + r"\(", source, re.M)
    if not match:
        raise ValueError(f"Missing function: {name}")
    start = source.index("{", match.start())
    depth = 1
    end = start + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[match.start():end]


SHIM = r"""
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <linux/videodev2.h>
#define AVERROR(e) (-(e))
#define FFMIN(a,b) ((a) < (b) ? (a) : (b))
#define FFMAX(a,b) ((a) > (b) ? (a) : (b))
#define FFABS(a) ((a) >= 0 ? (a) : -(a))
#define AV_CEIL_RSHIFT(a,b) (-((-(a)) >> (b)))
enum { AV_PIX_FMT_NV12, AV_PIX_FMT_NV21 };
typedef struct { uint8_t *data; int size; } AVBufferRef;
typedef struct {
    int format, width, height;
    uint8_t *data[8];
    int linesize[8];
    AVBufferRef *buf[8];
} AVFrame;
typedef struct {
    int nb_components;
    struct { int plane; } comp[4];
    int log2_chroma_h;
} AVPixFmtDescriptor;
static const AVPixFmtDescriptor *av_pix_fmt_desc_get(int fmt) {
    static const AVPixFmtDescriptor d = { 3, {{0},{1},{1},{0}}, 1 };
    (void)fmt;
    return &d;
}
typedef struct { struct v4l2_format format; } V4L2Context;
typedef struct {
    V4L2Context *context;
    int num_planes;
    struct { void *mm_addr; unsigned int length; int bytesperline; } plane_info[8];
    struct v4l2_buffer buf;
    struct v4l2_plane planes[8];
} V4L2Buffer;
typedef struct { uint8_t *mapping, *data; size_t length, map_size; } Guard;
static Guard guard_alloc(size_t length) {
    size_t page = (size_t)sysconf(_SC_PAGESIZE);
    size_t rounded = (length + page - 1) / page * page;
    Guard g = {0};
    g.length = length;
    g.map_size = rounded + page;
    g.mapping = mmap(NULL, g.map_size, PROT_READ | PROT_WRITE,
                     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    assert(g.mapping != MAP_FAILED);
    assert(mprotect(g.mapping + rounded, page, PROT_NONE) == 0);
    g.data = g.mapping + rounded - length;
    return g;
}
static void guard_free(Guard *g) { assert(munmap(g->mapping, g->map_size) == 0); }
"""

CASES = r"""
static void init_out(V4L2Buffer *out, V4L2Context *ctx, Guard *dst,
                     int stride, int height, int nv21, int multi) {
    memset(out, 0, sizeof(*out));
    memset(ctx, 0, sizeof(*ctx));
    out->context = ctx;
    out->num_planes = 1;
    out->buf.type = ctx->format.type = multi ?
        V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE : V4L2_BUF_TYPE_VIDEO_OUTPUT;
    if (multi) {
        ctx->format.fmt.pix_mp.height = height;
        ctx->format.fmt.pix_mp.pixelformat = nv21 ? V4L2_PIX_FMT_NV21 : V4L2_PIX_FMT_NV12;
    } else {
        ctx->format.fmt.pix.height = height;
        ctx->format.fmt.pix.pixelformat = nv21 ? V4L2_PIX_FMT_NV21 : V4L2_PIX_FMT_NV12;
    }
    out->plane_info[0].mm_addr = dst->data;
    out->plane_info[0].length = dst->length;
    out->plane_info[0].bytesperline = stride;
}
static void run_case(int w, int h, int src_stride, int dst_stride, int dh,
                     int nv21, int negative, int multi, int original) {
    int uvw = (w + 1) / 2 * 2, uvh = (h + 1) / 2;
    size_t sy = (size_t)src_stride * h, dy = (size_t)dst_stride * dh;
    size_t dsize = dy + (size_t)dst_stride * ((dh + 1) / 2);
    Guard src = guard_alloc(sy + (size_t)src_stride * uvh);
    Guard dst = guard_alloc(dsize);
    V4L2Buffer out;
    V4L2Context ctx;
    AVFrame f = {0};
    f.width = w; f.height = h; f.format = nv21 ? AV_PIX_FMT_NV21 : AV_PIX_FMT_NV12;
    memset(src.data, 0xcd, src.length);
    memset(dst.data, 0xa5, dst.length);
    for (int p = 0; p < 2; p++) {
        int rows = p ? uvh : h, bytes = p ? uvw : w;
        uint8_t *base = src.data + (p ? sy : 0);
        for (int y = 0; y < rows; y++)
            for (int x = 0; x < bytes; x++)
                base[(size_t)y * src_stride + x] = (uint8_t)(p * 71 + y * 3 + x * 5);
        f.linesize[p] = negative ? -src_stride : src_stride;
        f.data[p] = base + (negative ? (size_t)(rows - 1) * src_stride : 0);
    }
    init_out(&out, &ctx, &dst, dst_stride, dh, nv21, multi);
    int ret;
#ifdef WITH_ORIGINAL
    if (original) {
        ret = original_swframe_to_buf(&f, &out);
    } else
#else
    (void)original;
#endif
    {
        ret = v4l2_buffer_swframe_to_buf(&f, &out);
    }
    assert(ret == 0);
    assert((multi ? out.planes[0].bytesused : out.buf.bytesused) == dsize);
    for (int p = 0; p < 2; p++) {
        int rows = p ? uvh : h, drows = p ? (dh + 1) / 2 : dh;
        int bytes = p ? uvw : w, sample = p ? 2 : 1;
        uint8_t *base = dst.data + (p ? dy : 0);
        for (int y = 0; y < drows; y++) {
            int source_y = FFMIN(y, rows - 1);
            const uint8_t *line = f.data[p] + (ptrdiff_t)source_y * f.linesize[p];
            for (int x = 0; x < dst_stride; x++) {
                int sx = x < bytes ? x : bytes - sample + (x - bytes) % sample;
                assert(base[(size_t)y * dst_stride + x] == line[sx]);
            }
        }
    }
    guard_free(&src); guard_free(&dst);
}
static void reject_bad_layouts(void) {
    Guard src = guard_alloc(24), dst = guard_alloc(48);
    V4L2Buffer out;
    V4L2Context ctx;
    AVFrame f = {0};
    f.width = 4; f.height = 4; f.format = AV_PIX_FMT_NV12;
    f.data[0] = src.data; f.data[1] = src.data + 16;
    f.linesize[0] = f.linesize[1] = 4;
    memset(dst.data, 0xa5, dst.length);
    init_out(&out, &ctx, &dst, 4, 8, 0, 0);
    out.plane_info[0].length = 47;
    assert(v4l2_buffer_swframe_to_buf(&f, &out) == AVERROR(EINVAL));
    assert(dst.data[0] == 0xa5);
    out.plane_info[0].length = dst.length;
    f.linesize[1] = 3;
    assert(v4l2_buffer_swframe_to_buf(&f, &out) == AVERROR(EINVAL));
    f.linesize[1] = 4;
    f.height = 10;
    assert(v4l2_buffer_swframe_to_buf(&f, &out) == AVERROR(EINVAL));
    f.height = 4; f.data[1] = NULL;
    assert(v4l2_buffer_swframe_to_buf(&f, &out) == AVERROR(EINVAL));
    guard_free(&src); guard_free(&dst);
}
int main(void) {
    struct rlimit no_core = {0, 0};
    assert(setrlimit(RLIMIT_CORE, &no_core) == 0);
#ifdef WITH_ORIGINAL
    pid_t child = fork();
    assert(child >= 0);
    if (child == 0) {
        run_case(1920, 1080, 1920, 1920, 1088, 0, 0, 0, 1);
        _exit(0);
    }
    int status;
    assert(waitpid(child, &status, 0) == child);
    assert(WIFSIGNALED(status) && WTERMSIG(status) == SIGSEGV);
    puts("PASS: original upload reproduces SIGSEGV at 1080 -> 1088");
#endif
    run_case(1920, 1080, 1920, 1920, 1088, 0, 0, 0, 0);
    run_case(1920, 1080, 1920, 1920, 1088, 1, 0, 1, 0);
    run_case(1280, 720, 1280, 1280, 720, 0, 0, 0, 0);
    run_case(638, 478, 638, 640, 480, 0, 0, 0, 0);
    run_case(639, 479, 640, 640, 480, 0, 0, 1, 0);
    run_case(638, 478, 672, 640, 480, 1, 0, 0, 0);
    run_case(638, 478, 640, 640, 480, 0, 1, 0, 0);
    reject_bad_layouts();
    puts("PASS: visible Y/UV pixels, padding, strides, queue types, and bounds");
    return 0;
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("patched", type=Path)
    parser.add_argument("--original", type=Path)
    parser.add_argument("--cc", default="cc")
    args = parser.parse_args()
    patched = args.patched.read_text()
    names = ["v4l2_bufref_to_buf", "v4l2_buffer_copy_nv12",
             "v4l2_buffer_swframe_to_buf"]
    code = SHIM
    if args.original:
        original = args.original.read_text()
        code += "\n#define WITH_ORIGINAL\n"
        for name in ("v4l2_bufref_to_buf", "v4l2_buffer_swframe_to_buf"):
            body = function(original, name)
            body = body.replace("v4l2_bufref_to_buf", "original_bufref_to_buf")
            body = body.replace("v4l2_buffer_swframe_to_buf", "original_swframe_to_buf")
            code += body + "\n"
    code += "\n".join(function(patched, name) for name in names) + CASES
    with tempfile.TemporaryDirectory(prefix="v4l2-nv12-") as directory:
        source = Path(directory) / "test.c"
        binary = Path(directory) / "test"
        source.write_text(code)
        subprocess.run([args.cc, "-std=c11", "-O2", "-Wall", "-Wextra",
                        "-Wno-sign-compare", str(source), "-o", str(binary)],
                       check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    main()

